import time

import torch.nn as nn

from opencood.models.sub_modules.base_bev_backbone import BaseBEVBackbone
from opencood.models.fuse_modules.HPHA_fuse import HPHA
from opencood.models.sub_modules.downsample_conv import DownsampleConv
from opencood.models.sub_modules.naive_compress import NaiveCompressor
from opencood.models.sub_modules.pillar_vfe import PillarVFE
from opencood.models.sub_modules.point_pillar_scatter import PointPillarScatter
import torch

class PointPillarIoSICP(nn.Module):
    def __init__(self, args):
        super(PointPillarIoSICP, self).__init__()
        self.max_cav = args['max_cav']
        # Pillar VFE
        self.pillar_vfe = PillarVFE(args['pillar_vfe'],
                                    num_point_features=4,
                                    voxel_size=args['voxel_size'],
                                    point_cloud_range=args['lidar_range'])
        self.scatter = PointPillarScatter(args['point_pillar_scatter'])
        self.backbone = BaseBEVBackbone(args['base_bev_backbone'], 64)

        # Used to down-sample the feature map for efficient computation
        if 'shrink_header' in args:
            self.shrink_flag = True
            self.shrink_conv = DownsampleConv(args['shrink_header'])
        else:
            self.shrink_flag = False

        if args['compression']:
            self.compression = True
            self.naive_compressor = NaiveCompressor(256, args['compression'])
        else:
            self.compression = False

        self.fusion_net = HPHA(args['HPHA_fusion'])
        self.multi_scale = args['HPHA_fusion']['multi_scale']

        self.cls_head = nn.Conv2d(args['head_dim'], args['anchor_number'], kernel_size=1)
        self.reg_head = nn.Conv2d(args['head_dim'], 7 * args['anchor_number'], kernel_size=1)

        if args['backbone_fix']:
            self.backbone_fix()

    def backbone_fix(self):
        """
        Fix the parameters of backbone during finetune on timedelay.
        """

        for p in self.pillar_vfe.parameters():
            p.requires_grad = False

        for p in self.scatter.parameters():
            p.requires_grad = False

        for p in self.backbone.parameters():
            p.requires_grad = False

        if self.compression:
            for p in self.naive_compressor.parameters():
                p.requires_grad = False
        if self.shrink_flag:
            for p in self.shrink_conv.parameters():
                p.requires_grad = False

        for p in self.cls_head.parameters():
            p.requires_grad = False
        for p in self.reg_head.parameters():
            p.requires_grad = False

    def forward(self, data_dict):
        voxel_features = data_dict['processed_lidar']['voxel_features']
        voxel_coords = data_dict['processed_lidar']['voxel_coords']
        voxel_num_points = data_dict['processed_lidar']['voxel_num_points']
        record_len = data_dict['record_len']
        pairwise_t_matrix = data_dict['pairwise_t_matrix']
        time_delay = data_dict['time_delay'] ## add time delay for feature enhance
        batch_dict = {'voxel_features': voxel_features,
                      'voxel_coords': voxel_coords,
                      'voxel_num_points': voxel_num_points,
                      'record_len': record_len}
        # n, 4 -> n, c
        batch_dict = self.pillar_vfe(batch_dict)
        # n, c -> N, C, H, W
        batch_dict = self.scatter(batch_dict)
        batch_dict = self.backbone(batch_dict)

        # N, C, H', W': [N, 256, 48, 176]
        # print('batch_dict spatial_features_2d shape:',batch_dict['spatial_features_2d'].shape,len(batch_dict['spatial_features_2d'].shape),len(batch_dict['spatial_features_2d'][3:].shape))
        if len(batch_dict['spatial_features_2d'][3:].shape) >= 4:
            spatial_features_2d = torch.cat([batch_dict['spatial_features_2d'][0].unsqueeze(0),batch_dict['spatial_features_2d'][3:]], dim=0)
        else:
            spatial_features_2d = torch.cat([batch_dict['spatial_features_2d'][0].unsqueeze(0),batch_dict['spatial_features_2d'][3:]].unsqueeze(0), dim=0)
        # print('spatial_features_2d.shape:',spatial_features_2d.shape)
        # Down-sample feature to reduce memory
        if self.shrink_flag: ## self.shrink_flag->True
            spatial_features_2d = self.shrink_conv(spatial_features_2d)  ## [4, 384, 96, 352]->[4, 256, 48, 176]
        psm_single = self.cls_head(spatial_features_2d) ## [4, 2, 48, 176]

        # Compressor
        if self.compression:  ## self.compression False
            # The ego feature is also compressed
            spatial_features_2d = self.naive_compressor(spatial_features_2d)
        # print('t2:',time.time())
        if self.multi_scale:
            # add historical semantic information of ego
            if len(batch_dict['spatial_features'][3:].shape) >= 4:
                batch_semantic_informantion_dict = torch.cat(
                    [batch_dict['spatial_features'][0].unsqueeze(0), batch_dict['spatial_features'][3:]], dim=0)
            else:
                batch_semantic_informantion_dict = torch.cat([batch_dict['spatial_features'][0].unsqueeze(0),
                                                 batch_dict['spatial_features'][3:]].unsqueeze(0), dim=0)
            fused_feature, communication_rates = self.fusion_net(batch_semantic_informantion_dict, ## semantic information ## batch_dict['spatial_features']-> [4, 64, 192, 704])
                                                                 batch_dict['spatial_features'][1:3], ## historical semantic information
                                                                 psm_single,
                                                                 record_len,
                                                                 pairwise_t_matrix,
                                                                 time_delay,
                                                                 self.backbone)
            if self.shrink_flag:
                fused_feature = self.shrink_conv(fused_feature) ## [1, 384, 96, 352] -> [1, 256, 48, 176]
        else:   ## 采用downsample后的低分辨率feature融合,
            fused_feature, communication_rates = self.fusion_net(spatial_features_2d,
                                                                 psm_single,
                                                                 record_len,
                                                                 pairwise_t_matrix,time_delay,)
        psm = self.cls_head(fused_feature) ## [1, 256, 48, 176] -> [1, 256, 48, 176]
        rm = self.reg_head(fused_feature)  ## [1, 256, 48, 176] -> [1, 256, 48, 176]
        output_dict = {'psm': psm, 'rm': rm, 'com': communication_rates}
        # print('t3:', time.time())
        return output_dict
