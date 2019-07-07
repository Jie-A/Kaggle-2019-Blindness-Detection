from __future__ import absolute_import

import argparse
import collections
import multiprocessing
import os
from datetime import datetime
from functools import partial

import cv2
import numpy as np
import torch
from catalyst.dl import SupervisedRunner, EarlyStoppingCallback
from catalyst.dl.callbacks import F1ScoreCallback, AccuracyCallback
from catalyst.utils import load_checkpoint, unpack_checkpoint
from pytorch_toolbelt.utils import fs
from pytorch_toolbelt.utils.catalyst import ShowPolarBatchesCallback, ConfusionMatrixCallback
from pytorch_toolbelt.utils.random import set_manual_seed
from pytorch_toolbelt.utils.torch_utils import maybe_cuda, count_parameters, to_numpy, rgb_image_from_tensor, set_trainable
from sklearn.model_selection import train_test_split
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data import DataLoader
from tqdm import tqdm
import pandas as pd
from pytorch_toolbelt.utils.catalyst.visualization import draw_binary_segmentation_predictions

from retinopathy.lib.callbacks import CappaScoreCallback
from retinopathy.lib.dataset import RetinopathyDataset, CLASS_NAMES
from retinopathy.lib.factory import get_model, get_loss, get_optimizer, get_optimizable_parameters, get_train_aug, get_test_aug
from retinopathy.lib.visualization import draw_classification_predictions


