from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from . import resnet, resnext
# from lib.nn import SynchronizedBatchNorm2d, PrRoIPool2D
from torch.nn import SyncBatchNorm as SynchronizedBatchNorm2d
# from lib.nn import PrRoIPool2D

from broden_dataset_utils.joint_dataset import broden_dataset


class SegmentationModuleBase(nn.Module):
    def __init__(self):
        super(SegmentationModuleBase, self).__init__()

    @staticmethod
    def pixel_acc(pred: torch.Tensor, label: torch.Tensor, 
                  ignore_index: int=-1,
                  samples_valid_mask: torch.tensor = None,
                  missing_score: float = 1.0) -> float:
        r'''Getting the total pixel accuracy.

            Args:
                pred (torch.Tensor): The propabilities mask from the model.
                label (torch.Tensor): The Ground Truth Label 
                ignore_index (int): The category index to ignore while
                    calculating the overall accuracy.
                samples_valid_mask (torch.tensor): The batch valid mask per
                    sample.
                missing_score (float): The score to replace if the total count
                of valid pixels are zero.
        '''
        # Getting Prediction labels
        _, preds = torch.max(pred, dim=1)
        # Getting the valid mask
        valid = (label != ignore_index).long()
        # Adding the batch validation mask to the calculations
        if samples_valid_mask is not None:
            pred_dim = len(preds.shape)
            samples_valid_mask_view_shape = [samples_valid_mask.shape[0]] + \
                [1 for i in range(pred_dim - 1)]
            samples_valid_mask_reshaped = \
                samples_valid_mask.view(samples_valid_mask_view_shape)
            # Multiplying by valid to apply the 
            valid *= samples_valid_mask_reshaped
        # Getting the count of accurcate pixels.
        acc_sum = torch.sum(valid * (preds == label).long())
        # Getting the total valid pixels sum.
        pixel_sum = torch.sum(valid)
        if pixel_sum == 0:
            return missing_score
        # Calculating the total accuracy.
        acc = acc_sum.float() / (pixel_sum.float() + 1e-10)
        return acc

    @staticmethod
    def get_correct_and_valid_pixels(pred: torch.Tensor, label: torch.Tensor, 
                                     ignore_index: int=-1,
                                     samples_valid_mask: torch.tensor = None):
        r'''Gets the correct and total valid pixels to be used in accomulated
            pixels calculations.

            Args:
                pred (torch.Tensor): The propabilities mask from the model.
                label (torch.Tensor): The Ground Truth Label 
                ignore_index (int): The category index to ignore while
                    calculating the overall accuracy.
                samples_valid_mask (torch.tensor): The batch valid mask per
                    sample.
        '''
        # Getting Prediction labels
        _, preds = torch.max(pred, dim=1)
        # Getting the valid mask
        valid = (label != ignore_index).long()
        # Adding the batch validation mask to the calculations
        if samples_valid_mask is not None:
            pred_dim = len(preds.shape)
            samples_valid_mask_view_shape = [samples_valid_mask.shape[0]] + \
                [1 for i in range(pred_dim - 1)]
            samples_valid_mask_reshaped = \
                samples_valid_mask.view(samples_valid_mask_view_shape)
            # Multiplying by valid to apply the 
            valid *= samples_valid_mask_reshaped
        # Getting the count of accurcate pixels.
        acc_sum = torch.sum(valid * (preds == label).long()).long()
        # Getting the total valid pixels sum.
        pixel_sum = torch.sum(valid).long()
        return acc_sum, pixel_sum

    @staticmethod
    def part_pixel_acc(pred_part, gt_seg_part, gt_seg_object, object_label, valid):
        # Object mask
        mask_object = (gt_seg_object == object_label)
        # Getting part prediction
        _, pred = torch.max(pred_part, dim=1)
        # Getting Accurate valid pixels mask
        acc_sum = mask_object * (pred == gt_seg_part)
        acc_sum = torch.sum(acc_sum.view(acc_sum.size(0), -1), dim=1)
        acc_sum = torch.sum(acc_sum * valid)
        pixel_sum = torch.sum(mask_object.view(mask_object.size(0), -1), dim=1)
        pixel_sum = torch.sum(pixel_sum * valid)
        return acc_sum, pixel_sum 

    @staticmethod
    def part_loss(pred_part, gt_seg_part, gt_seg_object, object_label, valid):
        mask_object = (gt_seg_object == object_label)
        # loss = F.nll_loss(pred_part, gt_seg_part * mask_object.long(), reduction='none')
        # Modified by Remon
        loss = F.cross_entropy(pred_part, gt_seg_part * mask_object.long(),
            reduction='none')  
        loss = loss * mask_object.float()
        loss = torch.sum(loss.view(loss.size(0), -1), dim=1)
        nr_pixel = torch.sum(mask_object.view(mask_object.shape[0], -1), dim=1)
        sum_pixel = (nr_pixel * valid).sum()
        loss = (loss * valid.float()).sum() / torch.clamp(sum_pixel, 1).float()
        return loss

