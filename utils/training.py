# Copyright 2022-present, Lorenzo Bonicelli, Pietro Buzzega, Matteo Boschini, Angelo Porrello, Simone Calderara.
# All rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import math
import sys
from argparse import Namespace
from typing import Tuple

import torch
from datasets import get_dataset
from datasets.utils.continual_dataset import ContinualDataset
from models.utils.continual_model import ContinualModel

from utils.loggers import *
from utils.status import ProgressBar
from utils.saliency_metrics import compute_saliency_metrics
import pickle
from copy import deepcopy
try:
    import wandb
except ImportError:
    wandb = None


def mask_classes(outputs: torch.Tensor, dataset: ContinualDataset, k: int) -> None:
    """
    Given the output tensor, the dataset at hand and the current task,
    masks the former by setting the responses for the other tasks at -inf.
    It is used to obtain the results for the task-il setting.
    :param outputs: the output tensor
    :param dataset: the continual dataset
    :param k: the task index
    """
    outputs[:, 0:k * dataset.N_CLASSES_PER_TASK] = -float('inf')
    outputs[:, (k + 1) * dataset.N_CLASSES_PER_TASK:
               dataset.N_TASKS * dataset.N_CLASSES_PER_TASK] = -float('inf')


def evaluate(model: ContinualModel, dataset: ContinualDataset, last=False) -> Tuple[list, list]:
    """
    Evaluates the accuracy of the model for each past task.
    :param model: the model to be evaluated
    :param dataset: the continual dataset at hand
    :return: a tuple of lists, containing the class-il
             and task-il accuracy for each task
    """
    status = model.net.training
    model.net.eval()
    #cl_saliency_model
    if hasattr(model, 'saliency_net'):
        sal_status = model.saliency_net.training
        model.saliency_net.eval()
    
    accs, accs_mask_classes = [], []
    sal_scores = []
    for k, test_loader in enumerate(dataset.test_loaders):
        if last and k < len(dataset.test_loaders) - 1:
            continue
        correct, correct_mask_classes, total = 0.0, 0.0, 0.0
        for data in test_loader:
            with torch.no_grad():
                inputs, labels = data
                if isinstance(inputs, list):
                    inputs = [inp.to(model.device) for inp in inputs]
                else:
                    inputs = inputs.to(model.device)
                labels = labels.to(model.device)

                if hasattr(model, 'saliency_net'):
                    if 'class-il' not in model.COMPATIBILITY:
                        sal_preds, outputs = model(inputs, k)
                    else:
                        sal_preds, outputs = model(inputs)
                else:    
                    if 'class-il' not in model.COMPATIBILITY:
                        outputs = model(inputs, k)
                    else:
                        outputs = model(inputs)

                _, pred = torch.max(outputs.data, 1)
                correct += torch.sum(pred == labels).item()
                total += labels.shape[0]

                if hasattr(model, 'saliency_net'):
                    assert isinstance(inputs, list)
                    #compute saliency metrics
                    sal_metrics = compute_saliency_metrics(sal_preds, inputs[1], metrics = ('kld', 'cc', 'sim'))
                    sal_scores.append(sal_metrics)

                if dataset.SETTING == 'class-il':
                    mask_classes(outputs, dataset, k)
                    _, pred = torch.max(outputs.data, 1)
                    correct_mask_classes += torch.sum(pred == labels).item()

        accs.append(correct / total * 100
                    if 'class-il' in model.COMPATIBILITY else 0)
        accs_mask_classes.append(correct_mask_classes / total * 100)
        
        if hasattr(model, 'saliency_net'):
            final_sal_scores = []
            for m_index in range(len(sal_scores[0])):
                values = [s[m_index] for s in sal_scores]
                values = torch.cat(values)
                final_sal_scores.append(torch.mean(values).item())

    model.net.train(status)
    if hasattr(model, 'saliency_net'):
        model.saliency_net.train(sal_status)
        return accs, accs_mask_classes, final_sal_scores

    return accs, accs_mask_classes


