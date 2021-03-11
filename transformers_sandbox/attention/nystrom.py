# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/02b_attention.nystrom.ipynb (unless otherwise specified).

__all__ = ['reshape2bhnd', 'reshape2bnd', 'NystromAttention']

# Cell
import torch
from torch import nn, einsum
import torch.nn.functional as F
from fastai.basics import *

from functools import partial, reduce
from inspect import isfunction
from operator import mul
from copy import deepcopy
import math
from torch import Tensor
from typing import Tuple

from einops import rearrange, repeat

from ..core import *
from ..layers import *
from .core import *

# Cell
class reshape2bhnd:
    "b n (h d) -> b h n d"
    def __init__(self, h=8): self.h = h

    def __call__(self, x):
        b, n, d = x.size()
        return x.view(b, n, self.h, d//self.h).transpose(1,2)

def reshape2bnd(x):
    b, h, n, d = x.size()
    return x.transpose(1,2).contiguous().view(b, n, -1)

# Cell
class NystromAttention(Module):
    """Computes attention using nystrom aproximation based approach"""
    def __init__(self, d_model, n_heads=8, causal=False, n_landmarks=64,
                 store_attention:bool=False, use_conv=False, dropout=0.,
                 pinv_n_iter=6, conv_kernel_size=33, **kwargs):
        store_attr()
        self.scale = (d_model//n_heads)**-0.5
        if use_conv:
            self.conv = nn.Conv2d(n_heads, n_heads,
                                  kernel_size=(conv_kernel_size, 1),
                                  padding=(conv_kernel_size//2, 0),
                                  groups=n_heads, bias=False)

    def forward(self, q, k, v, attn_mask=None):
        bs, n, d, h, l = *q.size(), self.n_heads, self.n_landmarks
        dh = d//h
        #reshape = partial(rearrange, pattern='b n (h d) -> b h n d', h=self.n_heads)
        q, k, v = map(reshape2bhnd(h), (q,k,v))

        if n <= self.n_landmarks:
            #?? mb do this with threshold instead
            dots = F.softmax(einsum('...nd, ...md -> ...nm', q*scale, k), dim=-1)
            if exists(attn_mask):
                dots.masked_fill_(~attn_mask, MASK_VAL)
                del attn_mask
            out = einsum('...nm, ...md -> ...nd', dots, v)

        ql = torch.reshape(q, (bs, h, l, -1, dh)).mean(dim=-2)
        kl = torch.reshape(k, (bs, h, l, -1, dh)).mean(dim=-2)

        f = F.softmax(einsum('...nd, ...md -> ...nm', q *self.scale, kl), dim=-1)
        b = F.softmax(einsum('...md, ...nd -> ...mn', ql*self.scale, k ), dim=-1)
        a = F.softmax(einsum('...ld, ...md -> ...lm', ql*self.scale, kl), dim=-1)
        a = iter_pinv(a)

        out = einsum('...nm, ...md -> ...nd',
                     einsum('...nl, ...lm -> ...nm', f, a),
                     einsum('...mn, ...nd -> ...md', b, v))

        if self.use_conv:
            out += self.conv(v)
        return reshape2bnd(out)