class CrossEntropyLossWithValidationMask(nn.CrossEntropyLoss):
    r'''This class has been added by Remon to account for the validation mask
        when calculating the cross entropy loss.

        Args:
            ignore_index: The index to ignore.
    '''
    def __init__(self, ignore_index):
        super().__init__(ignore_index=ignore_index, reduction='none')

    def forward(self, input: torch.Tensor, target: torch.Tensor, 
                validation_mask: torch.Tensor):
        target_dim = len(target.shape)
        validation_mask_view_shape = [validation_mask.shape[0]] + \
            [1 for i in range(target_dim - 1)]
        validation_mask_reshaped = \
            validation_mask.view(validation_mask_view_shape)
        loss_unreduced = super().forward(input, target)
        loss_masked = validation_mask_reshaped * loss_unreduced
        mean_loss = torch.mean(loss_masked)
        return mean_loss

class SegmentationModule(SegmentationModuleBase):
    def __init__(self,
                 net_enc,
                 net_dec,
                 loss_scale=None,
                 test_output_switch = {"object": True,
                                       "part": True,
                                       "scene": True,
                                       "material": True},
                 disable_metric_and_loss_calculations = False):
        super(SegmentationModule, self).__init__()
        self.test_output_switch = test_output_switch
        self.encoder = net_enc
        self.decoder = net_dec
        self.disable_metric_and_loss_calculations = disable_metric_and_loss_calculations
        self.crit_dict = nn.ModuleDict()
        if loss_scale is None:
            self.loss_scale = {"object": 1, "part": 0.5, "scene": 0.25, "material": 1}
        else:
            self.loss_scale = loss_scale

        # criterion
        # self.crit_dict["object"] = nn.NLLLoss(ignore_index=0)  # ignore background 0
        # self.crit_dict["material"] = nn.NLLLoss(ignore_index=0)  # ignore background 0
        # self.crit_dict["scene"] = nn.NLLLoss(ignore_index=-1)  # ignore unlabelled -1
        
        # Code modified by Remon
        # self.crit_dict["object"] = nn.CrossEntropyLoss(ignore_index=0)  # ignore background 0
        # self.crit_dict["material"] = nn.CrossEntropyLoss(ignore_index=0)  # ignore background 0
        self.crit_dict["scene"] = nn.CrossEntropyLoss(ignore_index=-1)
        self.crit_dict["object"] = CrossEntropyLossWithValidationMask(ignore_index=0)  # ignore background 0
        self.crit_dict["material"] = CrossEntropyLossWithValidationMask(ignore_index=0)  # ignore background 0

    def forward(self, feed_dict, *, seg_size=None):
        if seg_size is None: # training

            # if feed_dict['source_idx'] == 0:
            #     output_switch = {"object": True, "part": True, "scene": True, "material": False}
            # elif feed_dict['source_idx'] == 1:
            #     output_switch = {"object": False, "part": False, "scene": False, "material": True}
            # else:
            #     raise ValueError

            # Code added by Remon
            output_switch = {"object": True,
                             "part": True,
                             "scene": True,
                             "material": True}

            pred = self.decoder(
                self.encoder(feed_dict['img'], return_feature_maps=True),
                output_switch=output_switch
            )

            output_dict = {'pred': pred}
            if not self.disable_metric_and_loss_calculations:
                # loss
                loss_dict = {}
                if pred['object'] is not None:  # object
                    loss_dict['object'] = self.crit_dict['object'](pred['object'], feed_dict['seg_object'], feed_dict['valid_object'])
                if pred['part'] is not None:  # part
                    part_loss = 0
                    for idx_part, object_label in enumerate(broden_dataset.object_with_part):
                        part_loss += self.part_loss(
                            pred['part'][idx_part], feed_dict['seg_part'],
                            feed_dict['seg_object'], object_label, feed_dict['valid_part'][:, idx_part])
                    loss_dict['part'] = part_loss
                if pred['scene'] is not None:  # scene
                    loss_dict['scene'] = self.crit_dict['scene'](pred['scene'], feed_dict['scene_label'])
                if pred['material'] is not None:  # material
                    loss_dict['material'] = self.crit_dict['material'](pred['material'], feed_dict['seg_material'], feed_dict['valid_material'])
                loss_dict['total'] = sum([loss_dict[k] * self.loss_scale[k] for k in loss_dict.keys()])

                output_dict['loss'] = loss_dict

                # metric 
                metric_dict= {}
                if pred['object'] is not None:
                    # metric_dict['object'] = self.pixel_acc(
                    #     pred['object'], feed_dict['seg_object'], ignore_index=0,
                    #     samples_valid_mask=feed_dict['valid_object'],
                    #     missing_score = None)
                    correct, valid = self.get_correct_and_valid_pixels(
                        pred['object'], feed_dict['seg_object'], ignore_index=0,
                        samples_valid_mask=feed_dict['valid_object']
                    )
                    metric_dict['object'] = {'correct': correct, 'valid': valid}
                if pred['material'] is not None:
                    # metric_dict['material'] = self.pixel_acc(
                    #     pred['material'], feed_dict['seg_material'], ignore_index=0,
                    #     samples_valid_mask=feed_dict['valid_material'],
                    #     missing_score = None)
                    correct, valid = self.get_correct_and_valid_pixels(
                        pred['material'], feed_dict['seg_material'], ignore_index=0,
                        samples_valid_mask=feed_dict['valid_material']
                    )
                    metric_dict['material'] = {'correct': correct, 'valid': valid}
                if pred['part'] is not None:
                    acc_sum, pixel_sum = 0, 0
                    for idx_part, object_label in enumerate(broden_dataset.object_with_part):
                        acc, pixel = self.part_pixel_acc(
                            pred['part'][idx_part], feed_dict['seg_part'], feed_dict['seg_object'],
                            object_label, feed_dict['valid_part'][:, idx_part])
                        acc_sum += acc
                        pixel_sum += pixel
                    # if pixel_sum > 0:
                    #     part_acc = acc_sum.float() / pixel_sum.float()
                    # else:
                    #     part_acc = None
                    #metric_dict['part'] = acc_sum.float() / (pixel_sum.float() + 1e-10)
                    # metric_dict['part'] = part_acc
                    metric_dict['part'] = {'correct': acc_sum.long(),
                                            'valid': pixel_sum.long()}  # Should we account for background when calculaing valid pixels 
                if pred['scene'] is not None:
                    metric_dict['scene'] = self.pixel_acc(
                        pred['scene'], feed_dict['scene_label'], ignore_index=-1,
                        missing_score = None)
                    correct, valid = self.get_correct_and_valid_pixels(
                        pred['scene'], feed_dict['scene_label'], ignore_index=-1
                    )
                    metric_dict['scene'] = {'correct': correct, 'valid': valid}

                output_dict['metric'] = metric_dict

            return output_dict
        else: # inference
            # output_switch = {"object": True, "part": True, "scene": True, "material": True}
            pred = self.decoder(self.encoder(feed_dict['img'], return_feature_maps=True),
                                output_switch = self.test_output_switch,
                                seg_size = seg_size)
            return {'pred': pred}