def train(model: ContinualModel, dataset: ContinualDataset,
          args: Namespace) -> None:
    """
    The training process, including evaluations and loggers.
    :param model: the module to be trained
    :param dataset: the continual dataset at hand
    :param args: the arguments of the current execution
    """
    print(args)

    if not args.nowand:
        assert wandb is not None, "Wandb not installed, please install it or run without wandb"
        wandb.init(project=args.wandb_project, entity=args.wandb_entity, config=vars(args))
        args.wandb_url = wandb.run.get_url()

    model.net.to(model.device) # model.net
    if hasattr(model, 'saliency_net'):
        model.saliency_net.to(model.device)
    results, results_mask_classes = [], []

    if not args.disable_log:
        logger = Logger(dataset.SETTING, dataset.NAME, model.NAME)

    progress_bar = ProgressBar(verbose=not args.non_verbose)

    if not args.ignore_other_metrics:
        dataset_copy = get_dataset(args)
        for t in range(dataset.N_TASKS):
            model.net.train()
            _, _ = dataset_copy.get_data_loaders()

    print(file=sys.stderr)
    for t in range(dataset.N_TASKS):
        model.net.train()
        train_loader, test_loader = dataset.get_data_loaders()
        if hasattr(model, 'begin_task'):
            model.begin_task(dataset) # call the begin_task method of the model

        if t and not args.ignore_other_metrics:
            accs = evaluate(model, dataset, last=True)
            results[t-1] = results[t-1] + accs[0]
            if dataset.SETTING == 'class-il':
                results_mask_classes[t-1] = results_mask_classes[t-1] + accs[1]

        scheduler = dataset.get_scheduler(model, args)
        print(f"Task: {t+1}; num_images: {len(train_loader.dataset.data)}")        
        for epoch in range(model.args.n_epochs):
            if args.model == 'joint':
                continue
            for i, data in enumerate(train_loader):
                if args.debug_mode and i > 3:
                    break
                if hasattr(dataset.train_loader.dataset, 'logits'):
                    inputs, labels, not_aug_inputs, logits = data
                    if isinstance(inputs, list):
                        inputs = [inp.to(model.device) for inp in inputs]
                    else:
                        inputs = inputs.to(model.device)  
                    labels = labels.to(model.device)
                    not_aug_inputs = not_aug_inputs.to(model.device)
                    logits = logits.to(model.device)
                    loss = model.meta_observe(inputs, labels, not_aug_inputs, logits) # call the meta_observe method of the model
                else:
                    inputs, labels, not_aug_inputs = data
                    if isinstance(inputs, list):
                        inputs = [inp.to(model.device) for inp in inputs]
                    else:
                        inputs = inputs.to(model.device)    
                    labels = labels.to(model.device)
                    not_aug_inputs = not_aug_inputs.to(model.device)
                    loss = model.meta_observe(inputs, labels, not_aug_inputs)
                if isinstance(loss, list):  
                    assert not math.isnan(loss[0])
                else: 
                    assert not math.isnan(loss)
                progress_bar.prog(i, len(train_loader), epoch, t, loss)

            if scheduler is not None:
                scheduler.step()

            if hasattr(model, 'saliency_net'):
                if hasattr(model, 'saliency_scheduler') and model.saliency_scheduler is not None:
                    model.saliency_scheduler.step()

        if hasattr(model, 'end_task'):
            model.end_task(dataset)

        accs = evaluate(model, dataset)
        results.append(accs[0])
        results_mask_classes.append(accs[1])
        if hasattr(model, 'saliency_net') and len(accs)>2:
            sal_metrics = accs[-1]
            accs = accs[:-1]
        else:
            sal_metrics = [0., 0., 0.]

        mean_acc = np.mean(accs, axis=1)
        print_mean_accuracy(mean_acc, t + 1, dataset.SETTING)

        if not args.disable_log:
            logger.log(mean_acc)
            logger.log_fullacc(accs)

        if not args.nowand:
            d2={'RESULT_class_mean_accs': mean_acc[0], 'RESULT_task_mean_accs': mean_acc[1],
                'kld':sal_metrics[0], 'cc':sal_metrics[1], 'sim':sal_metrics[2],
                **{f'RESULT_class_acc_{i}': a for i, a in enumerate(accs[0])},
                **{f'RESULT_task_acc_{i}': a for i, a in enumerate(accs[1])}}
            if hasattr(model, 'saliency_net'):
                if hasattr(model, 'saliency_scheduler') and model.saliency_scheduler is not None:
                    lr = model.saliency_opt.param_groups[0]['lr']
                    d2.update({'sal_lr':lr})
            wandb.log(d2)
        
        if args.savecheck:
            print(f"Saving checkpoint into: data/results/{args.ckpt_name}")
            create_if_not_exists(f'data/results/{args.ckpt_name}')
            # model
            torch.save(model.net.state_dict(), f'data/results/{args.ckpt_name}/{args.ckpt_name}_{t}.pt')
            # saliency_net (if exists)
            if hasattr(model, 'saliency_net'):
                torch.save(model.saliency_net.state_dict(), f'data/results/{args.ckpt_name}/{args.ckpt_name}_sal_model_{t}.pt')
            if 'buffer_size' in model.args:
                with open(f'data/results/{args.ckpt_name}/{args.ckpt_name_replace.format("bufferoni")}_{t}.pkl', 'wb') as f:
                    pickle.dump(obj=deepcopy(model.buffer).to('cpu'), file=f)
            with open(f'data/results/{args.ckpt_name}/{args.ckpt_name_replace.format("interpr")}_{t}.pkl', 'wb') as f:
                pickle.dump(obj=args, file=f)
            
            with open(f'data/results/{args.ckpt_name}/{args.ckpt_name_replace.format("results")}_{t}.pkl', 'wb') as f:
                pickle.dump(obj=[
                    results, 
                    results_mask_classes, 
                    sal_metrics,
                    logger.dump()], file=f)

    if not args.disable_log and not args.ignore_other_metrics:
        logger.add_bwt(results, results_mask_classes)
        logger.add_forgetting(results, results_mask_classes)

    if not args.disable_log:
        logger.write(vars(args))
        if not args.nowand:
            d = logger.dump()
            d['wandb_url'] = wandb.run.get_url()
            wandb.log(d)

    if not args.nowand:
        wandb.finish()