def get_dataloaders(data_dir, batch_size, num_workers,
                    image_size, augmentation, fast, fold):
    dataset_fname = os.path.join(data_dir, 'train.csv') if fold is None else os.path.join(data_dir, 'train_with_folds.csv')
    dataset = pd.read_csv(dataset_fname)
    dataset['id_code'] = dataset['id_code'].apply(lambda x: os.path.join(data_dir, 'train_images', f'{x}.png'))

    if fold is not None:
        train_set = dataset[dataset['fold'] != fold]
        valid_set = dataset[dataset['fold'] == fold]

        train_x = train_set['id_code']
        train_y = train_set['diagnosis']

        valid_x = valid_set['id_code']
        valid_y = valid_set['diagnosis']
    else:
        x = dataset['id_code']
        y = dataset['diagnosis']
        train_x, valid_x, train_y, valid_y = train_test_split(x, y, random_state=42, test_size=0.1, shuffle=True, stratify=y)

    if fast:
        train_x = train_x[:32]
        train_y = train_y[:32]

        valid_x = valid_x[:32]
        valid_y = valid_y[:32]

        num_workers = 0

    train_ds = RetinopathyDataset(train_x, train_y, transform=get_train_aug(image_size, augmentation))
    valid_ds = RetinopathyDataset(valid_x, valid_y, transform=get_test_aug(image_size, augmentation))

    train_dl = DataLoader(train_ds, batch_size=batch_size, shuffle=True, pin_memory=True, drop_last=True, num_workers=num_workers)
    valid_dl = DataLoader(valid_ds, batch_size=batch_size, shuffle=False, pin_memory=True, drop_last=False, num_workers=num_workers)

    return train_dl, valid_dl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=42, help='Random seed')
    parser.add_argument('--fast', action='store_true')
    parser.add_argument('-dd', '--data-dir', type=str, default='data', help='Data directory for INRIA sattelite dataset')
    parser.add_argument('-m', '--model', type=str, default='cls_resnet18', help='')
    parser.add_argument('-b', '--batch-size', type=int, default=8, help='Batch Size during training, e.g. -b 64')
    parser.add_argument('-e', '--epochs', type=int, default=100, help='Epoch to run')
    parser.add_argument('-es', '--early-stopping', type=int, default=None, help='Maximum number of epochs without improvement')
    parser.add_argument('-f', '--fold', type=int, default=None)
    parser.add_argument('-fe', '--freeze-encoder', action='store_true')
    parser.add_argument('-lr', '--learning-rate', type=float, default=1e-4, help='Initial learning rate')
    parser.add_argument('-l', '--criterion', type=str, default='ce', help='Criterion')
    parser.add_argument('-o', '--optimizer', default='Adam', help='Name of the optimizer')
    parser.add_argument('-c', '--checkpoint', type=str, default=None, help='Checkpoint filename to use as initial model weights')
    parser.add_argument('-w', '--workers', default=multiprocessing.cpu_count(), type=int, help='Num workers')
    parser.add_argument('-a', '--augmentations', default='hard', type=str, help='')
    parser.add_argument('-tta', '--tta', default=None, type=str, help='Type of TTA to use [fliplr, d4]')
    parser.add_argument('-tm', '--train-mode', default='random', type=str, help='')
    parser.add_argument('-rm', '--run-mode', default='fit_predict', type=str, help='')
    parser.add_argument('--transfer', default=None, type=str, help='')
    parser.add_argument('--fp16', action='store_true')

    args = parser.parse_args()
    set_manual_seed(args.seed)

    data_dir = args.data_dir
    num_workers = args.workers
    num_epochs = args.epochs
    batch_size = args.batch_size
    learning_rate = args.learning_rate
    early_stopping = args.early_stopping
    model_name = args.model
    optimizer_name = args.optimizer
    image_size = (512, 512)
    fast = args.fast
    augmentations = args.augmentations
    train_mode = args.train_mode
    run_mode = args.run_mode
    log_dir = None
    fp16 = args.fp16
    freeze_encoder = args.freeze_encoder
    fold = args.fold

    run_train = run_mode == 'fit_predict' or run_mode == 'fit'
    run_predict = run_mode == 'fit_predict' or run_mode == 'predict'

    model = maybe_cuda(get_model(model_name, num_classes=len(CLASS_NAMES)))

    if args.transfer:
        transfer_checkpoint = fs.auto_file(args.transfer)
        print("Transfering weights from model checkpoint", transfer_checkpoint)
        checkpoint = load_checkpoint(transfer_checkpoint)
        pretrained_dict = checkpoint['model_state_dict']

        for name, value in pretrained_dict.items():
            try:
                model.load_state_dict(collections.OrderedDict([(name, value)]), strict=False)
            except Exception as e:
                print(e)

    checkpoint = None
    if args.checkpoint:
        checkpoint = load_checkpoint(fs.auto_file(args.checkpoint))
        unpack_checkpoint(checkpoint, model=model)

        checkpoint_epoch = checkpoint['epoch']
        print('Loaded model weights from:', args.checkpoint)
        print('Epoch                    :', checkpoint_epoch)
        print('Metrics (Train):',
              'cappa:', checkpoint['epoch_metrics']['train']['kappa_score'],
              'accuracy01:', checkpoint['epoch_metrics']['train']['accuracy01'],
              'loss:', checkpoint['epoch_metrics']['train']['loss'])
        print('Metrics (Valid):',
              'cappa:', checkpoint['epoch_metrics']['valid']['kappa_score'],
              'accuracy01:', checkpoint['epoch_metrics']['valid']['accuracy01'],
              'loss:', checkpoint['epoch_metrics']['valid']['loss'])

        log_dir = os.path.dirname(os.path.dirname(fs.auto_file(args.checkpoint)))

    if run_train:

        if freeze_encoder:
            set_trainable(model.encoder, trainable=False, freeze_bn=True)

        criterion = get_loss(args.criterion)
        parameters = get_optimizable_parameters(model)
        optimizer = get_optimizer(optimizer_name, parameters, learning_rate)

        if checkpoint is not None:
            try:
                unpack_checkpoint(checkpoint, optimizer=optimizer)
                print('Restored optimizer state from checkpoint')
            except Exception as e:
                print('Failed to restore optimizer state from checkpoint', e)

        train_loader, valid_loader = get_dataloaders(data_dir=data_dir,
                                                     batch_size=batch_size,
                                                     num_workers=num_workers,
                                                     image_size=image_size,
                                                     augmentation=augmentations,
                                                     fast=fast,
                                                     fold=fold)

        loaders = collections.OrderedDict()
        loaders["train"] = train_loader
        loaders["valid"] = valid_loader

        current_time = datetime.now().strftime('%b%d_%H_%M')
        prefix = f'classification/{args.model}/fold_{fold}/{current_time}_{args.criterion}'

        if fp16:
            prefix += '_fp16'

        if fast:
            prefix += '_fast'

        log_dir = os.path.join('runs', prefix)
        os.makedirs(log_dir, exist_ok=False)

        scheduler = MultiStepLR(optimizer,
                                milestones=[10, 30, 50, 70, 90], gamma=0.5)

        print('Train session    :', prefix)
        print('\tFP16 mode      :', fp16)
        print('\tFast mode      :', args.fast)
        print('\tTrain mode     :', train_mode)
        print('\tEpochs         :', num_epochs)
        print('\tEarly stopping :', early_stopping)
        print('\tWorkers        :', num_workers)
        print('\tData dir       :', data_dir)
        print('\tFold           :', fold)
        print('\tLog dir        :', log_dir)
        print('\tAugmentations  :', augmentations)
        print('\tTrain size     :', len(train_loader), len(train_loader.dataset))
        print('\tValid size     :', len(valid_loader), len(valid_loader.dataset))
        print('Model            :', model_name)
        print('\tParameters     :', count_parameters(model))
        print('\tImage size     :', image_size)
        print('\tFreeze encoder :', freeze_encoder)
        print('Optimizer        :', optimizer_name)
        print('\tLearning rate  :', learning_rate)
        print('\tBatch size     :', batch_size)
        print('\tCriterion      :', args.criterion)

        # model training
        visualization_fn = partial(draw_classification_predictions, class_names=CLASS_NAMES)

        callbacks = [
            AccuracyCallback(),
            CappaScoreCallback(),
            ConfusionMatrixCallback(class_names=CLASS_NAMES),
            # ShowPolarBatchesCallback(visualization_fn, metric='f1_score', minimize=False),
            ShowPolarBatchesCallback(visualization_fn, metric='accuracy01', minimize=False),
        ]

        if early_stopping:
            callbacks += [EarlyStoppingCallback(early_stopping, metric='kappa_score', minimize=False)]

        runner = SupervisedRunner(input_key='image')
        runner.train(
            fp16=fp16,
            model=model,
            criterion=criterion,
            optimizer=optimizer,
            scheduler=scheduler,
            callbacks=callbacks,
            loaders=loaders,
            logdir=log_dir,
            num_epochs=num_epochs,
            verbose=True,
            main_metric='kappa_score',
            minimize_metric=False,
            state_kwargs={"cmd_args": vars(args)}
        )

    if run_predict and not fast:
        # Training is finished. Let's run predictions using best checkpoint weights
        best_checkpoint = load_checkpoint(fs.auto_file('best.pth', where=log_dir))
        unpack_checkpoint(best_checkpoint, model=model)

        model.eval()
        torch.no_grad()

        train_csv = pd.read_csv(os.path.join(data_dir, 'test.csv'))
        train_csv['id_code'] = train_csv['id_code'].apply(lambda x: os.path.join(data_dir, 'test_images', f'{x}.png'))
        test_ds = RetinopathyDataset(train_csv['id_code'], None, get_test_aug(image_size, augmentations))
        test_dl = DataLoader(test_ds, batch_size)

        test_ids = []
        test_preds = []

        for batch in tqdm(test_dl):
            input = batch['image'].cuda()
            outputs = model(input)
            predictions = np.argmax(to_numpy(outputs['logits']), axis=1)

            test_ids.extend(batch['image_id'])
            test_preds.extend(predictions)

        df = pd.DataFrame.from_dict({'id_code': test_ids, 'diagnosis': test_preds})
        df.to_csv(os.path.join(log_dir, 'submission.csv'), index=None)


if __name__ == '__main__':
    main()
