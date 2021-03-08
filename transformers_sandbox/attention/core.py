# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/02_attention.core.ipynb (unless otherwise specified).

__all__ = ['MASK_VAL', 'SELF_ATTN_MASK_VAL', 'AttnInProj', 'AttnInProjV2', 'SharedQKAttnInProj',
           'ScaledDotProdAttention', 'make_mask', 'Attention', 'ChunkedDotProdAttention', 'ChunkedAttention',
           'AdditiveInProj', 'make_additive_attn_mask', 'AdditiveAttention', 'LSHAttention', 'LSHSelfAttention',
           'ReformerAttention']

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
from typing import Tuple, Callable #, Final TODO add this back when move to min-python == 3.8

from einops import rearrange, repeat

from ..core import *
from ..layers import *

from torch.utils.checkpoint import *

# Cell
MASK_VAL = -5e4
SELF_ATTN_MASK_VAL = -1e4

# Cell
class AttnInProj(Module):
    """Computes q, k, v from input x and [optional] context"""
    def __init__(self, d_model:int, bias:bool=False):
        self.to_q = nn.Linear(d_model, d_model, bias=bias)
        self.to_k = nn.Linear(d_model, d_model, bias=bias)
        self.to_v = nn.Linear(d_model, d_model, bias=bias)
    def forward(self, x, context=None):
        context = ifnone(context, x)
        q = self.to_q(x)
        k, v = self.to_k(context), self.to_v(context)
        return q, k, v

# Cell
class AttnInProjV2(Module):
    """Computes q, k, v from input x and [optional] context"""
    def __init__(self, d_model:int, bias:bool=False):
        self.to_q = nn.Linear(d_model, d_model, bias=bias)
        self.to_kv = nn.Linear(d_model, 2*d_model, bias=bias)
    def forward(self, x, context=None):
        context = ifnone(context, x)
        q = self.to_q(x)
        k, v = self.to_kv(context).chunk(2, -1)
        return q, k, v

# Cell
class SharedQKAttnInProj(Module):
    """Computes q, k, v from input x and [optional] context"""
    def __init__(self, d_model:int, bias:bool=False, normalize:bool=True):
        self.normalize = normalize
        self.to_qk = nn.Linear(d_model, d_model, bias=bias)
        self.to_v = nn.Linear(d_model, d_model, bias=bias)
    def forward(self, x, context=None):
        context = ifnone(context, x)
        q = self.to_qk(x)
        k = F.normalize(q, 2, dim=-1).type_as(q) if self.normalize else q
        v = self.to_v(context)
        return q, k, v

