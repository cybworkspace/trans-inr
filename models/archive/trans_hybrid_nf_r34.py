import math

import torch
import torch.nn as nn
import torch.nn.functional as F
import einops

import models
from models import register


def init_wb(shape):
    weight = torch.empty(shape[1], shape[0] - 1)
    nn.init.kaiming_uniform_(weight, a=math.sqrt(5))

    bias = torch.empty(shape[1], 1)
    fan_in, _ = nn.init._calculate_fan_in_and_fan_out(weight)
    bound = 1 / math.sqrt(fan_in) if fan_in > 0 else 0
    nn.init.uniform_(bias, -bound, bound)

    return torch.cat([weight, bias], dim=1).t().detach()


@register('trans_hybrid_nf_r34')
class TransHybridNfR34(nn.Module):

    def __init__(self, tokenizer, hyponet, n_groups, transformer_encoder):
        super().__init__()
        dim = transformer_encoder['args']['dim']
        self.tokenizer = models.make(tokenizer, args={'dim': dim})
        self.hyponet = models.make(hyponet, args={'locfeat_dim': 512})
        self.transformer_encoder = models.make(transformer_encoder)

        resnet_encoder = {
            'name': 'resnet34',
            'args': {},
            'sd': torch.load('./assets/resnet34.pth'),
        }
        self.resnet_encoder = models.make(resnet_encoder, load_sd=True)

        self.base_params = nn.ParameterDict()
        n_wtokens = 0
        self.wtoken_postfc = nn.ModuleDict()
        self.wtoken_rng = dict()
        for name, shape in self.hyponet.param_shapes.items():
            self.base_params[name] = nn.Parameter(init_wb(shape))
            g = min(n_groups, shape[1])
            assert shape[1] % g == 0
            self.wtoken_postfc[name] = nn.Sequential(
                nn.LayerNorm(dim), ##
                nn.Linear(dim, shape[0] - 1),
            )
            self.wtoken_rng[name] = (n_wtokens, n_wtokens + g)
            n_wtokens += g
        self.wtokens = nn.Parameter(torch.randn(n_wtokens, dim))

    def forward(self, data):
        imgs = data['support_imgs']
        B, N = imgs.shape[:2]
        imgs = imgs.view(B * N, *imgs.shape[2:])
        featmaps = self.resnet_encoder(imgs)
        resized = [featmaps[0]]
        for i in range(1, len(featmaps)):
            x = F.interpolate(featmaps[i], size=featmaps[0].shape[2:], mode='bilinear', align_corners=False)
            resized.append(x)
        featmaps = torch.cat(resized, dim=1)
        featmaps = featmaps.view(B, N, *featmaps.shape[1:])

        dtokens = self.tokenizer(data)
        B = dtokens.shape[0]
        wtokens = einops.repeat(self.wtokens, 'n d -> b n d', b=B)
        trans_in = torch.cat([dtokens, wtokens], dim=1)
        trans_out = self.transformer_encoder(trans_in)

        # n_views = data['support_imgs'].shape[1]
        # featmaps = trans_out[:, :-len(self.wtokens), :].view(B, n_views, *self.tokenizer.grid_shape, -1)
        # featmaps = einops.rearrange(featmaps, 'b n h w d -> b n d h w')

        trans_out = trans_out[:, -len(self.wtokens):, :]

        params = dict()
        for name, shape in self.hyponet.param_shapes.items():
            wb = einops.repeat(self.base_params[name], 'n m -> b n m', b=B)
            w, b = wb[:, :-1, :], wb[:, -1:, :]

            l, r = self.wtoken_rng[name]
            x = self.wtoken_postfc[name](trans_out[:, l: r, :])
            x = x.transpose(-1, -2) # (B, shape[0] - 1, g)
            w = F.normalize(w * x.repeat(1, 1, w.shape[2] // x.shape[2]), dim=1)

            wb = torch.cat([w, b], dim=1)
            params[name] = wb

        params['_featmaps'] = featmaps
        params['_poses'] = data['support_poses']
        params['_HWf'] = (*data['support_imgs'].shape[-2:], data['focal'][0].item())
        self.hyponet.set_params(params)
        return self.hyponet