def conv3x3(in_planes, out_planes, stride=1, has_bias=False):
    "3x3 convolution with padding"
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=has_bias)


def conv3x3_bn_relu(in_planes, out_planes, stride=1):
    return nn.Sequential(
            conv3x3(in_planes, out_planes, stride),
            SynchronizedBatchNorm2d(out_planes),
            nn.ReLU(inplace=True),
            )


class ModelBuilder:
    def __init__(self):
        pass

    # custom weights initialization
    @staticmethod
    def weights_init(m):
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            nn.init.kaiming_normal_(m.weight.data, nonlinearity='relu')
        elif classname.find('BatchNorm') != -1:
            m.weight.data.fill_(1.)
            m.bias.data.fill_(1e-4)
        #elif classname.find('Linear') != -1:
        #    m.weight.data.normal_(0.0, 0.0001)

    def build_encoder(self, arch='resnet50_dilated8', fc_dim=512, weights=''):
        pretrained = True if len(weights) == 0 else False
        if arch == 'resnet34':
            raise NotImplementedError
            orig_resnet = resnet.__dict__['resnet34'](pretrained=pretrained)
            net_encoder = Resnet(orig_resnet)
        elif arch == 'resnet34_dilated8':
            raise NotImplementedError
            orig_resnet = resnet.__dict__['resnet34'](pretrained=pretrained)
            net_encoder = ResnetDilated(orig_resnet,
                                        dilate_scale=8)
        elif arch == 'resnet34_dilated16':
            raise NotImplementedError
            orig_resnet = resnet.__dict__['resnet34'](pretrained=pretrained)
            net_encoder = ResnetDilated(orig_resnet,
                                        dilate_scale=16)
        elif arch == 'resnet50':
            orig_resnet = resnet.__dict__['resnet50'](pretrained=pretrained)
            net_encoder = Resnet(orig_resnet)
        elif arch == 'resnet101':
            orig_resnet = resnet.__dict__['resnet101'](pretrained=pretrained)
            net_encoder = Resnet(orig_resnet)
        elif arch == 'resnext101':
            orig_resnext = resnext.__dict__['resnext101'](pretrained=pretrained)
            net_encoder = Resnet(orig_resnext) # we can still use class Resnet
        else:
            raise Exception('Architecture undefined!')

        # net_encoder.apply(self.weights_init)
        if len(weights) > 0:
            print('Loading weights for net_encoder')
            net_encoder.load_state_dict(
                torch.load(weights, map_location=lambda storage, loc: storage), strict=False)
        return net_encoder

    def build_decoder(self, nr_classes,
                      arch='ppm_bilinear_deepsup', fc_dim=512,
                      weights='', use_softmax=False):
        if arch == 'upernet_lite':
            net_decoder = UPerNet(
                nr_classes=nr_classes,
                fc_dim=fc_dim,
                use_softmax=use_softmax,
                fpn_dim=256)
        elif arch == 'upernet':
            net_decoder = UPerNet(
                nr_classes=nr_classes,
                fc_dim=fc_dim,
                use_softmax=use_softmax,
                fpn_dim=512)
        else:
            raise Exception('Architecture undefined!')

        net_decoder.apply(self.weights_init)
        if len(weights) > 0:
            print('Loading weights for net_decoder')
            net_decoder.load_state_dict(
                torch.load(weights, map_location=lambda storage, loc: storage), strict=False)
        return net_decoder