# Cell
#TODO make sure store_attention works
class ScaledDotProdAttention(Module):
    """
    Computes scaled dot-product attnetion given q, k, v
    """
    def __init__(self, d_model, n_heads, causal=False, dropout=0., shared_qk=False, store_attention:bool=False):
        store_attr()
        self.scale = (d_model//n_heads)**-0.5
        self.dropout = nn.Dropout(dropout)

    def forward(self, q, k, v, attn_mask=None):
        n, device = q.size(1), q.device
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.n_heads), (q, k, v))

        # classic dot-product attention
        dots = torch.einsum('bhid,bhjd->bhij', q*self.scale, k)

        if exists(attn_mask):
            dots.masked_fill_(~attn_mask, MASK_VAL)
            del attn_mask
        if self.shared_qk:
            m = torch.arange(n)
            dots[:, :, m, m] = SELF_ATTN_MASK_VAL
        if self.causal:
            i, j = torch.triu_indices(n, n, 1)
            dots[:,:,i,j] = MASK_VAL

        attn = F.softmax(dots, -1)
        if self.store_attention: self.attention = attn.detach().cpu()

        attn = self.dropout(attn)
        out = torch.einsum('b h i j, b h j d -> b h i d', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return out

# Cell
def make_mask(mask, context_mask, x, context):
    "Given (optionally None) input `mask` and `context_mask` produces attention mask of shape [bs, 1, input_sz, context_sz]"
    if any(map(exists, (mask, context_mask))):
        b, n, _, device = *x.size(), x.device
        q_mask = default(mask, lambda: torch.ones((b, n), device = device).bool())
        k_mask = q_mask if not exists(context) else context_mask
        k_mask = default(k_mask, lambda: torch.ones((b, context.shape[-2]), device = device).bool())

        q_mask = rearrange(q_mask, 'b i -> b () i ()')
        k_mask = rearrange(k_mask, 'b j -> b () () j')
        return q_mask * k_mask
    else: return None #attn_mask is None if both mask and context_mask are None

# Cell
class Attention(Module):
    """
    proto ver
    Configurable attention module
    Accepts custom in-projection and attention functions
    """
    def __init__(self,
                 d_model:int,
                 proj_func:Module=AttnInProjV2,
                 attn_func:Module=ScaledDotProdAttention,
                 make_mask:Callable=make_mask,
                 n_heads:int = 8,
                 causal:bool = False,
                 mask:Tensor = None,
                 dropout:float=0.1,
                 out_dropout:float=None,
                 bias:bool=False,
                 shared_qk:bool=False,
                 store_attention:bool=False,
                 **kwargs): #?? use kwargs for flexibility
        store_attr('causal, mask, n_heads, bias, shared_qk, make_mask')
        out_dropout = ifnone(out_dropout, dropout)
        #TODO: mb parse kwargs here
        self.in_proj = proj_func(d_model, bias=bias)
        self.attn = attn_func(d_model, n_heads, causal=causal,
                              dropout=dropout, shared_qk=shared_qk,
                              store_attention=store_attention, **kwargs)
        self.out_proj = nn.Linear(d_model, d_model, bias=bias)
        self.dropout = nn.Dropout(out_dropout)
        self._init()

    def forward(self, x, context = None, mask = None, context_mask = None):
        q, k, v = self.in_proj(x, context)

        attn_mask = self._make_attn_mask(mask, context_mask, x, context)
        out = self.attn(q, k, v, attn_mask)

        out = self.out_proj(out)
        return self.dropout(out)

    def _init(self):
        [nn.init.xavier_uniform_(w) for w in self.parameters() if w.dim()>1]
        if self.bias:
            [nn.init.constant_(b, 0) for b in self.parameters() if b.dim()==1]

    def _make_attn_mask(self, *args):
        return self.make_mask(*args)


# Cell
class _ChunkedAttnCptFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, run_function, preserve_rng_state, qc, k, v, i, csz, self, l, attn_mask):
        # check_backward_validity((qc,k,v))
        ctx.run_function = run_function
        ctx.preserve_rng_state = preserve_rng_state
        ctx.extra = (i, csz, self, l, attn_mask)
        if preserve_rng_state:
            ctx.fwd_cpu_state = torch.get_rng_state()
            # Don't eagerly initialize the cuda context by accident.
            # (If the user intends that the context is initialized later, within their
            # run_function, we SHOULD actually stash the cuda state here.  Unfortunately,
            # we have no way to anticipate this will happen before we run the function.)
            ctx.had_cuda_in_fwd = False
            if torch.cuda._initialized:
                ctx.had_cuda_in_fwd = True
                ctx.fwd_gpu_devices, ctx.fwd_gpu_states = get_device_states(qc,k,v)
        ctx.save_for_backward(qc, k, v)
        with torch.no_grad():
            outputs = run_function(qc, k, v, i, csz, self, l, attn_mask)
        return outputs

    @staticmethod
    def backward(ctx, *args):
        if not torch.autograd._is_checkpoint_valid():
            raise RuntimeError("Checkpointing is not compatible with .grad(), please use .backward() if possible")
        inputs = ctx.saved_tensors
        # Stash the surrounding rng state, and mimic the state that was
        # present at this time during forward.  Restore the surrounding state
        # when we're done.
        rng_devices = []
        if ctx.preserve_rng_state and ctx.had_cuda_in_fwd:
            rng_devices = ctx.fwd_gpu_devices
        with torch.random.fork_rng(devices=rng_devices, enabled=ctx.preserve_rng_state):
            if ctx.preserve_rng_state:
                torch.set_rng_state(ctx.fwd_cpu_state)
                if ctx.had_cuda_in_fwd:
                    set_device_states(ctx.fwd_gpu_devices, ctx.fwd_gpu_states)
            detached_inputs = detach_variable(inputs)
            with torch.enable_grad():
                outputs = ctx.run_function(*detached_inputs, *ctx.extra)

        if isinstance(outputs, torch.Tensor):
            outputs = (outputs,)
        torch.autograd.backward(outputs, args)
        grads = tuple(inp.grad if isinstance(inp, torch.Tensor) else inp
                      for inp in detached_inputs)
        return (None, None) + grads + tuple([None]*5)

