import warnings
warnings.filterwarnings("ignore")

from data import *

from layers.modules import MultiBoxLoss
from utils.augmentations import *
from csd import build_ssd_con
import os
import sys
import time
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import torch.nn.init as init
import torch.utils.data as data
import numpy as np
import argparse
import math


def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


parser = argparse.ArgumentParser(
    description='Single Shot MultiBox Detector Training With Pytorch')
train_set = parser.add_mutually_exclusive_group()
parser.add_argument('--dataset', default='VOC300', choices=['VOC300', 'VOC512'],
                    type=str, help='VOC300 or VOC512')
parser.add_argument('--dataset_root', default=VOC_ROOT,
                    help='Dataset root directory path')
parser.add_argument('--basenet', default='vgg16_reducedfc.pth',
                    help='Pretrained base model')
parser.add_argument('--batch_size', default=32, type=int,
                    help='Batch size for training')
parser.add_argument('--resume', default=None, type=str,  # None  'weights/ssd300_COCO_80000.pth'
                    help='Checkpoint state_dict file to resume training from')
parser.add_argument('--num_workers', default=4, type=int,
                    help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True, type=str2bool,
                    help='Use CUDA to train model')
parser.add_argument('--seed', default=123, type=int,
                    help='random seed')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                    help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float,
                    help='Momentum value for optim')
parser.add_argument('--weight_decay', default=5e-4, type=float,
                    help='Weight decay for SGD')
parser.add_argument('--gamma', default=0.1, type=float,
                    help='Gamma update for SGD')
parser.add_argument('--save_folder', default='weights/',
                    help='Directory for saving checkpoint models')
parser.add_argument('--save_interval', default=2000, type=int,
                    help='Directory for saving checkpoint models')
parser.add_argument('--warmup', default=False, action='store_true',
                    help='Do Supervised Learning in first epoch')
parser.add_argument('--sup_aug_type', default='default', type=str,
                    help='default | autoaugment | gridmask')
parser.add_argument('--unsup_aug_type', default='default', type=str,
                    help='default | autoaugment | gridmask')

parser.add_argument('--debug', action='store_true')

args = parser.parse_args()

print(args)

if torch.cuda.is_available():
    if args.cuda:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    if not args.cuda:
        print("WARNING: It looks like you have a CUDA device, but aren't " +
              "using CUDA.\nRun with --cuda for optimal training speed.")
        torch.set_default_tensor_type('torch.FloatTensor')
else:
    torch.set_default_tensor_type('torch.FloatTensor')

if not os.path.exists(args.save_folder):
    os.mkdir(args.save_folder)

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    print("random seed is set as {}".format(seed))

