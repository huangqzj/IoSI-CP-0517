"""
Implementation of Where2comm fusion.
"""

import numpy as np
import random
import torch
import torch.nn as nn
import torch.nn.functional as F
import cv2
from opencood.models.fuse_modules.self_attn import ScaledDotProductAttention
import os
import shutil

class Communication(nn.Module):
    def __init__(self, args):
        super(Communication, self).__init__()
        # Threshold of objectiveness
        self.threshold = args['threshold']
        if 'gaussian_smooth' in args:
            # Gaussian Smooth
            self.smooth = True
            kernel_size = args['gaussian_smooth']['k_size']
            c_sigma = args['gaussian_smooth']['c_sigma']
            self.gaussian_filter = nn.Conv2d(1, 1, kernel_size=kernel_size, stride=1, padding=(kernel_size - 1) // 2)
            self.init_gaussian_filter(kernel_size, c_sigma)
            self.gaussian_filter.requires_grad = False
        else:
            self.smooth = False

    def init_gaussian_filter(self, k_size=5, sigma=1.0):
        center = k_size // 2
        x, y = np.mgrid[0 - center: k_size - center, 0 - center: k_size - center]
        gaussian_kernel = 1 / (2 * np.pi * sigma) * np.exp(-(np.square(x) + np.square(y)) / (2 * np.square(sigma)))

        self.gaussian_filter.weight.data = torch.Tensor(gaussian_kernel).to(
            self.gaussian_filter.weight.device).unsqueeze(0).unsqueeze(0)
        self.gaussian_filter.bias.data.zero_()

    def forward(self, batch_confidence_maps, B):
        """
        Args:
            batch_confidence_maps: [(L1, H, W), (L2, H, W), ...]
        """

        _, _, H, W = batch_confidence_maps[0].shape
        communication_masks = []
        communication_rates = []
        for b in range(B):
            ori_communication_maps, _ = batch_confidence_maps[b].sigmoid().max(dim=1, keepdim=True)
            if self.smooth:
                communication_maps = self.gaussian_filter(ori_communication_maps)
            else:
                communication_maps = ori_communication_maps

            L = communication_maps.shape[0]
            if self.training:
                # Official training proxy objective
                K = int(H * W * random.uniform(0, 1))
                communication_maps = communication_maps.reshape(L, H * W)
                _, indices = torch.topk(communication_maps, k=K, sorted=False)
                communication_mask = torch.zeros_like(communication_maps).to(communication_maps.device)
                ones_fill = torch.ones(L, K, dtype=communication_maps.dtype, device=communication_maps.device)
                communication_mask = torch.scatter(communication_mask, -1, indices, ones_fill).reshape(L, 1, H, W)
                # print('communication_mask.sum(),K,communication_mask.shape,communication_maps.shape:',communication_mask.sum(),K,communication_mask.shape,communication_maps.shape)
                # communication_mask.sum(),K,communication_mask.shape,communication_maps.shape: tensor(14878., device='cuda:0') 7439 torch.Size([2, 1, 48, 176]) torch.Size([2, 8448])
                # communication_mask.sum(),K,communication_mask.shape,communication_maps.shape: tensor(32964., device='cuda:0') 8241 torch.Size([4, 1, 48, 176]) torch.Size([4, 8448])
                # communication_mask.sum(),K,communication_mask.shape,communication_maps.shape: tensor(12260., device='cuda:0') 6130 torch.Size([2, 1, 48, 176]) torch.Size([2, 8448])
            elif self.threshold:
                ones_mask = torch.ones_like(communication_maps).to(communication_maps.device)
                zeros_mask = torch.zeros_like(communication_maps).to(communication_maps.device)
                communication_mask = torch.where(communication_maps > self.threshold, ones_mask, zeros_mask)
                # print('self.threshold,communication_maps.sum(),communication_mask.sum(),L * H * W:',self.threshold,communication_maps.sum(),communication_mask.sum(),L * H * W,L,W,H)
            else:
                communication_mask = torch.ones_like(communication_maps).to(communication_maps.device)

            communication_rate = communication_mask.sum() / (L * H * W)
            # Ego
            communication_mask[0] = 1

            communication_masks.append(communication_mask)
            communication_rates.append(communication_rate)
        communication_rates = sum(communication_rates) / B
        # print('self.training,communication_rates:',self.training,communication_rates)
        communication_masks = torch.cat(communication_masks, dim=0)
        return communication_masks, communication_rates


class AttentionFusion(nn.Module):
    def __init__(self, feature_dim):
        super(AttentionFusion, self).__init__()
        self.att = ScaledDotProductAttention(feature_dim)

    def forward(self, x):
        cav_num, C, H, W = x.shape  ##x.shape [4, 64, 96, 352]
        x = x.view(cav_num, C, -1).permute(2, 0, 1)  # (H*W, cav_num, C), perform self attention on each pixel # x.shape [33792, 4, 64]
        x = self.att(x, x, x)
        x = x.permute(1, 2, 0).view(cav_num, C, H, W)[0]  # C, W, H before ## [ 4, 64, 33792] -> [ 4, 64, 96, 352] -> [ 1, 64, 96, 352]
        return x


class Where2comm(nn.Module):
    def __init__(self, args):
        super(Where2comm, self).__init__()
        self.discrete_ratio = args['voxel_size'][0]
        self.downsample_rate = args['downsample_rate']

        self.fully = args['fully']
        if self.fully:
            print('constructing a fully connected communication graph')
        else:
            print('constructing a partially connected communication graph')

        self.multi_scale = args['multi_scale']
        if self.multi_scale:
            layer_nums = args['layer_nums']
            num_filters = args['num_filters']
            self.num_levels = len(layer_nums)
            self.fuse_modules = nn.ModuleList()
            # print('layer_nums,self.num_levels,num_filters:',layer_nums,self.num_levels,num_filters)
            # layer_nums,self.num_levels,num_filters: [3, 5, 8] 3 [64, 128, 256]
            for idx in range(self.num_levels):
                fuse_network = AttentionFusion(num_filters[idx])
                self.fuse_modules.append(fuse_network)
        else:
            self.fuse_modules = AttentionFusion(args['in_channels'])

        self.naive_communication = Communication(args['communication'])

    def regroup(self, x, record_len):
        cum_sum_len = torch.cumsum(record_len, dim=0)
        split_x = torch.tensor_split(x, cum_sum_len[:-1].cpu())
        # print('cum_sum_len,split_x.shape:',cum_sum_len,len(split_x),split_x[0].shape)
        return split_x

    def forward(self, x, psm_single, record_len, pairwise_t_matrix, time_delay, backbone=None):
    # def forward(self, x, psm_single, record_len, pairwise_t_matrix, time_delay, backbone=None):
        """
        Fusion forwarding.

        Parameters:
            x: Input data, (sum(n_cav), C, H, W).
            record_len: List, (B).
            pairwise_t_matrix: The transformation matrix from each cav to ego, (B, L, L, 4, 4).

        Returns:
            Fused feature.
        """

        _, C, H, W = x.shape  ## x.shape -> [4, 64, 192, 704]
        B = pairwise_t_matrix.shape[0] ## shape -> [1, 5, 5, 4, 4]
        # print('time_delay.shape:',time_delay)
        if self.multi_scale:
            ups = []
            # print('x orinshape:',x.shape)
            for i in range(self.num_levels):
                # print('i,x.shape before:',i,x.shape)
                x = backbone.blocks[i](x) ## x.shape -> [4, 64, 96, 352]
                # print('i x.shape after :',i,x.shape)
                # 1. Communication (mask the features)
                if i == 0:
                    if self.fully:
                        communication_rates = torch.tensor(1).to(x.device)
                    else:
                        # Prune
                        batch_confidence_maps = self.regroup(psm_single, record_len)  ## batch_confidence_maps.shape -> [1, 4, 2, 48, 176],B=1
                        communication_masks, communication_rates = self.naive_communication(batch_confidence_maps, B)
                        # print('communication_rates:',communication_rates)
                        ## communication_masks.shape -> [4, 1, 48, 176]
                        ## communication_rates value ~ [0,1]
                        if x.shape[-1] != communication_masks.shape[-1]:  ##
                            communication_masks = F.interpolate(communication_masks, size=(x.shape[-2], x.shape[-1]),
                                                                mode='bilinear', align_corners=False)
                        ### Original
                        # communication_masks.shape -> [4, 1, 96, 352]
                        x = x * communication_masks  ## x.shape -> [4, 64, 96, 352]
                        # print(x[0].element_size() * x[0].nelement())
                        # print('x.shape, communication_masks.shape,time_delay.shape:',x.shape, communication_masks.shape,time_delay.shape)
                        ### add aoi
                        # aoI_time_delay = time_delay[0]
                        # aoi_flag_keep = []
                        # print("aoI_time_delay:",aoI_time_delay)
                        # for k in range(communication_masks.shape[0]):
                        #     shutil.rmtree('opencood/logs/commasks/')
                        #     os.mkdir('opencood/logs/commasks/')
                        #     cv2.imwrite('opencood/logs/commasks/'+str(k)+'.png',(communication_masks[k].permute(2,1,0).cpu().numpy()*255))
                        #     # if aoI_flag[k] == 2.0:
                        #         # communication_masks[k] = communication_masks[k] * 0.0
                        #     if aoI_time_delay[k] != 5.0:
                        #         aoi_flag_keep.append(k)
                        # mask_communication_masks = communication_masks[aoi_flag_keep]
                        # mask_x = x[aoi_flag_keep]
                        # x = mask_x * mask_communication_masks  ## x.shape -> [4, 64, 96, 352]
                        ####################
                        ## add AoI with weight
                        # aoI_time_delay = time_delay[0]
                        # # print('aoI_time_delay:',aoI_time_delay)
                        # for k in range(communication_masks.shape[0]):
                        #     # cv2.imwrite('opencood/logs/commasks/' + str(k) + '.png',
                        #     #             (communication_masks[k].permute(2, 1, 0).cpu().numpy() * 255))
                        #     if aoI_time_delay[k]>=2:
                        #         communication_masks[k] = communication_masks[k] * (1.8 / (1 + aoI_time_delay[k]))
                        #     # cv2.imwrite('opencood/logs/commasks/' + str(k) + '_aoiweight.png',
                        #     #             (communication_masks[k].permute(2, 1, 0).cpu().numpy() * 255))
                        # x = x * communication_masks  ## x.shape -> [4, 64, 96, 352]
                # 2. Split the features
                # split_x: [(L1, C, H, W), (L2, C, H, W), ...]
                # For example [[2, 256, 48, 176], [1, 256, 48, 176], ...]
                batch_node_features = self.regroup(x, record_len)

                # 3. Fusion
                x_fuse = []
                for b in range(B):
                    neighbor_feature = batch_node_features[b]  ## neighbor_feature.shape -> [4, 64, 96, 352]
                    x_fuse.append(self.fuse_modules[i](neighbor_feature))
                x_fuse = torch.stack(x_fuse) # x_fuse three time : [4, 64, 96, 352] [4, 128, 48, 176] [4, 256, 24, 88]
                # print('i,x_fuse:',i,x_fuse.shape)
                # 4. Deconv
                if len(backbone.deblocks) > 0:
                    ups.append(backbone.deblocks[i](x_fuse))
                else:
                    ups.append(x_fuse)
                # print('i ups.shape:',i,ups[i].shape)
            ## len(ups),len(backbone.deblocks),self.num_levels: 3 3 3
            if len(ups) > 1:
                x_fuse = torch.cat(ups, dim=1)  # x_fuse [1, 384, 96, 352]
            elif len(ups) == 1:
                x_fuse = ups[0]
            if len(backbone.deblocks) > self.num_levels:
                x_fuse = backbone.deblocks[-1](x_fuse)
            # print('x_fuse:',x_fuse.shape)
        else:
            # 1. Communication (mask the features)
            if self.fully:
                communication_rates = torch.tensor(1).to(x.device)
            else:
                # Prune
                batch_confidence_maps = self.regroup(psm_single, record_len)
                communication_masks, communication_rates = self.naive_communication(batch_confidence_maps, B)
                x = x * communication_masks

            # 2. Split the features
            # split_x: [(L1, C, H, W), (L2, C, H, W), ...]
            # For example [[2, 256, 48, 176], [1, 256, 48, 176], ...]
            batch_node_features = self.regroup(x, record_len)

            # 3. Fusion
            x_fuse = []
            for b in range(B):
                neighbor_feature = batch_node_features[b]
                x_fuse.append(self.fuse_modules(neighbor_feature))
            x_fuse = torch.stack(x_fuse)
        return x_fuse, communication_rates
