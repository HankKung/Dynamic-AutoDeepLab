import argparse
import os
import numpy as np
from tqdm import tqdm
from mypath import Path
from dataloaders import make_data_loader
from modeling.sync_batchnorm.replicate import patch_replication_callback
from utils.loss import SegmentationLosses
from utils.calculate_weights import calculate_weigths_labels
from utils.lr_scheduler import LR_Scheduler
from utils.saver import Saver
from utils.summaries import TensorboardSummary
from utils.metrics import Evaluator
from modeling.new_model import *
from torchviz import make_dot, make_dot_from_trace

APEX_AVAILABLE = False
torch.backends.cudnn.benchmark = True

class trainNew(object):
    def __init__(self, args):
        self.args = args

        # Define Saver
        self.saver = Saver(args)
        self.saver.save_experiment_config()
        # Define Tensorboard Summary
        self.summary = TensorboardSummary(self.saver.experiment_dir)
        self.writer = self.summary.create_summary()
        self.use_amp = False

        # Define Dataloader
        kwargs = {'num_workers': args.workers, 'pin_memory': True, 'drop_last': True}
        self.train_loader, self.val_loader, self.test_loader, self.nclass = make_data_loader(args, **kwargs)

        cell_path_1 = os.path.join(args.saved_arch_path, 'genotype_device.npy')
        cell_path_2 = os.path.join(args.saved_arch_path, 'genotype_cloud.npy')
        network_path_space = os.path.join(args.saved_arch_path, 'network_path_space.npy')

        new_cell_arch_1 = np.load(cell_path_1)
        new_cell_arch_2 = np.load(cell_path_2)
 
        new_network_arch = np.load(network_path_space)
        
        if args.network == 'dist':