def train():
    if args.dataset == 'COCO':
        if args.dataset_root == VOC_ROOT:
            if not os.path.exists(COCO_ROOT):
                parser.error('Must specify dataset_root if specifying dataset')
            print("WARNING: Using default COCO dataset_root because " +
                  "--dataset_root was not specified.")
            args.dataset_root = COCO_ROOT
        cfg = coco
    elif args.dataset == 'VOC300':
        #if args.dataset_root == COCO_ROOT:
        #    parser.error('Must specify dataset if specifying dataset_root')
        cfg = voc300
    elif args.dataset == 'VOC512':
        if args.dataset_root == COCO_ROOT:
            parser.error('Must specify dataset if specifying dataset_root')
        cfg = voc512

    if args.sup_aug_type == "default":
        img_transform_sup = SSDAugmentation(cfg['min_dim'], MEANS)
    elif (args.sup_aug_type).startswith('autoaugment'):
        from utils.Myautoaugment_utils import AutoAugmenter
        if args.sup_aug_type == 'autoaugment_v0':
            autoaugmenter = AutoAugmenter('v0')
        elif args.sup_aug_type == 'autoaugment_v1':
            autoaugmenter = AutoAugmenter('v1')
        elif args.sup_aug_type == 'autoaugment_v2':
            autoaugmenter = AutoAugmenter('v2')
        elif args.sup_aug_type == 'autoaugment_v3':
            autoaugmenter = AutoAugmenter('v3')
        elif args.sup_aug_type == 'autoaugment_v4':
            autoaugmenter = AutoAugmenter('v4')
        elif args.sup_aug_type == 'autoaugment_custom':
            autoaugmenter = AutoAugmenter('custom')
        else:
            raise ValueError("No such autoaugmenter version, please check.")
        img_transform_sup = Compose([ConvertFromInts(),
                                       ToAbsoluteCoords(),
                                       PhotometricDistort(),
                                       Expand(MEANS),
                                       RandomSampleCrop(),
                                       RandomMirror(),
                                       ToPercentCoords(),
                                       autoaugmenter,
                                       Resize(cfg['min_dim']),
                                       SubtractMeans(MEANS),
                                       ])
    elif args.sup_aug_type == "gridmask":
        from utils.MyGridMask import GridMask
        # default setting in https://github.com/Jia-Research-Lab/GridMask/blob/master/detection_grid/maskrcnn_benchmark/config/defaults.py
        GRID_ROTATE = 1
        GRID_OFFSET = 0
        GRID_RATIO = 0.5
        GRID_MODE = 1
        GRID_PROB = 0.5
        img_transform_sup = Compose([ConvertFromInts(),
                                      ToAbsoluteCoords(),
                                      PhotometricDistort(),
                                      Expand(MEANS),
                                      RandomSampleCrop(),
                                      RandomMirror(),
                                      ToPercentCoords(),
                                      GridMask(True, True, GRID_ROTATE, GRID_OFFSET, GRID_RATIO, GRID_MODE, GRID_PROB),
                                      Resize(cfg['min_dim']),
                                      SubtractMeans(MEANS),
                                      ])
    else:
        raise ValueError("args.sup_aug_type should be in [default | autoaugment | gridmask]")

    if args.unsup_aug_type == "default":
        img_transform_unsup = SSDAugmentation(cfg['min_dim'], MEANS)
    elif (args.unsup_aug_type).startswith('autoaugment'):
        from utils.Myautoaugment_utils import AutoAugmenter
        if args.unsup_aug_type == 'autoaugment_v0':
            autoaugmenter = AutoAugmenter('v0')
        elif args.unsup_aug_type == 'autoaugment_v1':
            autoaugmenter = AutoAugmenter('v1')
        elif args.unsup_aug_type == 'autoaugment_v2':
            autoaugmenter = AutoAugmenter('v2')
        elif args.unsup_aug_type == 'autoaugment_v3':
            autoaugmenter = AutoAugmenter('v3')
        elif args.unsup_aug_type == 'autoaugment_v4':
            autoaugmenter = AutoAugmenter('v4')
        elif args.unsup_aug_type == 'autoaugment_custom':
            autoaugmenter = AutoAugmenter('custom')
        else:
            raise ValueError("No such autoaugmenter version, please check.")
        img_transform_unsup = Compose([ConvertFromInts(),
                                       ToAbsoluteCoords(),
                                       PhotometricDistort(),
                                       Expand(MEANS),
                                       RandomSampleCrop(),
                                       RandomMirror(),
                                       ToPercentCoords(),
                                       autoaugmenter,
                                       Resize(cfg['min_dim']),
                                       SubtractMeans(MEANS),
                                       ])
    elif args.unsup_aug_type == "gridmask":
        from utils.MyGridMask import GridMask
        # default setting in https://github.com/Jia-Research-Lab/GridMask/blob/master/detection_grid/maskrcnn_benchmark/config/defaults.py
        GRID_ROTATE = 1
        GRID_OFFSET = 0
        GRID_RATIO = 0.5
        GRID_MODE = 1
        GRID_PROB = 0.5
        img_transform_unsup = Compose([ConvertFromInts(),
                                      ToAbsoluteCoords(),
                                      PhotometricDistort(),
                                      Expand(MEANS),
                                      RandomSampleCrop(),
                                      RandomMirror(),
                                      ToPercentCoords(),
                                      GridMask(True, True, GRID_ROTATE, GRID_OFFSET, GRID_RATIO, GRID_MODE, GRID_PROB),
                                      Resize(cfg['min_dim']),
                                      SubtractMeans(MEANS),
                                      ])
    else:
        raise ValueError("args.unsup_aug_type should be in [default | autoaugment | gridmask]")


    # while finish_flag:
    ssd_net = build_ssd_con('train', cfg['min_dim'], cfg['num_classes'])
    net = ssd_net

    if args.cuda:
        net = torch.nn.DataParallel(ssd_net)

    if args.resume:
        print('Resuming training, loading {}...'.format(args.resume))
        ssd_net.load_weights(args.resume)
    else:
        vgg_weights = torch.load(args.save_folder + args.basenet)
        print('Loading base network...')
        ssd_net.vgg.load_state_dict(vgg_weights)

    if args.cuda:
        net = net.cuda()

    if not args.resume:
        print('Initializing weights...')
        # initialize newly added layers' weights with xavier method
        ssd_net.extras.apply(weights_init)
        ssd_net.loc.apply(weights_init)
        ssd_net.conf.apply(weights_init)

    setup_seed(args.seed)

    optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=args.momentum,
                          weight_decay=args.weight_decay)
    criterion = MultiBoxLoss(cfg['num_classes'], 0.5, True, 0, True, 3, 0.5,
                             False, args.cuda)
    conf_consistency_criterion = torch.nn.KLDivLoss(size_average=False, reduce=False).cuda()


    net.train()
    # loss counters
    print('Loading the dataset...')

    step_index = 0

    supervised_batch = args.batch_size

    supervised_dataset = VOCDetection(root=args.dataset_root, image_sets=[('2007', 'trainval'), ('2012', 'trainval')], transform=img_transform_sup)

    supervised_data_loader = data.DataLoader(supervised_dataset, supervised_batch,
                                             num_workers=args.num_workers,
                                             shuffle=True, collate_fn=detection_collate,
                                             pin_memory=True, drop_last=True)

    batch_iterator = iter(supervised_data_loader)

    for iteration in range(cfg['max_iter'] // (args.batch_size // 32)):

        if iteration * (args.batch_size // 32) in cfg['lr_steps']:
            step_index += 1
            adjust_learning_rate(optimizer, args.gamma, step_index)

        if args.resume and iteration < int((args.resume).split('_')[-1].split('.')[0]):
            continue

        try:
            images, targets = next(batch_iterator)
        except StopIteration:
            supervised_dataset = VOCDetection(root=args.dataset_root, image_sets=[('2007', 'trainval'), ('2012', 'trainval')], transform=img_transform_sup)
            supervised_data_loader = data.DataLoader(supervised_dataset, supervised_batch,
                                                     num_workers=args.num_workers,
                                                     shuffle=True, collate_fn=detection_collate,
                                                     pin_memory=True, drop_last=True)
            batch_iterator = iter(supervised_data_loader)
            images, targets = next(batch_iterator)

        if args.cuda:
            images = images.cuda()
            with torch.no_grad():
                targets = [ann.cuda() for ann in targets]
        else:
            with torch.no_grad():
                targets = [ann for ann in targets]

        # forward
        t0 = time.time()

        output, conf, conf_flip, loc, loc_flip = net(images)

        # backprop
        loss_l, loss_c = criterion(output, targets)

        conf_class = conf[:,:,1:].clone()
        background_score = conf[:, :, 0].clone()
        each_val, each_index = torch.max(conf_class, dim=2)
        mask_val = each_val > background_score
        mask_val = mask_val.data

        mask_conf_index = mask_val.unsqueeze(2).expand_as(conf)
        mask_loc_index = mask_val.unsqueeze(2).expand_as(loc)

        conf_mask_sample = conf.clone()
        loc_mask_sample = loc.clone()
        conf_sampled = conf_mask_sample[mask_conf_index].view(-1, 21)
        loc_sampled = loc_mask_sample[mask_loc_index].view(-1, 4)

        conf_mask_sample_flip = conf_flip.clone()
        loc_mask_sample_flip = loc_flip.clone()
        conf_sampled_flip = conf_mask_sample_flip[mask_conf_index].view(-1, 21)
        loc_sampled_flip = loc_mask_sample_flip[mask_loc_index].view(-1, 4)

        if mask_val.sum() > 0:
            ## JSD !!!!!1
            conf_sampled_flip = conf_sampled_flip + 1e-7
            conf_sampled = conf_sampled + 1e-7
            consistency_conf_loss_a = conf_consistency_criterion(conf_sampled.log(), conf_sampled_flip.detach()).sum(-1).mean()
            consistency_conf_loss_b = conf_consistency_criterion(conf_sampled_flip.log(), conf_sampled.detach()).sum(-1).mean()
            consistency_conf_loss = consistency_conf_loss_a + consistency_conf_loss_b

            ## LOC LOSS
            consistency_loc_loss_x = torch.mean(torch.pow(loc_sampled[:, 0] + loc_sampled_flip[:, 0], exponent=2))
            consistency_loc_loss_y = torch.mean(torch.pow(loc_sampled[:, 1] - loc_sampled_flip[:, 1], exponent=2))
            consistency_loc_loss_w = torch.mean(torch.pow(loc_sampled[:, 2] - loc_sampled_flip[:, 2], exponent=2))
            consistency_loc_loss_h = torch.mean(torch.pow(loc_sampled[:, 3] - loc_sampled_flip[:, 3], exponent=2))

            consistency_loc_loss = torch.div(consistency_loc_loss_x + consistency_loc_loss_y + consistency_loc_loss_w + consistency_loc_loss_h, 4)
        else:
            consistency_conf_loss = torch.cuda.FloatTensor([0])
            consistency_loc_loss = torch.cuda.FloatTensor([0])
            if not args.cuda:
                consistency_conf_loss = consistency_conf_loss.cpu()
                consistency_loc_loss = consistency_loc_loss.cpu()

        consistency_loss = torch.div(consistency_conf_loss,2) + consistency_loc_loss

        ramp_weight = rampweight(iteration)
        consistency_loss = torch.mul(consistency_loss, ramp_weight)

        loss = loss_l + loss_c + consistency_loss

        if float(loss.item()) > 0:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        t1 = time.time()                

        if iteration % 200 == 0:
            print('timer: %.4f sec.' % (t1 - t0))
            print('iter ' + repr(iteration) + ' || Loss: %.4f || consistency_loss : %.4f ||' % (loss.item(), consistency_loss.item()), end=' ')
            print('loss: %.4f , loss_c: %.4f , loss_l: %.4f , loss_con: %.4f, lr : %.4f\n' % (loss.item(), loss_c.item(), loss_l.item(), consistency_loss.item(), float(optimizer.param_groups[0]['lr'])))

        if float(loss.item()) > 10000:
            # raise ValueError("Whoa! loss.item() is larger than 100, something must be wrong!")
            break

        if iteration != 0 and (iteration+1) % args.save_interval == 0:
            print('Saving state, iter:', iteration)
            if not os.path.exists(os.path.join(args.save_folder, "{}+{}".format(args.sup_aug_type, args.unsup_aug_type))):
                os.makedirs(os.path.join(args.save_folder, "{}+{}".format(args.sup_aug_type, args.unsup_aug_type)))
            torch.save(ssd_net.state_dict(), os.path.join(args.save_folder, "{}+{}".format(args.sup_aug_type, args.unsup_aug_type), 'ssd300_csd_sup_0712_' + repr(iteration+1) + '.pth'))

    print('-------------------------------\n')
    print(loss.item())
    print('-------------------------------')
    

def rampweight(iteration):
    ramp_up_end = 32000 // (args.batch_size // 32)
    ramp_down_start = 100000 // (args.batch_size // 32)

    if iteration < ramp_up_end:
        ramp_weight = math.exp(-5 * math.pow((1 - iteration / ramp_up_end), 2))
    elif iteration > ramp_down_start:
        ramp_weight = math.exp(-12.5 * math.pow((1 - (120000 // (args.batch_size // 32) - iteration) / (20000 // (args.batch_size // 32))), 2)) 
    else:
        ramp_weight = 1 


    if iteration == 0:
        ramp_weight = 0

    return ramp_weight


def adjust_learning_rate(optimizer, gamma, step):
    """Sets the learning rate to the initial LR decayed by 10 at every
        specified step
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    lr = args.lr * (gamma ** (step))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def xavier(param):
    init.xavier_uniform_(param)


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        xavier(m.weight.data)
        m.bias.data.zero_()


if __name__ == '__main__':
    train()
