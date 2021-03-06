import os
import errno
import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import OrderedDict

class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


def conv_layer(in_channels, out_channels, kernel=3, stride=1, dropout=0.1, bias=False):
    groups = 1
    #print(kernel, 'x', kernel, 'x', in_channels, 'x', out_channels)
    return nn.Sequential(OrderedDict([
        ('conv', nn.Conv2d(in_channels, out_channels, kernel_size=kernel,
                           stride=stride, padding=kernel//2, groups=groups, bias=bias)),
        ('norm', nn.BatchNorm2d(out_channels)),
        ('relu', nn.ReLU6(inplace=True)),
    ]))


def dw_conv_layer(in_channels, out_channels, stride=1, bias=False):
    groups = in_channels

    return nn.Sequential(OrderedDict([
        ('dwconv', nn.Conv2d(groups, groups, kernel_size=3,
                             stride=stride, padding=1, groups=groups, bias=bias)),
        ('norm', nn.BatchNorm2d(groups)),
    ]))


def comb_conv_layer(in_channels, out_channels, kernel=1, stride=1, dropout=0.1, bias=False):
    return nn.Sequential(OrderedDict([
        ('layer1', conv_layer(in_channels, out_channels, kernel)),
        ('layer2', dw_conv_layer(out_channels, out_channels, stride=stride))
    ]))


class HarDBlock(nn.Module):
    def get_link(self, layer, base_ch, growth_rate, grmul):
        if layer == 0:
            return base_ch, 0, []
        out_channels = growth_rate
        link = []
        for i in range(10):
            dv = 2 ** i
            if layer % dv == 0:
                k = layer - dv
                link.append(k)
                if i > 0:
                    out_channels *= grmul
        out_channels = int(int(out_channels + 1) / 2) * 2
        in_channels = 0
        for i in link:
            ch,_,_ = self.get_link(i, base_ch, growth_rate, grmul)
            in_channels += ch
        return out_channels, in_channels, link

    def get_out_ch(self):
        return self.out_channels

    def __init__(self, in_channels, growth_rate, grmul, n_layers, keepBase=False, residual_out=False, dwconv=False):
        super().__init__()
        self.keepBase = keepBase
        self.links = []
        layers_ = []
        self.out_channels = 0 # if upsample else in_channels
        for i in range(n_layers):
            outch, inch, link = self.get_link(i+1, in_channels, growth_rate, grmul)
            self.links.append(link)
            use_relu = residual_out
            if dwconv:
                layers_.append(comb_conv_layer(inch, outch))
            else:
                layers_.append(conv_layer(inch, outch))

            if (i % 2 == 0) or (i == n_layers - 1):
                self.out_channels += outch
        #print("Blk out =",self.out_channels)
        self.layers = nn.ModuleList(layers_)

    def forward(self, x):
        layers_ = [x]

        for layer in range(len(self.layers)):
            link = self.links[layer]
            tin = []
            for i in link:
                tin.append(layers_[i])
            if len(tin) > 1:
                x = torch.cat(tin, 1)
            else:
                x = tin[0]
            out = self.layers[layer](x)
            layers_.append(out)

        t = len(layers_)
        out_ = []
        for i in range(t):
            if (i == 0 and self.keepBase) or \
               (i == t-1) or (i%2 == 1):
                out_.append(layers_[i])
        out = torch.cat(out_, 1)
        return out




class HarDNet(nn.Module):
    def __init__(self, depth_wise=False, arch=85, pretrained=True, weight_path=''):
        super().__init__()

        if arch == 68:
            # HarDNet68
            first_ch = [32, 64]
            ch_list  = [128, 256, 320, 640, 1024]
            grmul = 1.7
            gr       = [ 14,  16,  20,  40,  160]
            n_layers = [  8,  16,  16,  16,    4]
            downSamp = [  1,   0,   1,   1,    0]
            drop_rate = 0.1
        elif arch == 85:
            # HarDNet85
            first_ch = [48, 96]
            ch_list  = [192, 256, 320, 480, 720, 1280]
            grmul = 1.7
            gr       = [ 24,  24,  28,  36,  48,  256]
            n_layers = [  8,  16,  16,  16,  16,    4]
            downSamp = [  1,   0,   1,   0,   1,    0]
            drop_rate = 0.2
        elif arch == 39:
            # HarDNet39
            first_ch = [24, 48]
            ch_list  = [96, 320, 640, 1024]
            grmul = 1.6
            gr       = [16,  20,  64,  160]
            n_layers = [ 4,  16,   8,    4]
            downSamp = [ 1,   1,   1,    0]
        else:
            raise ValueError("Architecture type %s is not supported" % arch)

        second_kernel = 3
        max_pool = True

        if depth_wise:
            second_kernel = 1
            max_pool = False
            drop_rate = 0.05

        blks = len(n_layers)
        self.base = nn.ModuleList([])

        # First Layer: Standard Conv3x3, Stride=2
        self.base.append(
             conv_layer(in_channels=3, out_channels=first_ch[0], kernel=3,
                        stride=2,  bias=False))

        # Second Layer
        self.base.append(conv_layer(first_ch[0], first_ch[1], kernel=second_kernel))

        # Maxpooling or DWConv3x3 downsampling
        if max_pool:
            self.base.append(nn.MaxPool2d(kernel_size=3, stride=2, padding=1))
        else:
            self.base.append(dw_conv_layer(first_ch[1], first_ch[1], stride=2))

        # Build all HarDNet blocks
        ch = first_ch[1]
        for i in range(blks):
            blk = HarDBlock(ch, gr[i], grmul, n_layers[i], dwconv=depth_wise)
            ch = blk.get_out_ch()
            self.base.append(blk)

            if i == blks-1 and arch == 85:
                self.base.append(nn.Dropout(0.1))

            self.base.append(conv_layer(ch, ch_list[i], kernel=1))
            ch = ch_list[i]
            if downSamp[i] == 1:
                if max_pool:
                    self.base.append(nn.MaxPool2d(kernel_size=2, stride=2))
                else:
                    self.base.append(dw_conv_layer(ch, ch, stride=2))


        ch = ch_list[blks-1]
        self.base.append(
            nn.Sequential(
                nn.AdaptiveAvgPool2d((1,1)),
                Flatten(),
                nn.Dropout(drop_rate),
                nn.Linear(ch, 1000) ))

        #print(self.base)

        if pretrained:
            # Represent the architecture with a single string
            arch_codename = "HarDNet%d" % (arch)
            if depth_wise:
                arch_codename += "DS"

            if hasattr(torch, 'hub'):
                checkpoint_urls = {
                    "HarDNet39_DS": 'https://ping-chao.com/hardnet/hardnet39ds-0e6c6fa9.pth',
                    "HarDNet68": 'https://ping-chao.com/hardnet/hardnet68-5d684880.pth',
                    "HarDNet68DS": 'https://ping-chao.com/hardnet/hardnet68ds-632474d2.pth',
                    "HarDNet85": 'https://ping-chao.com/hardnet/hardnet85-a28faa00.pth',
                }

                checkpoint = checkpoint_urls[arch_codename]
                self.load_state_dict(torch.hub.load_state_dict_from_url(checkpoint, progress=False))
            else:
                weight_file = '%s%s.pth' % (weight_path, arch_codename.lower())
                if not os.path.isfile(weight_file):
                    raise FileNotFoundError(
                        errno.ENOENT, os.strerror(errno.ENOENT), weight_file)

                weights = torch.load(weight_file)
                self.load_state_dict(weights)

            print('ImageNet pretrained weights for %s is loaded' % arch_codename)

    def forward(self, x):
        for layer in self.base:
            x = layer(x)
        return x