#            new_network_arch = [0, 1, 2, 3, 2, 2, 1, 0, 1, 2, 3, 2]
#            new_network_arch = [1, 0, 0, 1, 2, 3, 2, 3, 3, 2, 2, 3]
#            new_network_arch = [1, 2, 3, 2, 3, 2, 1, 2, 1, 2, 1, 2]
            new_network_arch = [0, 1, 2, 2, 3, 2, 2, 1, 2, 1, 1, 2]
            args.B_1=4
            low_level_layer = 1

        elif args.network == 'autodeeplab':
            new_network_arch = [0, 0, 0, 1, 2, 1, 2, 2, 3, 3, 2, 1]
            cell = np.zeros((10, 2))
            cell[0] = [0, 7]
            cell[1] = [1, 4]
            cell[2] = [2, 4]
            cell[3] = [3, 6]
            cell[4] = [5, 4]
            cell[5] = [8, 4]
            cell[6] = [11, 5]
            cell[7] = [13, 5]
            cell[8] = [19, 7]
            cell[9] = [18, 5]
            cell=np.int_(cell)
            new_cell_arch_1 = cell
            new_cell_arch_2 = cell      
            args.B_1=5
            low_level_layer = 2

        # Define network
        model = new_cloud_Model(new_network_arch,
                                new_cell_arch_1,
                                new_cell_arch_2,
                                self.nclass,
                                args,
                                low_level_layer)

        train_params = [{'params': model.get_1x_lr_params(), 'lr': args.lr},
                        {'params': model.get_10x_lr_params(), 'lr': args.lr * 10}]

        # Define Optimizer
        optimizer = torch.optim.SGD(train_params, momentum=args.momentum,
                                    weight_decay=args.weight_decay, nesterov=args.nesterov)

        # Define Criterion
        # whether to use class balanced weights
        if args.use_balanced_weights:
            classes_weights_path = os.path.join(Path.db_root_dir(args.dataset), args.dataset + '_classes_weights.npy')
            if os.path.isfile(classes_weights_path):
                weight = np.load(classes_weights_path)
            else:
                weight = calculate_weigths_labels(args.dataset, self.train_loader, self.nclass)
            weight = torch.from_numpy(weight.astype(np.float32))
        else:
            weight = None
        self.criterion = SegmentationLosses(weight=weight, cuda=args.cuda).build_loss(mode=args.loss_type)
        self.model, self.optimizer = model, optimizer

        # Define Evaluator
        self.evaluator_1 = Evaluator(self.nclass)
        self.evaluator_2 = Evaluator(self.nclass)
        # Define lr scheduler
        self.scheduler = LR_Scheduler(args.lr_scheduler, args.lr,
                                      args.epochs, len(self.train_loader)) #TODO: use min_lr ?

        # Using cuda
        if args.cuda:
            if args.sync_bn:
                self.model = torch.nn.DataParallel(self.model, device_ids=self.args.gpu_ids)
                patch_replication_callback(self.model)
            self.model = self.model.cuda()

        # Resuming checkpoint
        self.best_pred = 0.0
        if args.resume is not None:
            if not os.path.isfile(args.resume):
                raise RuntimeError("=> no checkpoint found at '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']

            # if the weights are wrapped in module object we have to clean it
            if args.clean_module:
                self.model.load_state_dict(checkpoint['state_dict'])
                state_dict = checkpoint['state_dict']
                new_state_dict = OrderedDict()
                for k, v in state_dict.items():
                    name = k[7:]  # remove 'module.' of dataparallel
                    new_state_dict[name] = v
                self.model.load_state_dict(new_state_dict)

            else:
                if (torch.cuda.device_count() > 1 or args.load_parallel):
                    self.model.module.load_state_dict(checkpoint['state_dict'])
                else:
                    self.model.load_state_dict(checkpoint['state_dict'])

            if not args.ft:
                self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.best_pred = checkpoint['best_pred']
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))

        # Clear start epoch if fine-tuning
        if args.ft:
            args.start_epoch = 0

    def training(self, epoch):
        train_loss = 0.0
        self.model.train()
        tbar = tqdm(self.train_loader)
        num_img_tr = len(self.train_loader)
        for i, sample in enumerate(tbar):
            image, target = sample['image'], sample['label']
            if self.args.cuda:
                image, target = image.cuda(non_blocking=True), target.cuda(non_blocking=True)
            self.scheduler(self.optimizer, i, epoch, self.best_pred)
            self.optimizer.zero_grad()
            output_1, output_2 = self.model(image)
            
            loss_1 = self.criterion(output_1, target)
            loss_2 = self.criterion(output_2, target)
            loss = (loss_1 + loss_2)/2
            loss.backward()

            self.optimizer.step()
            train_loss += loss.item()
            if i % 50 == 0:
                tbar.set_description('Train loss: %.3f' % (train_loss / (i + 1)))
            # if i %50 == 0:
            #     self.writer.add_scalar('train/total_loss_iter', loss.item(), i + num_img_tr * epoch)

            del loss_1
            del loss_2
            del loss
            del output_1
            del output_2
        self.writer.add_scalar('train/total_loss_epoch', train_loss, epoch)
        print('[Epoch: %d, numImages: %5d]' % (epoch, i * self.args.batch_size + image.data.shape[0]))
        print('Loss: %.3f' % train_loss)

    def validation(self, epoch):
        self.model.eval()
        self.evaluator_1.reset()
        self.evaluator_2.reset()
        tbar = tqdm(self.val_loader, desc='\r')
        test_loss = 0.0
        for i, sample in enumerate(tbar):
            image, target = sample['image'], sample['label']
            if self.args.cuda:
                image, target = image.cuda(), target.cuda()

            with torch.no_grad():
                output_1, output_2 = self.model(image)
            loss_1 = self.criterion(output_1, target)
            loss_2 = self.criterion(output_2, target)
            loss = (loss_1 + loss_2) /2
            test_loss += loss.item()
            tbar.set_description('Test loss: %.3f' % (test_loss / (i + 1)))

            target_show = target
            pred_1 = torch.argmax(output_1, axis=1)
            pred_2 = torch.argmax(output_2, axis=1)
            # Add batch sample into evaluator
            self.evaluator_1.add_batch(target, pred_1)
            self.evaluator_2.add_batch(target, pred_2)
            if epoch//100 == i:
                global_step = epoch
                self.summary.visualize_image(self.writer, self.args.dataset, image, target_show, output_2, global_step)

        mIoU_1 = self.evaluator_1.Mean_Intersection_over_Union()
        mIoU_2 = self.evaluator_2.Mean_Intersection_over_Union()

        self.writer.add_scalar('val/total_loss_epoch', test_loss, epoch)
        self.writer.add_scalar('val/classifier_1/mIoU', mIoU_1, epoch)
        self.writer.add_scalar('val/classifier_2/mIoU', mIoU_2, epoch)

        print('Validation:')
        print('[Epoch: %d, numImages: %5d]' % (epoch, i * self.args.test_batch_size + image.data.shape[0]))
        print("classifier_1_mIoU:{}, classifier_2_mIoU: {}".format(mIoU_1, mIoU_2))
        print('Loss: %.3f' % test_loss)

        new_pred = (mIoU_1 + mIoU_2)/2
        if new_pred > self.best_pred:
            is_best = True
            self.best_pred = new_pred
            self.saver.save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': self.model.state_dict(),
                'optimizer': self.optimizer.state_dict(),
                'best_pred': self.best_pred,
            }, is_best)

