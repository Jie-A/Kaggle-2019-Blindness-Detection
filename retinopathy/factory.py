from functools import partial

import torch.nn.functional as F
from catalyst.contrib.schedulers import OneCycleLR, ExponentialLR
from pytorch_toolbelt.losses import FocalLoss
from pytorch_toolbelt.modules.encoders import *
from torch import nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss
from torch.optim import SGD, Adam
from torch.optim.lr_scheduler import MultiStepLR
from torch.optim.rmsprop import RMSprop
from torchvision.models import densenet169, densenet121, densenet201

from retinopathy.losses import ClippedMSELoss, ClippedWingLoss, CumulativeLinkLoss, LabelSmoothingLoss, \
    SoftCrossEntropyLoss, ClippedHuber, CustomMSE, HybridCappaLoss
from retinopathy.models.heads import GlobalAvgPool2dHead, GlobalMaxPool2dHead, \
    ObjectContextPoolHead, \
    GlobalMaxAvgPool2dHead, EncoderHeadModel, RMSPoolHead, MultistageModel
from retinopathy.models.inceptionv4 import InceptionV4Encoder


class DenseNet121Encoder(EncoderModule):
    def __init__(self, pretrained=True):
        densenet = densenet121(pretrained=pretrained)
        super().__init__([1024], [32], [0])
        self.features = densenet.features

    def forward(self, x):
        x = self.features(x)
        x = F.relu(x, inplace=True)
        return [x]


class DenseNet169Encoder(EncoderModule):
    def __init__(self, pretrained=True):
        densenet = densenet169(pretrained=pretrained)
        super().__init__([1664], [32], [0])
        self.features = densenet.features

    def forward(self, x):
        x = self.features(x)
        x = F.relu(x, inplace=True)
        return [x]


class DenseNet201Encoder(EncoderModule):
    def __init__(self, pretrained=True):
        densenet = densenet201(pretrained=pretrained)
        super().__init__([1920], [32], [0])
        self.features = densenet.features

    def forward(self, x):
        x = self.features(x)
        x = F.relu(x, inplace=True)
        return [x]


def get_model(model_name, num_classes, pretrained=True, dropout=0.0, **kwargs):
    kind, encoder_name, head_name = model_name.split('_')

    ENCODERS = {
        'resnet18': Resnet18Encoder,
        'resnet34': Resnet34Encoder,
        'resnet50': Resnet50Encoder,
        'resnet101': Resnet101Encoder,
        'resnet152': Resnet152Encoder,
        'seresnext50': SEResNeXt50Encoder,
        'seresnext101': SEResNeXt101Encoder,
        'seresnet152': SEResnet152Encoder,
        'densenet121': DenseNet121Encoder,
        'densenet169': DenseNet169Encoder,
        'densenet201': DenseNet201Encoder,
        'inceptionv4': InceptionV4Encoder
    }

    encoder = ENCODERS[encoder_name](pretrained=pretrained)

    POOLING = {
        'gap': GlobalAvgPool2dHead,
        'avg': GlobalAvgPool2dHead,
        'gmp': GlobalMaxPool2dHead,
        'max': GlobalMaxPool2dHead,
        'ocp': partial(ObjectContextPoolHead, oc_features=encoder.output_filters[-1] // 4),
        'rms': RMSPoolHead,
        'maxavg': GlobalMaxAvgPool2dHead,
    }

    MODELS = {
        'reg': partial(EncoderHeadModel, num_classes=num_classes, dropout=dropout),
        'cls': partial(EncoderHeadModel, num_classes=num_classes, dropout=dropout),
        'ord': partial(EncoderHeadModel, num_classes=num_classes, dropout=dropout),
        'mul': partial(MultistageModel, num_classes=num_classes, dropout=dropout)
    }

    head = POOLING[head_name](encoder.output_filters)
    model = MODELS[kind](encoder, head)
    return model


def get_optimizable_parameters(model: nn.Module):
    return filter(lambda x: x.requires_grad, model.parameters())


def get_optimizer(optimizer_name: str, parameters, learning_rate: float, weight_decay=1e-5, **kwargs):
    if optimizer_name.lower() == 'sgd':
        return SGD(parameters, learning_rate, momentum=0.9, nesterov=True, weight_decay=weight_decay, **kwargs)

    if optimizer_name.lower() == 'adam':
        return Adam(parameters, learning_rate, weight_decay=weight_decay,
                    eps=1e-3,  # As Jeremy suggests
                    **kwargs)

    if optimizer_name.lower() == 'rms':
        return RMSprop(parameters, learning_rate, weight_decay=weight_decay, **kwargs)

    raise ValueError("Unsupported optimizer name " + optimizer_name)


def get_loss(loss_name: str, **kwargs):
    if loss_name.lower() == 'bce':
        return BCEWithLogitsLoss(**kwargs)

    if loss_name.lower() == 'ce':
        return CrossEntropyLoss(**kwargs)

    if loss_name.lower() == 'focal':
        return FocalLoss(**kwargs)

    if loss_name.lower() == 'mse':
        return CustomMSE(**kwargs)

    if loss_name.lower() == 'huber':
        return ClippedHuber(min=0, max=4, **kwargs)

    if loss_name.lower() == 'wing_loss':
        return ClippedWingLoss(width=2, curvature=0.1, min=0, max=4, **kwargs)

    if loss_name.lower() == 'clipped_huber':
        raise NotImplementedError(loss_name)

    if loss_name.lower() == 'clipped_mse':
        return ClippedMSELoss(min=0, max=4, **kwargs)

    if loss_name.lower() == 'link':
        return CumulativeLinkLoss()

    if loss_name.lower() == 'smooth_kl':
        return LabelSmoothingLoss()

    if loss_name.lower() == 'soft_ce':
        return SoftCrossEntropyLoss(**kwargs)

    if loss_name.lower() == 'hybrid_kappa':
        return HybridCappaLoss(**kwargs)

    raise KeyError(loss_name)


def get_scheduler(scheduler_name: str,
                  optimizer,
                  lr,
                  num_epochs,
                  batches_in_epoch=None):
    if scheduler_name is None or scheduler_name.lower() == 'none':
        return None

    if scheduler_name.lower() in {'1cycle', 'one_cycle'}:
        return OneCycleLR(optimizer,
                          lr_range=(lr, 1e-6, 1e-5),
                          num_steps=batches_in_epoch,
                          warmup_fraction=0.05, decay_fraction=0.1)

    if scheduler_name.lower() == 'exp':
        return ExponentialLR(optimizer, gamma=0.95)

    if scheduler_name.lower() == 'multistep':
        return MultiStepLR(optimizer,
                           milestones=[
                               int(num_epochs * 0.3),
                               int(num_epochs * 0.5),
                               int(num_epochs * 0.7),
                               int(num_epochs * 0.9)],
                           gamma=0.5)

    raise KeyError(scheduler_name)