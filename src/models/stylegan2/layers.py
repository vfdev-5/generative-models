import math

import torch
import torch.nn.functional as F

from torch import nn, Tensor


def equalized_lr_init(weight: Tensor, bias: Tensor, scale_weights=True,
                      lr_mult=1.0) -> float:
    fan_in, _ = nn.init._calculate_fan_in_and_fan_out(weight)
    he_std = 1.0 / math.sqrt(fan_in)

    if scale_weights:
        init_std = 1.0 / lr_mult
        scale = he_std * lr_mult
    else:
        init_std = he_std / lr_mult
        scale = lr_mult

    nn.init.normal_(weight, mean=0.0, std=init_std)
    if bias is not None:
        nn.init.zeros_(bias)
    return scale


class EqualLeakyReLU(nn.LeakyReLU):
    def __init__(self, negative_slope=0.2, inplace=False, gain=math.sqrt(2)):
        self.gain = gain
        super(EqualLeakyReLU, self).__init__(negative_slope, inplace)

    def forward(self, x):
        if self.inplace:
            x = x.mul_(self.gain)
        else:
            x = x * self.gain
        return F.leaky_relu(x, self.negative_slope, self.inplace)


class RandomGaussianNoise(nn.Module):
    def __init__(self):
        super(RandomGaussianNoise, self).__init__()
        self.gain = nn.Parameter(torch.empty(1), requires_grad=True)
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.zeros_(self.gain)

    def forward(self, x):
        N, C, H, W = x.shape
        noise = x.new_empty(N, 1, H, W).normal_()
        return x + noise * self.gain


class InputNoise(nn.Module):
    def __init__(self, channels, size=4):
        super(InputNoise, self).__init__()
        self.weight = nn.Parameter(torch.empty(1, channels, size, size),
                                   requires_grad=True)
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.normal_(self.weight)

    def forward(self, n):
        x = self.weight.expand(n, -1, -1, -1)
        return x


class EqualLinear(nn.Linear):
    def __init__(self, in_features, out_features, bias=True,
                 scale_weights=True, lr_mult=1.0):
        self.scale_weights = scale_weights
        self.lr_mult = lr_mult
        self.w_mult = 1.0
        super(EqualLinear, self).__init__(in_features, out_features, bias)

    def reset_parameters(self):
        self.w_mult = equalized_lr_init(
            self.weight, self.bias, self.scale_weights, self.lr_mult)

    def forward(self, x):
        weight = self.weight * self.w_mult
        bias = self.bias
        if bias is not None:
            bias = bias * self.lr_mult
        return F.linear(x, weight, bias)


class EqualConv2d(nn.Conv2d):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1, bias=True,
                 scale_weights=True, lr_mult=1.0):
        self.scale_weights = scale_weights
        self.lr_mult = lr_mult
        self.w_mult = 1.0
        super(EqualConv2d, self).__init__(
            in_channels, out_channels, kernel_size, stride, padding,
            dilation, groups, bias, padding_mode='zeros')

    def reset_parameters(self):
        self.w_mult = equalized_lr_init(
            self.weight, self.bias, self.scale_weights, self.lr_mult)

    def conv2d_forward(self, x, weight, bias):
        return F.conv2d(x, weight, bias, self.stride, self.padding,
                        self.dilation, self.groups)

    def forward(self, x):
        weight = self.weight * self.w_mult
        bias = self.bias
        if bias is not None:
            bias = bias * self.lr_mult
        return self.conv2d_forward(x, weight, bias)


class ModulatedConv2d(nn.Conv2d):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, bias=True, demodulate=True,
                 scale_weights=True, lr_mult=1.0):
        self.demodulate = demodulate
        self.scale_weights = scale_weights
        self.lr_mult = lr_mult
        self.w_mult = 1.0
        super(ModulatedConv2d, self).__init__(
            in_channels, out_channels, kernel_size, stride, padding,
            dilation, groups=1, bias=bias, padding_mode='zeros')

    def reset_parameters(self):
        self.w_mult = equalized_lr_init(
            self.weight, self.bias, self.scale_weights, self.lr_mult)

    def conv2d_forward(self, x, style, weight, bias):
        N, C0, H0, W0 = x.shape
        w = weight[None, :]  # OIkk -> NOIkk

        s = style[:, None, :, None, None]  # NI -> NOIkk
        w = w * s

        if self.demodulate:
            d = torch.rsqrt(w.pow(2).sum(dim=(2, 3, 4), keepdim=True) + 1e-8)
            w = w * d

        _, C1, _, Hk, Wk = w.shape
        w = w.view(N*C1, C0, Hk, Wk)

        x = x.view(1, N*C0, H0, W0)
        out = F.conv2d(x, w, None, self.stride, self.padding, self.dilation, groups=N)
        _, _, H1, W1 = out.shape
        out = out.view(N, C1, H1, W1)

        if bias is not None:
            out = out + bias[:, None, None]
        return out

    # noinspection PyMethodOverriding
    def forward(self, x, style):
        weight = self.weight * self.w_mult
        bias = self.bias
        if bias is not None:
            bias = bias * self.lr_mult
        return self.conv2d_forward(x, style, weight, bias)