def main():
    parser = argparse.ArgumentParser(description="PyTorch DeeplabV3Plus Training")

    ## model setting
    parser.add_argument('--network', type=str, default='dist',
                        choices=['dist', 'autodeeplab'],
                        help='backbone name (default: resnet)')
    parser.add_argument('--backbone', type=str, default='resnet',
                        choices=['resnet', 'xception', 'drn', 'mobilenet', 'autodeeplab'],
                        help='backbone name (default: resnet)')
    parser.add_argument('--num_model_1_layers', type=int, default=6)
    parser.add_argument('--F_2', type=int, default=20)
    parser.add_argument('--F_1', type=int, default=20)
    parser.add_argument('--B_2', type=int, default=5)
    parser.add_argument('--B_1', type=int, default=5)


    ## dataset config
    parser.add_argument('--dataset', type=str, default='cityscapes',
                        choices=['pascal', 'coco', 'cityscapes'],
                        help='dataset name (default: pascal)')
    parser.add_argument('--workers', type=int, default=4,
                        metavar='N', help='dataloader threads')


    # training config
    parser.add_argument('--autodeeplab', type=str, default='train',
                        choices=['search', 'train'])
    parser.add_argument('--sync-bn', type=bool, default=None,
                        help='whether to use sync bn (default: auto)')
    parser.add_argument('--freeze-bn', type=bool, default=False,
                        help='whether to freeze bn parameters (default: False)')
    parser.add_argument('--loss-type', type=str, default='ce',
                        choices=['ce', 'focal'],
                        help='loss func type (default: ce)')
    parser.add_argument('--epochs', type=int, default=None, metavar='N',
                        help='number of epochs to train (default: auto)')
    parser.add_argument('--start_epoch', type=int, default=0,
                        metavar='N', help='start epochs (default:0)')
    parser.add_argument('--batch-size', type=int, default=None,
                        metavar='N', help='input batch size for \
                                training (default: auto)')
    parser.add_argument('--test-batch-size', type=int, default=None,
                        metavar='N', help='input batch size for \
                                testing (default: auto)')
    parser.add_argument('--use-balanced-weights', action='store_true', default=False,
                        help='whether to use balanced weights (default: False)')


    # optimizer params
    parser.add_argument('--lr', type=float, default=None, metavar='LR',
                        help='learning rate (default: auto)')
    parser.add_argument('--min_lr', type=float, default=0.000001) #TODO: CHECK THAT THEY EVEN DO THIS FOR THE MODEL IN THE PAPER
    parser.add_argument('--lr-scheduler', type=str, default='poly',
                        choices=['poly', 'step', 'cos'],
                        help='lr scheduler mode: (default: poly)')
    parser.add_argument('--momentum', type=float, default=0.9,
                        metavar='M', help='momentum (default: 0.9)')
    parser.add_argument('--clean-module', type=int, default=0)
    parser.add_argument('--weight-decay', type=float, default=4e-5,
                        metavar='M', help='w-decay (default: 1e-4)')
    parser.add_argument('--nesterov', action='store_true', default=False,
                        help='whether use nesterov (default: False)')


    # cuda, seed and logging
    parser.add_argument('--no-cuda', action='store_true', default=
                        False, help='disables CUDA training')
    parser.add_argument('--gpu-ids', type=str, default='0',
                        help='use which gpu to train, must be a \
                        comma-separated list of integers only (default=0)')

    parser.add_argument('--seed', type=int, default=2, metavar='S',
                        help='random seed (default: 1)')


    # checking point
    parser.add_argument('--resume', type=str, default=None,
                        help='put the path to resuming file if needed')
    parser.add_argument('--saved-arch-path', type=str, default=None,
                        help='put the path to alphas and betas')
    parser.add_argument('--checkname', type=str, default=None,
                        help='set the checkpoint name')


    # finetuning pre-trained models
    parser.add_argument('--ft', action='store_true', default=False,
                        help='finetuning on a different dataset')


    # evaluation option
    parser.add_argument('--eval-interval', type=int, default=100,
                        help='evaluuation interval (default: 1)')
    
    args = parser.parse_args()
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    if args.cuda:
        try:
            args.gpu_ids = [int(s) for s in args.gpu_ids.split(',')]
        except ValueError:
            raise ValueError('Argument --gpu_ids must be a comma-separated list of integers only')

    if args.sync_bn is None:
        if args.cuda and len(args.gpu_ids) > 1:
            args.sync_bn = True
        else:
            args.sync_bn = False

    if args.test_batch_size is None:
        args.test_batch_size = 1

    if args.checkname is None:
        args.checkname = 'deeplab-'+str(args.backbone)
        
    print(args)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    new_trainer = trainNew(args)
    print('Starting Epoch:', new_trainer.args.start_epoch)
    print('Total Epoches:', new_trainer.args.epochs)
    for epoch in range(new_trainer.args.start_epoch, new_trainer.args.epochs):
        new_trainer.training(epoch)
        if epoch==0 or epoch % args.eval_interval == (args.eval_interval - 1) or epoch > new_trainer.args.epochs - 50:
            new_trainer.validation(epoch)
    new_trainer.writer.close()

if __name__ == "__main__":
   main()