class Resnet(nn.Module):
    def __init__(self, orig_resnet):
        super(Resnet, self).__init__()

        # take pretrained resnet, except AvgPool and FC
        self.conv1 = orig_resnet.conv1
        self.bn1 = orig_resnet.bn1
        self.relu1 = orig_resnet.relu1
        self.conv2 = orig_resnet.conv2
        self.bn2 = orig_resnet.bn2
        self.relu2 = orig_resnet.relu2
        self.conv3 = orig_resnet.conv3
        self.bn3 = orig_resnet.bn3
        self.relu3 = orig_resnet.relu3
        self.maxpool = orig_resnet.maxpool
        self.layer1 = orig_resnet.layer1
        self.layer2 = orig_resnet.layer2
        self.layer3 = orig_resnet.layer3
        self.layer4 = orig_resnet.layer4

    def forward(self, x, return_feature_maps=False):
        conv_out = []

        x = self.relu1(self.bn1(self.conv1(x)))
        x = self.relu2(self.bn2(self.conv2(x)))
        x = self.relu3(self.bn3(self.conv3(x)))
        x = self.maxpool(x)

        x = self.layer1(x); conv_out.append(x);
        x = self.layer2(x); conv_out.append(x);
        x = self.layer3(x); conv_out.append(x);
        x = self.layer4(x); conv_out.append(x);

        if return_feature_maps:
            return conv_out
        return [x]