# Cell
def _checkpoint(function, *args, **kwargs):
    "Same as torch.utils.checkpoint.checkpoint bu allows kwargs"
    preserve = kwargs.pop('preserve_rng_state', True)
    assert not kwargs
    return _ChunkedAttnCptFunction.apply(function, preserve, *args)

# Cell
def _chunked_attn(qc, k, v, i, csz, self, l, attn_mask):
    dots = torch.einsum('bhid, bhjd -> bhij', qc*self.scale, k)
    #pading masking
    if exists(attn_mask): raise NotImplementedError
    #shared qk masking
    if self.shared_qk:
        ii, jj = torch.arange(csz), torch.arange(i*csz, (i+1)*csz)
        dots[:,:,ii,jj] = SELF_ATTN_MASK_VAL
    #causal masking
    if self.causal:
        ii, jj = torch.triu_indices(csz, l, offset=i*csz+1)
        dots[:,:,ii,jj] = MASK_VAL

    attn = F.softmax(dots, -1)
    # if self.store_attention: self.attention[:,:,i,:] = attn.detach().cpu()
    attn = self.dropout(attn)
    return torch.einsum('bhij, bhjd -> bhid', attn, v)

# Cell
#TODO make store_attention work
class ChunkedDotProdAttention(Module):
    """
    Memory efficient and time inefficient attention for long seqences

    O(L) memory complexity if `n_chunks == seq_len` but uses python loop to compute
    attention for chunks of queries at a time
    """
    def __init__(self, d_model, n_heads, causal=False, dropout=0., shared_qk=False, n_chunks=1,
                 store_attention:bool=False):
        store_attr()
        self.scale = (d_model//n_heads)**-0.5
        self.dropout = nn.Dropout(dropout)

    def forward(self, q, k, v, attn_mask=None):
        b,n,d,l, device = *q.size(), k.size(1), q.device
        csz = math.ceil(n/self.n_chunks)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.n_heads), (q, k, v))
        qs = q.chunk(self.n_chunks, dim=2)
        outs = []
        if self.store_attention: self.attention = torch.zeros(b, d//self.n_heads, n,l)
        for i, qc in enumerate(qs):
            res = _checkpoint(_chunked_attn, qc, k, v, i, csz, self, l, attn_mask)
            outs.append(res)
        del attn_mask
        out = torch.cat(outs, dim=2)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return out


# Cell
class ChunkedAttention(Module):
    """
    Standard attention module using scaled dot-product attention
    """
    def __init__(self,
                 d_model:int,
                 n_heads:int = 8,
                 causal:bool = False,
                 mask:Tensor = None,
                 dropout:float=0.1,
                 out_dropout:float=None,
                 bias:bool=False,
                 shared_qk:bool=False,
                 n_chunks:int=1,
                 store_attention:bool=False):
        store_attr('causal, mask, n_heads, bias, shared_qk')
        out_dropout = ifnone(out_dropout, dropout)
        if shared_qk: self.in_proj = SharedQKAttnInProj(d_model, bias=bias)
        else: self.in_proj = AttnInProjV2(d_model, bias=bias)
        self.attn = ChunkedDotProdAttention(d_model, n_heads, causal=causal,
                                            dropout=dropout, shared_qk=shared_qk,
                                            store_attention=store_attention,
                                            n_chunks=n_chunks)
        self.out_proj = nn.Linear(d_model, d_model, bias=bias)
        self.dropout = nn.Dropout(out_dropout)
        self._init()

    def forward(self, x, context = None, mask = None, context_mask = None):
        q, k, v = self.in_proj(x, context)
        if self.shared_qk: k = F.normalize(k, 2, dim=-1).type_as(k)

        attn_mask = None#self._make_attn_mask(mask, context_mask, x, context)
        out = self.attn(q, k, v, attn_mask)

        out = self.out_proj(out)
        return self.dropout(out)

    def _init(self):
        [nn.init.xavier_uniform_(w) for w in self.parameters() if w.dim()>1]
        if self.bias:
            [nn.init.constant_(b, 0) for b in self.parameters() if b.dim()==1]

    def _make_attn_mask(self, mask, context_mask, x, context):
        if any(map(exists, (mask, context_mask))):
            b, n, _, device = *x.size(), x.device
            q_mask = default(mask, lambda: torch.ones((b, n), device = device).bool())
            k_mask = q_mask if not exists(context) else context_mask
            k_mask = default(k_mask, lambda: torch.ones((b, context.shape[-2]), device = device).bool())

            q_mask = rearrange(q_mask, 'b i -> b () i ()')
            k_mask = rearrange(k_mask, 'b j -> b () () j')
            return q_mask * k_mask
        else: return None #attn_mask is None if both mask and context_mask are None

# Cell
class AdditiveInProj(Module):
    """Computes q, k, v from input x and [optional] context"""
    def __init__(self, d_model:int, bias:bool=False):
        self.to_q = nn.Linear(d_model, d_model, bias=bias)
        self.to_k = nn.Linear(d_model, d_model, bias=bias)
        self.to_v = nn.Linear(d_model, d_model, bias=bias)
    def forward(self, x, context=None):
        b, _, d = x.size()
        context = ifnone(context, torch.empty(b, 0, d, dtype=x.dtype, device=x.device))
        kv_input = torch.cat([x, context], dim=-2)
        q = self.to_q(x)
        k, v = self.to_k(kv_input), self.to_v(kv_input)
        return q, k, v

# Cell
def make_additive_attn_mask(mask, context_mask, x, context):
    "Given (optionally None) input `mask` and `context_mask` produces attention mask of shape [bs, 1, input_sz, input_sz+context_sz]"
    b, n, _, device = *x.size(), x.device
    if any(map(exists, (mask, context_mask))):
        q_mask = default(mask, lambda: torch.ones((b, n), device=device).bool())
        self_mask = q_mask[:, None, :, None] * q_mask[:, None, None, :]
        if exists(context):
            k_mask = default(context_mask, lambda: torch.ones((b, context.shape[-2]), device=device).bool())
            cross_mask = q_mask[:, None, :, None] * k_mask[:, None, None, :]
        else: cross_mask = torch.empty(0, dtype=self_mask.dtype, device=device)
        return torch.cat([self_mask, cross_mask], dim=-1)
    else: return None #attn_mask is None if both mask and context_mask are None

# Cell
AdditiveAttention = partial(Attention, proj_func=AdditiveInProj, attn_func=ScaledDotProdAttention, make_mask=make_additive_attn_mask)

# Cell
class LSHAttention(Module):
    """
    LSH attention module:
    """
    def __init__( self,
                  dropout = 0.,                       # attention matrix dropout
                  bucket_size = 64,                   # at least 64 suggested in trax
                  n_hashes = 8,                       # papers sugests 8
                  causal = False,
                  allow_duplicate_attention = False,  # as in the paper
                  attend_across_buckets = False,      # as in the paper
                  drop_for_hash_rate = 0.0,           # unsure of default, not mentioned in paper
                  return_attn = False,
                  seed = None,                # for reproducibility
                  **kwargs):

        if dropout >= 1.0 or drop_for_hash_rate >=1.0:
            raise ValueError('Dropout rates must be lower than 1.')

        store_attr(but=['dropout', 'drop_for_hash_rate'])  # fastcore - store attibutes
        self.dropout = nn.Dropout(dropout)
        self.dropout_for_hash = nn.Dropout(drop_for_hash_rate)
        self._cache = {} # cache buckets for reversible network, required to make Reformer work at depth

    @cache_method_decorator('_cache', 'buckets', reexecute=True)
    def hash_vectors(self, n_buckets, vecs):
        # 0. We need an even number of buckets:
        assert n_buckets % 2 == 0

        # 1. account for the input shapes. vecs = [bs, sl, dim]
        batch_size, seqlen, dim = vecs.shape
        device = vecs.device
        rotations_shape = (dim, self.n_hashes, n_buckets // 2)

        # 2. Calculate hash bucket id via random rotations, concatenation and argmax
        # note: we copy rotations accross batch dimension (see exploration notebook for details).

        if self.seed is not None:
            torch.manual_seed(self.seed)

        random_rotations = repeat(torch.randn(rotations_shape,device=device),
                                  'd nh nb -> bs d nh nb', bs=batch_size)
        dropped_vecs = self.dropout_for_hash(vecs)

        rotated_vecs = torch.einsum('bsd,bdhn->bhsn',
                                    dropped_vecs,       # [bs, sl, dim]
                                    random_rotations)   # [bs, dim, n_hashes, n_buckets//2]
                                                        # rotated vecs: [bs, n_hashes, sl, n_buckets//2]

        rotated_vecs = torch.cat([rotated_vecs, -rotated_vecs], dim=-1) # [bs, n_hashes, sl, n_buckets]
        buckets = torch.argmax(rotated_vecs, dim=-1)                    # [bs, n_hashes, sl]

        # 3. Next we add offsets so that bucket numbers from different hashing rounds don't overlap.
        # We also reshape the buckets so that each hash round is concatenated along the -1 dim
        offsets = torch.arange(self.n_hashes,device=device)                              # list of [0,1,2,..n_hashes-1]
        offsets = rearrange(offsets * n_buckets, 'nh -> 1 nh 1')        # [1, n_hashes, 1]
        buckets = rearrange(buckets+offsets, 'bs nh sl -> bs (nh sl)')  # [bs, (n_hashes*sl)]
        return buckets

    def forward(self, q, k, v, attn_mask = None, **kwargs):
        batch_size, seqlen, dim, device = *q.shape, q.device

        # caching
        is_reverse = kwargs.pop('_reverse', False)
        depth = kwargs.pop('_depth', None)

        # We will have an even number of buckets, and our attention chunks needs to fit completely within a seqlen
        assert seqlen % (self.bucket_size * 2) == 0, f'Sequence length ({seqlen}) needs to be divisible by target bucket size  x 2 - {self.bucket_size * 2}'

        # get the hash buckets for our q,k input vectors
        n_buckets = seqlen // self.bucket_size
        buckets = self.hash_vectors(n_buckets, q, key_namespace=depth, fetch=is_reverse, set_cache=self.training)

        # We use the same vector as both a query and a key.
        assert int(buckets.shape[1]) == self.n_hashes * seqlen

        # Create an index that reflexts both bucket id and sequence id. This let's us sort q, k according
        # to both simultaneously. Repeated across the batch dimension.
        ticker = repeat(torch.arange((self.n_hashes * seqlen),device=device), 'l -> bs l', bs=batch_size)
        buckets_and_t = seqlen * buckets + (ticker % seqlen)
        buckets_and_t = buckets_and_t.detach()                # [bs, seqlen*n_hashes]

        # Hash-based sort ("s" at the start of variable names means "sorted")
        sbuckets_and_t, sticker = sort_key_val(buckets_and_t, ticker, dim=-1)  # [bs, seqlen*n_hashes]
        _, undo_sort = sticker.sort(dim=-1)                                    # indexes to undo sortings
        del ticker

        sbuckets_and_t = sbuckets_and_t.detach()   # no need to store gradiens for indexes
        sticker = sticker.detach()
        undo_sort = undo_sort.detach()

        st = (sticker % seqlen)            # index of [0..seqlen-1] for each hash round
        sq = batched_index_select(q, st)   # get the sorted q, [bs, seqlen*n_hashes, dim]
        sk = batched_index_select(k, st)   # get the sorted k, [bs, seqlen*n_hashes, dim]
        sv = batched_index_select(v, st)   # get the sorted v, [bs, seqlen*n_hashes, dim]

        # Reshape to include a n_chunks axis.
        n_chunks = self.n_hashes * n_buckets
        bq_t = bkv_t = rearrange(st, 'bs (n s) -> bs n s', n=n_chunks) # [bs, n_chunks, chunk_size]
        bq = rearrange(sq, 'bs (n s) d -> bs n s d', n=n_chunks)       # [bs, n_chunks, chunk_size, dim]
        bk = rearrange(sk, 'bs (n s) d -> bs n s d', n=n_chunks)       # [bs, n_chunks, chunk_size, dim]
        bv = rearrange(sv, 'bs (n s) d -> bs n s d', n=n_chunks)       # [bs, n_chunks, chunk_size, dim]

        # Hashing operates on unit-length vectors. Unnormalized query vectors are
        # fine because they effectively provide a learnable temperature for the
        # attention softmax, but normalizing keys is needed so that similarity for
        # the purposes of attention correctly corresponds to hash locality.
        bk = F.normalize(bk, p=2, dim=-1)

        # Allow each chunk to attend within itself, and also one chunk back. Chunk
        # boundaries might occur in the middle of a sequence of items from the
        # same bucket, so this increases the chances of attending to relevant items.
        # Note: no look_back for queries

        bk = look_one_back(bk)        # [bs, n_chunks, chunk_size*2, dim]
        bv = look_one_back(bv)        # [bs, n_chunks, chunk_size*2, dim]
        bkv_t = look_one_back(bkv_t)

        # Dot-product attention.
        dots = torch.einsum('bnsd,bnzd->bnsz',
                    bq,                  # [bs, n_chunks, chunk_size, dim]
                    bk                   # [bs, n_chunks, chunk_size*2, dim]
                   ) * (dim ** -0.5)     # dots: [bs, n_chunks, chunk_size, chunk_size*2]
        masked_value = max_neg_value(dots)

        # Input mask for padding in variable lengthed sequences
        if attn_mask is not None:
            attn_mask = F.pad(attn_mask, (0, seqlen - attn_mask.shape[1]), value=True)
            mq = attn_mask.gather(1, st).reshape((batch_size, n_chunks, -1))
            mkv = look_one_back(mq)
            mask = mq[:, :, :, None] * mkv[:, :, None, :]
            dots.masked_fill_(~mask, masked_value)
            del mask

        # Causal masking
        if self.causal:
            mask = bq_t[:, :, :, None] < bkv_t[:, :, None, :]
            dots.masked_fill_(mask, masked_value)
            del mask

        # Mask out attention to self except when no other targets are available.
        self_mask = bq_t[:, :, :, None] == bkv_t[:, :, None, :]
        dots.masked_fill_(self_mask, SELF_ATTN_MASK_VAL)
        del self_mask

        # Mask out attention to other hash buckets.
        if not self.attend_across_buckets:
            bq_buckets = bkv_buckets = torch.reshape(sbuckets_and_t // seqlen, (batch_size, n_chunks, -1))
            bkv_buckets = look_one_back(bkv_buckets)
            bucket_mask = bq_buckets[:, :, :, None] != bkv_buckets[:, :, None, :]
            dots.masked_fill_(bucket_mask, masked_value)
            del bucket_mask

        # Don't double-count query-key pairs across multiple rounds of hashing.
        # There are two possible strategies here. (1) The default is to count how
        # many times a query-key pair is repeated, and to lower its log-prob
        # correspondingly at each repetition.

        if not self.allow_duplicate_attention:
            locs1 = undo_sort // bq_t.shape[-1]
            locs2 = (locs1 + 1) % n_chunks
            if not self.attend_across_buckets:
                locs1 = buckets * n_chunks + locs1
                locs2 = buckets * n_chunks + locs2
            locs = torch.cat([
                torch.reshape(locs1, (batch_size, self.n_hashes, seqlen)),
                torch.reshape(locs2, (batch_size, self.n_hashes, seqlen)),
            ], 1).permute((0, 2, 1))

            slocs = batched_index_select(locs, st)
            b_locs = torch.reshape(slocs, (batch_size, n_chunks, -1, 2 * self.n_hashes))

            b_locs1 = b_locs[:, :, :, None, :self.n_hashes]

            bq_locs = b_locs1.expand(b_locs.shape[:3] + (2, self.n_hashes))
            bq_locs = torch.reshape(bq_locs, b_locs.shape)
            bkv_locs = look_one_back(b_locs)

            dup_counts = (bq_locs[:, :, :, None, :] == bkv_locs[:, :, None, :, :])
            # for memory considerations, chunk summation of last dimension for counting duplicates
            dup_counts = chunked_sum(dup_counts, chunks=(self.n_hashes * batch_size))
            dup_counts = dup_counts.detach()
            assert dup_counts.shape == dots.shape
            dots = dots - torch.log(dup_counts + 1e-9)
            del dup_counts

        # Softmax.
        dots_logsumexp = torch.logsumexp(dots, dim=-1, keepdim=True)
        dots = torch.exp(dots - dots_logsumexp).type_as(dots)
        dropped_dots = self.dropout(dots)

        # calculate self-attention (attn * values)
        bo = torch.einsum('bnsz,bnzd->bnsd',
                          dropped_dots,      # [bs, n_chunks, chunk_size, chunk_size*2]
                          bv)                # [bs, n_chunks, chunk_size*2, dim]
                                             # bo: [bs, n_chunks, chunk_size, dim]

        # unchunk, unsort and reshape self-attention
        so = rearrange(bo, 'b n s d -> b (n s) d')                     # [bs, seqlen*n_hashes, dim]
        o = batched_index_select(so, undo_sort)                        # [bs, seqlen*n_hashes, dim]
        o = rearrange(o, 'b (nh sl) d -> b nh sl d', nh=self.n_hashes) # [bs, n_hashes, seqlen, dim]

        # unchunk, unsort and reshape logits
        slogits = rearrange(dots_logsumexp, 'bs n s 1 -> bs (n s)')              # [bs, seqlen*n_hashes]
        logits = slogits.gather(1, undo_sort)                                    # [bs, seqlen*n_hashes]
        logits = rearrange(logits, 'bs (nr sl) -> bs nr sl 1', nr=self.n_hashes) # [bs, n_hashes, seqlen, 1]

        # average probabilites across hash rounds (dim 1) and get weighted attention
        probs = torch.exp(logits - torch.logsumexp(logits, dim=1, keepdim=True)) # [bs, n_rounds, seqlen, 1]
        out = torch.sum(o * probs, dim=1)                                        # [bs, seqlen, dim]

        # return unsorted attention weights - empty otherwise
        attn = torch.empty(0, device=device)
        if self.return_attn:
            attn_unsort = ((bq_t * seqlen)[:, :, :, None] + bkv_t[:, :, None, :])
            attn_unsort = attn_unsort.view(batch_size * self.n_hashes, -1).long()
            unsorted_dots = torch.zeros(batch_size * self.n_hashes, seqlen * seqlen, device=device)
            unsorted_dots.scatter_add_(1, attn_unsort, dots.view_as(attn_unsort))
            del attn_unsort
            unsorted_dots = unsorted_dots.reshape(batch_size, self.n_hashes, seqlen, seqlen)
            attn = torch.sum(unsorted_dots * probs, dim=1)

        # return output, attention matrix, and bucket distribution
        return out, attn, buckets

# Cell
class LSHSelfAttention(Module):
    def __init__(self,
                 d_model,
                 n_heads = 8,
                 bucket_size = 64,                    # reccomended default from paper/lucid
                 n_hashes = 8,                        # reccomended default from paper/lucid
                 causal = False,
                 bias:bool=False,
                 attend_across_buckets = False,
                 allow_duplicate_attention = False,   # Penalize multiple qk-v pairs in same attention chunk or not
                 return_attn = False,                 # Not implemented yet
                 seed = None,                 # for reproducibility
                 dropout = 0.,                        # dropout for LSH-Attention attention matrix
                 dropout_hash = 0.,                   # dropout for hashing algorithm
                 out_dropout = 0.):                   # a final dropout on output

        assert (d_model % n_heads) == 0, 'dimensions must be divisible by number of heads'
        store_attr('n_heads, bias')
        self.in_proj = SharedQKAttnInProj(d_model, bias=bias)
        self.out_proj = nn.Linear(d_model, d_model, bias=bias)
        self.lsh_attn = LSHAttention(bucket_size=bucket_size,
                                     n_hashes=n_hashes,
                                     causal=causal,
                                     attend_across_buckets = attend_across_buckets,
                                     allow_duplicate_attention = allow_duplicate_attention,
                                     return_attn = return_attn,
                                     dropout = dropout,
                                     dropout_hash = dropout_hash,
                                     seed=seed)
        self.out_dropout = nn.Dropout(out_dropout)
        self._init()

    def forward(self, x, context = None, mask = None, context_mask = None, **kwargs):
        device, dtype = x.device, x.dtype
        bs, sl, d_model = x.shape

        # project keys, queries and valuess
        q, k, v = self.in_proj(x, context)     # [bs, sl(+csl), d_model]

        # split off head dimension for q, k and v. Resulting shapes are: [nh, bs, sl, dim_head]
        q, k, v = map(lambda t: rearrange(t, 'bs sl (nh dh) -> nh bs sl dh', nh=self.n_heads), (q, k, v))

        #create masks:
        attn_mask = self._make_attn_mask(mask, context_mask, x, context)

        # run lsh per head (iterate through 0th dim i.e. the n_head dim), concatenate and rearrange
        lsh_results = L([self.lsh_attn(q_h, k_h, v_h, attn_mask, **kwargs) for q_h, k_h, v_h in zip(q, k, v)])
        out = lsh_results.itemgot(0)                                   # split tuple (output, attn, buckets)
        out = torch.cat([head for head in out], dim=0)                 # concatenate [n_heads*bs, sl, dh]
        out = rearrange(out, '(nh bs) sl dh -> bs sl (nh dh)', bs=bs)  # [bs, sl, dim_heads] (dim_heads = head_dim * n_heads)

        # pass through final feed forward and maybe dropout
        out = self.out_proj(out)                                            # [bs, sl, dim]
        return self.out_dropout(out)

    # Note: masks are reused per head and should be of size bs, sl
    def _make_attn_mask(self, mask, context_mask, x, context):
        if any(map(exists, (mask, context_mask))):
            context_lenght = context.shape[-2] if context is not None else 0 # context.shape[-2] is sl dim (0 if none)
            default_mask = torch.tensor([True], device=x.device)
            i_mask = default(mask, default_mask.expand(bs, sl))
            c_mask = default(context_mask, default_mask.expand(bs, context_lenght))
            attn_mask = torch.cat((i_mask, c_mask), dim=1)
            return attn_mask
        else: return None #attn_mask is None if both mask and context_mask are None

    def _init(self):
        [nn.init.xavier_uniform_(w) for w in self.parameters() if w.dim()>1]
        if self.bias:
            [nn.init.constant_(b, 0) for b in self.parameters() if b.dim()==1]

# Cell
class ReformerAttention(Module):
    """
    Reformer attention container. Take on making it switchable on the fly.

    Switch between FullSharedQKAttention and LSHAttention.
    """
    def __init__(self,
                 d_model:int,
                 n_heads:int = 8,
                 causal:bool = False,
                 attn_mask:Tensor = None,
                 dropout:float=0.1,
                 out_dropout:float=None,
                 bias:bool=False,
                 store_attention:bool=False,
                 use_lsh:bool = True,
                 n_hashes:int = 8,
                 bucket_size:int = 64,
                 seed:int=None):
        store_attr('causal, attn_mask, n_heads, bias, use_lsh')

        out_dropout = ifnone(out_dropout, dropout)
        self.in_proj = SharedQKAttnInProj(d_model, bias=bias)

        self.lsh_attn = LSHAttention(bucket_size=bucket_size, n_hashes=n_hashes, causal=causal,
                                     return_attn=store_attention, dropout=dropout,
                                     seed=seed)
        self.full_attn = ScaledDotProdAttention(d_model, n_heads, causal=causal,
                                                dropout=dropout, shared_qk=True,
                                                store_attention=store_attention)

        self.out_proj = nn.Linear(d_model, d_model, bias=bias)
        self.dropout = nn.Dropout(out_dropout)
        self._init()

    def forward(self, x, context=None, mask=None, context_mask=None, **kwargs):
        #doesn't support cross attention for now?
        assert context is None, "sharedQK doesn't support cross attention yet"
        q, k, v = self.in_proj(x)
        # use LSH
        attn_mask = self._make_attn_mask(mask, context_mask, x, context)
        if self.use_lsh:
            bs = x.size(0)
            q, k, v = map(lambda t: rearrange(t, 'bs sl (nh dh) -> nh bs sl dh', nh=self.n_heads), (q, k, v))
            # run lsh per head (iterate through 0th dim i.e. the n_head dim), concatenate and rearrange
            # Note: masks are reused per head
            lsh_results = L([self.lsh_attn(q_h, k_h, v_h, attn_mask, **kwargs) for q_h, k_h, v_h in zip(q, k, v)])
            out = lsh_results.itemgot(0)                                   # split tuple (output, attn, buckets)
            out = torch.cat([head for head in out], dim=0)                 # concatenate [n_heads*bs, sl, dh]
            out = rearrange(out, '(nh bs) sl dh -> bs sl (nh dh)', bs=bs)  # [bs, sl, dim_heads] (dim_heads = head_dim * n_heads)
        # use full attention
        else:
            out = self.full_attn(q, k, v, attn_mask)

        out = self.out_proj(out)
        return self.dropout(out)

    def _init(self):
        [nn.init.xavier_uniform_(w) for w in self.parameters() if w.dim()>1]
        if self.bias:
            [nn.init.constant_(b, 0) for b in self.parameters() if b.dim()==1]
    #TODO: add attn_mask generation
    def _make_attn_mask(self, mask, context_mask, x, context):
        b, n, _, device = *x.size(), x.device
        if any(map(exists, (mask, context_mask))):
            q_mask = default(mask, lambda: torch.ones((b, n), device=device).bool())
            self_mask = q_mask[:, None, :, None] * q_mask[:, None, None, :]
            if exists(context):
                k_mask = default(context_mask, lambda: torch.ones((b, context.shape[-2]), device=device).bool())
                cross_mask = q_mask[:, None, :, None] * k_mask[:, None, None, :]
            else: cross_mask = torch.empty(0, dtype=self_mask.dtype, device=device)
            return torch.cat([self_mask, cross_mask], dim=-1)
        else: return None #attn_mask is None if both mask and context_mask are None