# upernet
class UPerNet(nn.Module):
    def __init__(self, nr_classes, fc_dim=4096,
                 use_softmax=False, pool_scales=(1, 2, 3, 6),
                 fpn_inplanes=(256,512,1024,2048), fpn_dim=256):
        super(UPerNet, self).__init__()
        self.use_softmax = use_softmax

        # PPM Module
        self.ppm_pooling = []
        self.ppm_conv = []

        for scale in pool_scales:
            # we use the feature map size instead of input image size, so down_scale = 1.0
            #self.ppm_pooling.append(PrRoIPool2D(scale, scale, 1.))
            self.ppm_pooling.append(nn.AdaptiveAvgPool2d(scale))
            self.ppm_conv.append(nn.Sequential(
                nn.Conv2d(fc_dim, 512, kernel_size=1, bias=False),
                SynchronizedBatchNorm2d(512),
                nn.ReLU(inplace=True)
            ))
        self.ppm_pooling = nn.ModuleList(self.ppm_pooling)
        self.ppm_conv = nn.ModuleList(self.ppm_conv)
        self.ppm_last_conv = conv3x3_bn_relu(fc_dim + len(pool_scales)*512, fpn_dim, 1)

        # FPN Module
        self.fpn_in = []
        for fpn_inplane in fpn_inplanes[:-1]: # skip the top layer
            self.fpn_in.append(nn.Sequential(
                nn.Conv2d(fpn_inplane, fpn_dim, kernel_size=1, bias=False),
                SynchronizedBatchNorm2d(fpn_dim),
                nn.ReLU(inplace=True)
            ))
        self.fpn_in = nn.ModuleList(self.fpn_in)

        self.fpn_out = []
        for i in range(len(fpn_inplanes) - 1): # skip the top layer
            self.fpn_out.append(nn.Sequential(
                conv3x3_bn_relu(fpn_dim, fpn_dim, 1),
            ))
        self.fpn_out = nn.ModuleList(self.fpn_out)

        self.conv_fusion = conv3x3_bn_relu(len(fpn_inplanes) * fpn_dim, fpn_dim, 1)

        # background included. if ignore in loss, output channel 0 will not be trained.
        self.nr_scene_class, self.nr_object_class, self.nr_part_class, self.nr_material_class = \
            nr_classes['scene'], nr_classes['object'], nr_classes['part'], nr_classes['material']

        # input: PPM out, input_dim: fpn_dim
        self.scene_head = nn.Sequential(
            conv3x3_bn_relu(fpn_dim, fpn_dim, 1),
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(fpn_dim, self.nr_scene_class, kernel_size=1, bias=True)
        )

        # input: Fusion out, input_dim: fpn_dim
        self.object_head = nn.Sequential(
            conv3x3_bn_relu(fpn_dim, fpn_dim, 1),
            nn.Conv2d(fpn_dim, self.nr_object_class, kernel_size=1, bias=True)
        )

        # input: Fusion out, input_dim: fpn_dim
        self.part_head = nn.Sequential(
            conv3x3_bn_relu(fpn_dim, fpn_dim, 1),
            nn.Conv2d(fpn_dim, self.nr_part_class, kernel_size=1, bias=True)
        )

        # input: FPN_2 (P2), input_dim: fpn_dim
        self.material_head = nn.Sequential(
            conv3x3_bn_relu(fpn_dim, fpn_dim, 1),
            nn.Conv2d(fpn_dim, self.nr_material_class, kernel_size=1, bias=True)
        )

    def forward(self, conv_out, output_switch=None, seg_size=None):

        output_dict = {k: None for k in output_switch.keys()}

        conv5 = conv_out[-1]
        input_size = conv5.size()
        ppm_out = [conv5]
        roi = [] # fake rois, just used for pooling
        for i in range(input_size[0]): # batch size
            roi.append(torch.Tensor([i, 0, 0, input_size[3], input_size[2]]).view(1, -1)) # b, x0, y0, x1, y1
        roi = torch.cat(roi, dim=0).type_as(conv5)
        ppm_out = [conv5]
        for pool_scale, pool_conv in zip(self.ppm_pooling, self.ppm_conv):
            ppm_out.append(pool_conv(F.interpolate(
                pool_scale(conv5),
                (input_size[2], input_size[3]),
                mode='bilinear', align_corners=False)))
        ppm_out = torch.cat(ppm_out, 1)
        f = self.ppm_last_conv(ppm_out)

        if output_switch['scene']: # scene
            output_dict['scene'] = self.scene_head(f)

        if output_switch['object'] or output_switch['part'] or output_switch['material']:
            fpn_feature_list = [f]
            for i in reversed(range(len(conv_out) - 1)):
                conv_x = conv_out[i]
                conv_x = self.fpn_in[i](conv_x) # lateral branch

                f = F.interpolate(
                    f, size=conv_x.size()[2:], mode='bilinear', align_corners=False) # top-down branch
                f = conv_x + f

                fpn_feature_list.append(self.fpn_out[i](f))
            fpn_feature_list.reverse() # [P2 - P5]

            # material
            if output_switch['material']:
                output_dict['material'] = self.material_head(fpn_feature_list[0])

            if output_switch['object'] or output_switch['part']:
                output_size = fpn_feature_list[0].size()[2:]
                fusion_list = [fpn_feature_list[0]]
                for i in range(1, len(fpn_feature_list)):
                    fusion_list.append(F.interpolate(
                        fpn_feature_list[i],
                        output_size,
                        mode='bilinear', align_corners=False))
                fusion_out = torch.cat(fusion_list, 1)
                x = self.conv_fusion(fusion_out)

                if output_switch['object']: # object
                    output_dict['object'] = self.object_head(x)
                if output_switch['part']:
                    output_dict['part'] = self.part_head(x)

        if self.use_softmax or seg_size is not None:  # is True during inference
            # inference scene
            x = output_dict['scene']
            x = x.squeeze(3).squeeze(2)
            if self.use_softmax:
                x = F.softmax(x, dim=1)
            output_dict['scene'] = x

            # inference object, material
            for k in ['object', 'material']:
                x = output_dict[k]
                x = F.interpolate(x, size=seg_size, mode='bilinear', align_corners=False)
                if self.use_softmax:
                    x = F.softmax(x, dim=1)
                output_dict[k] = x

            # inference part
            x = output_dict['part']
            x = F.interpolate(x, size=seg_size, mode='bilinear', align_corners=False)
            part_pred_list, head = [], 0
            for idx_part, object_label in enumerate(broden_dataset.object_with_part):
                n_part = len(broden_dataset.object_part[object_label])
                _x = F.interpolate(x[:, head: head + n_part], size=seg_size, mode='bilinear', align_corners=False)
                if self.use_softmax:
                    _x = F.softmax(_x, dim=1)
                part_pred_list.append(_x)
                head += n_part
            output_dict['part'] = part_pred_list

        else:   # Training
            # object, scene, material
            for k in ['object', 'scene', 'material']:
                if output_dict[k] is None:
                    continue
                x = output_dict[k]
                x = F.log_softmax(x, dim=1)
                if k == "scene":  # for scene
                    x = x.squeeze(3).squeeze(2)
                output_dict[k] = x
            if output_dict['part'] is not None:
                part_pred_list, head = [], 0
                for idx_part, object_label in enumerate(broden_dataset.object_with_part):
                    n_part = len(broden_dataset.object_part[object_label])
                    x = output_dict['part'][:, head: head + n_part]
                    x = F.log_softmax(x, dim=1)
                    part_pred_list.append(x)
                    head += n_part
                output_dict['part'] = part_pred_list

        return output_dict
