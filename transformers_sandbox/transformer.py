# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/03_models.transformer.ipynb (unless otherwise specified).

__all__ = ['LMMixin', 'EncDecMixin', 'TransformerEncoderBlock', 'TransformerEncoder', 'TransformerDecoderBlock',
           'TransformerDecoderBlockV2', 'TransformerDecoder', 'TransformerLM', 'transformer_lm_splits', 'Transformer',
           'transformer_splits', 'LowMemEncoderBlock', 'LowMemEncoder', 'ChunkedTransformerLM']

# Cell
from fastai.basics import *
from .core import *
from .core import _sampler
from .layers import *
from .attention.all import *

# Cell
class LMMixin:
    "Mixin for language models"
    #TODO maybe refactor
    @torch.no_grad()
    def generate(self, inp,
                max_len=50,
                temperature=1.,
                method = 'top_k',
                top_k = 20,
                top_p = 0.9,
                early_stopping=False, #need eos_idx to work
                eos_idx=None):
        self.to(inp.device) #TODO test for potential problems
        self.eval()
        thresh = top_k if method=='top_k' else top_p
        sampler = _sampler[method]
        inp = expand_dim1(inp)
        b, t = inp.shape
        out = inp
        for _ in range(max_len):
            x = out[:, -self.max_seq_len:]

            logits = self(x)[:, -1, :]
            if method == 'greedy':
                sample = sampler(logits)
            else:
                filtered_logits = sampler(logits, thresh)
                probs = F.softmax(filtered_logits / temperature, dim=-1)
                sample = torch.multinomial(probs, 1)

            out = torch.cat((out, sample), dim=-1)

            if early_stopping and (sample == eos_idx).all():
                break
        # out = out[:, t:]
        return out

    def store_attention(self, layer_ids=None):
        #defaults to storing attention for all layers
        layer_ids = default(layer_ids, list(range(self.n_layers)))
        for module in self.children():
            if isinstance(module, (TransformerEncoder, TransformerDecoder)):
                for i, l in enumerate(module.layers):
                    if i in layer_ids:
                        for m in l.modules():
                            if isinstance(m, (ScaledDotProdAttention)):
                                m.store_attention = True
    def get_attention_matrix(self):
        res = []
        for m in self.modules():
            if isinstance(m, (ScaledDotProdAttention)):
                attention = getattr(m, 'attention', None)
                if attention is not None:
                    res.append(attention)
                # reset stored attention
                m.attention = None
                m.store_attention = False
        return res

    @classmethod
    def from_config(cls, config):
        return cls(**config.dict())

# Cell
class EncDecMixin:
    "Mixin for encoder-decoder models"
    #TODO add beam search and refactor
    @torch.no_grad()
    def generate(self, src,
                src_mask=None,
                max_len=50,
                temperature=1.,
                method = 'top_k',
                top_k = 20,
                top_p = 0.9,
                early_stopping=False,
                bos_idx=2, # TODO change to match future usecases
                eos_idx=None):
        self.to(src.device) #TODO test for potential problems
        self.eval()
        thresh = top_k if method=='top_k' else top_p
        sampler = _sampler[method]
        src = expand_dim1(src)
        bs = src.size(0)
        inp = src.new_full((bs, 1), bos_idx) #start with bos tokens
        src_mask = default(src_mask, self.get_padding_mask(src))
        enc = self.encoder(self.enc_emb(src), mask = src_mask)
        out = inp
        for _ in range(max_len):
            x = out[:, -self.max_seq_len:]
            dec = self.decoder(self.dec_emb(out), context=enc)
            logits = self.proj(dec)[:, -1, :]
            if method == 'greedy':
                sample = sampler(logits)
            else:
                filtered_logits = sampler(logits, thresh)
                probs = F.softmax(filtered_logits / temperature, dim=-1)
                sample = torch.multinomial(probs, 1)

            out = torch.cat((out, sample), dim=-1)

            if (early_stopping and
                ((sample == eos_idx).all() or
                (sample == self.pad_idx).all())):
                break
        #TODO mb output cleanup
        return out

    def store_attention(self, layer_ids=None, store_encoder=False, store_decoder=True):
        #defaults to storing attention for all layers
        layer_ids = default(layer_ids, list(range(self.n_enc_layers)))
        for module in self.children():
            if isinstance(module, TransformerEncoder) and store_encoder:
                for i, l in enumerate(module.layers):
                    if i in layer_ids:
                        for m in l.modules():
                            if isinstance(m, (ScaledDotProdAttention)):
                                m.store_attention = True
            elif isinstance(module, TransformerDecoder) and store_encoder:
                for i, l in enumerate(module.layers):
                    if i in layer_ids:
                        for m in l.modules():
                            if isinstance(m, (ScaledDotProdAttention)):
                                m.store_attention = True
    #TODO mb separate encoder and decoder attention
    def get_attention_matrix(self, get_encoder=False, get_decoder=True):
        res = []
        if get_encoder:
            for m in self.encoder.modules():
                if isinstance(m, (ScaledDotProdAttention)):
                    attention = getattr(m, 'attention', None)
                    if attention is not None:
                        res.append(attention)
                    # reset stored attention
                    m.attention = None
                    m.store_attention = False
        if get_decoder:
            for m in self.decoder.modules():
                if isinstance(m, (ScaledDotProdAttention)):
                    attention = getattr(m, 'attention', None)
                    if attention is not None:
                        res.append(attention)
                    # reset stored attention
                    m.attention = None
                    m.store_attention = False
        return res

# Cell
class TransformerEncoderBlock(Module):
    """
    Bacis transformer encoder block. Consists of multi-head attention and positional
    feedforward layers
    """
    def __init__(self,
                 d_model:int,
                 n_heads:int = 8,
                 d_ff:int = None,
                 attn_dropout:float = 0.1,
                 ff_dropout:float = 0.1,
                 causal:bool = False,
                 attn_bias:bool = False,
                 prenorm:bool=False,
                 shared_qk:bool=False):
        store_attr('attn_dropout') # mb separate argument attn_post_dropout
        _proj = SharedQKAttnInProj if shared_qk else AttnInProjV2
        if prenorm:
            self.attn = Residual(PreNorm(d_model, Attention(d_model, n_heads=n_heads, proj_func=_proj, causal=causal, dropout=attn_dropout, bias=attn_bias, shared_qk=shared_qk)))
            self.ff = Residual(PreNorm(d_model, FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))
        else:
            self.attn = PostNorm(d_model, Residual(Attention(d_model, n_heads=n_heads, proj_func=_proj, causal=causal, dropout=attn_dropout, bias=attn_bias, shared_qk=shared_qk)))
            self.ff = PostNorm(d_model, Residual(FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))

    def forward(self, x, mask=None): #? more args
        out = self.attn(x, mask=mask)
        return self.ff(out)

# Cell
class TransformerEncoder(Module):
    """Stack of TransformerEncoderBlocks"""
    def __init__(self,
                 d_model,
                 n_layers=6,
                 n_heads=8,
                 d_ff=None,
                 ff_dropout=0.1,
                 attn_dropout=0.1,
                 attn_bias=False,
                 causal=False,
                 prenorm=False,
                 shared_qk:bool=False,
                 final_norm=None):
        store_attr('d_model')
        self.layers = nn.ModuleList([])
        for _ in range(n_layers):
            self.layers.append(TransformerEncoderBlock(d_model, n_heads, causal=causal,
                                    d_ff=d_ff, attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                    prenorm=prenorm, attn_bias=attn_bias, shared_qk=shared_qk))
        self.norm = None if final_norm is None else final_norm(d_model)

    def forward(self, x, mask=None):
        for layer in self.layers: x = layer(x, mask=mask)
        if self.norm is not None: x = self.norm(x)
        return x

# Cell
class TransformerDecoderBlock(Module):
    """
    Standart transformer decoder block. Consist of self-attention, encoder-decoder attention
    and positiona feed-forward alyers
    """
    def __init__(self,
                 d_model,
                 n_heads = 8,
                 d_ff = None,
                 attn_dropout = 0.1,
                 ff_dropout=0.1,
                 mask = None ,
                 attn_bias=False,
                 prenorm=False):
        # mb separate argument attn_post_dropout
        if prenorm:
            self.attn = Residual(PreNorm(d_model, Attention(d_model, n_heads=n_heads, causal=True, dropout=attn_dropout, bias=attn_bias)))
            self.cross = Residual(PreNorm(d_model, Attention(d_model, n_heads=n_heads, causal=False, dropout=attn_dropout, bias=attn_bias)))
            self.ff = Residual(PreNorm(d_model, FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))
        else:
            self.attn = PostNorm(d_model, Residual(Attention(d_model, n_heads=n_heads, causal=True, dropout=attn_dropout, bias=attn_bias)))
            self.cross = PostNorm(d_model, Residual(Attention(d_model, n_heads=n_heads, causal=False, dropout=attn_dropout, bias=attn_bias)))
            self.ff = PostNorm(d_model, Residual(FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))

    def forward(self, x, context, mask=None, context_mask=None):
        out = self.attn(x, mask=mask)
        out = self.cross(out, context, mask=mask, context_mask=context_mask)
        return self.ff(out)

# Cell
class TransformerDecoderBlockV2(Module):
    """Transformer decoder block using additive attention layer instead of self-attention
    followed by cross-attention"""
    def __init__(self,
                 d_model,
                 n_heads = 8,
                 mask = None,
                 d_ff=None,
                 attn_dropout=0.1,
                 ff_dropout=0.1,
                 attn_bias=False,
                 prenorm=False):
        if prenorm:
            self.attn = Residual(PreNorm(d_model, AdditiveAttention(d_model, n_heads=n_heads, causal=True, dropout=attn_dropout, bias=attn_bias)))
            self.ff = Residual(PreNorm(d_model, FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))
        else:
            self.attn = PostNorm(d_model, Residual(AdditiveAttention(d_model, n_heads=n_heads, causal=True, dropout=attn_dropout, bias=attn_bias)))
            self.ff = PostNorm(d_model, Residual(FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))

    def forward(self, x, context, mask=None, context_mask=None):
        out = self.attn(x, context, mask=mask, context_mask=context_mask)
        out = self.ff(out)
        return out

# Cell
class TransformerDecoder(Module):
    """Stack of TransformerDecoder layers"""
    def __init__(self,
                 d_model,
                 n_layers=6,
                 n_heads=8,
                 d_ff=None,
                 attn_dropout=0.1,
                 ff_dropout=0.1,
                 prenorm=False,
                 comb_attn=False,
                 attn_bias=False,
                 final_norm=None):
        store_attr('d_model')
        #TODO(Arto) refactor
        block = TransformerDecoderBlockV2 if comb_attn else TransformerDecoderBlock
        self.layers = nn.ModuleList([])
        for _ in range(n_layers):
            self.layers.append(block(d_model, n_heads, d_ff=d_ff, attn_dropout=attn_dropout,
                                     ff_dropout=ff_dropout, prenorm=prenorm, attn_bias=attn_bias))
        self.norm = None if final_norm is None else final_norm(d_model)

    def forward(self, x, context, mask=None, context_mask=None):
        for layer in self.layers: x = layer(x, context, mask, context_mask)
        if self.norm is not None: x = self.norm(x)
        return x

# Cell
class TransformerLM(Module, LMMixin):
    """
    Basic Transformer for language modelling

    Parameters:
        * vocab_sz: int
        * d_model: int - inner dimension of the model
        * n_layers: int (default: 6)
        * n_heads: int (default: 8)
        * d_ff: int - inner dimension of the pointwise FeedForward net, if None defaults to 4*d_model
        * attn_dropout: float - attention dropout
        * ff_dropout: float - feed-forward dropout
        * emb_dropout: float - embedding dropout
        * causal: bool (default: True) - if True does causal masking automatically
        * max_seq_len: int (default: 512)
        * tie_weights: bool - if True target embedding weights are used for computation output projection
        * prenorm: bool - wether to use PreNorm or PostNorm
        * attn_bias: bool - wether to allow biases in attention projection layers
        * pad_idx: int - padding token id, required for autogeneration of padding mask
        * pos_enc: str from {'absolute', 'fixed', 'axial'} - type of positional encoding to use
        * axial_shape: tuple - [optional] should be factors of max_seq_len
        * axial_emb_dims: tuple - [optional] axial embedding components, should sum to d_model
    Inputs:
        * x - input ids, shape [bs, sl]
        * mask - optional boolean mask, shape [bs, sl]
    Returns:
        * logits - target token logits, shape [bs, sl, vocab_sz]
    """
    def __init__(self,
                 vocab_sz:int,
                 d_model:int,
                 n_layers:int=6,
                 n_heads:int=8,
                 d_ff:int=None,
                 attn_dropout:float=0.1,
                 ff_dropout:float=0.1,
                 emb_dropout:float=0.1,
                 tie_weights:bool=True,
                 causal:bool=True,
                 pos_enc:str='absolute',
                 max_seq_len:int=512,
                 axial_shape:tuple=None,
                 axial_emb_dims:tuple=None,
                 pad_idx:int=None,
                 prenorm:bool=False,
                 attn_bias:bool=False,
                 shared_qk:bool=False):
        store_attr()
        self.emb = TransformerEmbedding(vocab_sz, d_model, max_seq_len, dropout=emb_dropout,
                                        pos_enc=pos_enc, axial_shape=axial_shape,
                                        axial_emb_dims=axial_emb_dims)
        final_norm = nn.LayerNorm if prenorm else None
        self.encoder = TransformerEncoder(d_model, n_layers, n_heads, causal=causal, d_ff=d_ff,
                                          attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                          prenorm=prenorm, attn_bias=attn_bias,
                                          shared_qk=shared_qk, final_norm=final_norm)
        self.proj = nn.Linear(d_model, vocab_sz)
        if tie_weights: self.proj.weight = self.emb.emb.weight

    def forward(self, x, mask=None):
        x = self.emb(x)
        x = self.encoder(x, mask=mask)
        return self.proj(x)


# Cell
def transformer_lm_splits(model):
    "Splits TransformerLM `model` into groups for differential learning rates."
    groups = L([model.emb] + [l for l in model.encoder.layers] + [model.proj])
    return groups.map(params)

# Cell
#TODO test weight tying
# Note on weight tying: it's done like here in fastai AWD_LSTM model
# Lucidrains does it with custom MatrixMultiply module https://github.com/lucidrains/reformer-pytorch/blob/master/reformer_pytorch/reformer_pytorch.py#L106
#TODO: update docstrings
class Transformer(Module, EncDecMixin):
    """
    Basic Transformer Encoder-Decoder model
    Parameters:
        * enc_vocab_sz: int - source vocab size
        * dec_vocab_sz: int - target vocab size
        * d_model: int - inner dimension of the model
        * n_enc_layers: int (default: 6)
        * n_dec_layers: int (default: 6)
        * heads: int (default: 8)
        * d_ff: int - inner dimension of the pointwise FeedForward net, if None defaults to 4*d_model
        * attn_dropout: float - attention dropout
        * ff_dropout: float - feed-forward dropout
        * emb_dropout: float - embedding dropout
        * max_seq_len: int (default: 512)
        * prenorm: bool - whether to use PreNorm or PostNorm
        * attn_bias: bool - whether to allow biases in attention projection layers
        * pad_idx: int - padding token id, if pad_idx is provided, and no mask/context_mask are
                passed to forward method will be used to generate padding masks
        * tie_weights: bool - if True target embedding weights are used for computation output projection
        * shared_emb: bool - if True encoder and decoder will use shared embedding layer
        * pos_enc: str from {'absolute', 'fixed', 'axial'} - type of positional encoding to use
        * axial_shape: tuple - [optional] should be factors of max_seq_len
        * axial_emb_dims: tuple - [optional] axial embedding components, should sum to d_model
    Inputs:
        * src - source input ids, shape [bs, src_sl]
        * tgt - target input ids, shape [bs, tgt_sl]
        * src_mask - optional boolean source mask, shape [bs, src_sl]
        * tgt_mask - optional boolean target mask, shape [bs, tgt_sl]
    Returns:
        * logits - target token logits, shape [bs, tgt_sl, tgt_vocab_sz]
    """
    def __init__(self,
                 enc_vocab_sz,
                 dec_vocab_sz,
                 d_model,
                 n_enc_layers=6,
                 n_dec_layers=6,
                 n_heads=8,
                 d_ff=None,
                 pad_idx=None,
                 tie_weights=True,
                 shared_emb = False,
                 attn_dropout=0.1,
                 ff_dropout=0.1,
                 emb_dropout=0.1,
                 prenorm=False,
                 attn_bias=False,
                 comb_attn=False,
                 pos_enc='absolute',
                 max_seq_len=512,
                 axial_shape=None,
                 axial_emb_dims=None):
        store_attr()
        self.enc_emb = TransformerEmbedding(enc_vocab_sz, d_model, max_seq_len, dropout=emb_dropout, pos_enc=pos_enc,
                                            axial_shape=axial_shape, axial_emb_dims=axial_emb_dims)
        if shared_emb:
            assert (enc_vocab_sz == dec_vocab_sz), "Encoder and decoder vocab size doesn't match"
            self.dec_emb = self.enc_emb
        else:
            self.dec_emb = TransformerEmbedding(dec_vocab_sz, d_model, max_seq_len, dropout=emb_dropout, pos_enc=pos_enc,
                                                axial_shape=axial_shape, axial_emb_dims=axial_emb_dims)
        final_norm = nn.LayerNorm if prenorm else None
        self.encoder = TransformerEncoder(d_model, n_enc_layers, n_heads, d_ff=d_ff, attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                          prenorm=prenorm, attn_bias=attn_bias, final_norm=final_norm, causal=False)
        self.decoder = TransformerDecoder(d_model, n_dec_layers, n_heads, d_ff=d_ff, attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                          prenorm=prenorm, comb_attn=comb_attn, attn_bias=attn_bias, final_norm=final_norm)
        self.proj = nn.Linear(d_model, dec_vocab_sz)
        if tie_weights: self.proj.weight = self.dec_emb.emb.weight

    def forward(self, src, tgt, src_mask=None, tgt_mask=None):
        src_mask = default(src_mask, self.get_padding_mask(src))
        tgt_mask = default(tgt_mask, self.get_padding_mask(tgt))
        enc = self.encoder(self.enc_emb(src), mask=src_mask)
        out = self.decoder(self.dec_emb(tgt), context=enc, mask=tgt_mask, context_mask=src_mask)
        return self.proj(out)

    def get_padding_mask(self, x):
        if self.pad_idx is None: return None
        return (x != self.pad_idx)


# Cell
#TODO find out what is the best way to split encoder-decoder architecture
def transformer_splits(model):
    "[v0] Splits Transformer `model` into groups for differential learning rates."
    groups = L([nn.ModuleList([model.enc_emb, model.dec_emb])] + [l for l in model.encoder.layers] + [l for l in model.decoder.layers] + [model.proj])
    return groups.map(params)

# Cell
class LowMemEncoderBlock(Module):
    """
    Low memory transformer encoder block. Consists of chunked multi-head attention and
    positional feedforward layers
    """
    def __init__(self,
                 d_model:int,
                 n_heads:int = 8,
                 d_ff:int = None,
                 attn_dropout:float = 0.1,
                 ff_dropout:float = 0.1,
                 causal:bool = False,
                 attn_bias:bool = False,
                 prenorm:bool=False,
                 shared_qk:bool=False,
                 attn_chunks:int=1):
        store_attr('attn_dropout') # mb separate argument attn_post_dropout
        if prenorm:
            self.attn = Residual(PreNorm(d_model, ChunkedAttention(d_model, n_heads=n_heads, causal=causal,
                                                    dropout=attn_dropout, bias=attn_bias, shared_qk=shared_qk,
                                                    n_chunks=attn_chunks)))
            self.ff = Residual(PreNorm(d_model, FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))
        else:
            self.attn = PostNorm(d_model, Residual(ChunkedAttention(d_model, n_heads=n_heads, causal=causal,
                                                    dropout=attn_dropout, bias=attn_bias, shared_qk=shared_qk,
                                                                    n_chunks=attn_chunks)))
            self.ff = PostNorm(d_model, Residual(FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)))

    def forward(self, x, mask=None): #? more args
        out = self.attn(x, mask=mask)
        return self.ff(out)

# Cell
class LowMemEncoder(Module):
    """Stack of LowMemEncoderBlocks"""
    def __init__(self,
                 d_model,
                 n_layers=6,
                 n_heads=8,
                 d_ff=None,
                 ff_dropout=0.1,
                 attn_dropout=0.1,
                 attn_bias=False,
                 causal=False,
                 prenorm=False,
                 shared_qk:bool=False,
                 final_norm=None,
                 attn_chunks:int=1):
        store_attr('d_model')
        self.layers = nn.ModuleList([])
        for _ in range(n_layers):
            self.layers.append(LowMemEncoderBlock(d_model, n_heads, causal=causal,
                                    d_ff=d_ff, attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                    prenorm=prenorm, attn_bias=attn_bias, shared_qk=shared_qk,
                                    attn_chunks=attn_chunks))
        self.norm = None if final_norm is None else final_norm(d_model)

    def forward(self, x, mask=None):
        for layer in self.layers: x = layer(x, mask=mask)
        if self.norm is not None: x = self.norm(x)
        return x

# Cell
class ChunkedTransformerLM(Module, LMMixin):
    """
    Basic Transformer for language modelling

    Parameters:
        * vocab_sz: int
        * d_model: int - inner dimension of the model
        * n_layers: int (default: 6)
        * n_heads: int (default: 8)
        * d_ff: int - inner dimension of the pointwise FeedForward net, if None defaults to 4*d_model
        * attn_chunks: int - number of queries chunks for memory-efficient attention
        * attn_dropout: float - attention dropout
        * ff_dropout: float - feed-forward dropout
        * emb_dropout: float - embedding dropout
        * causal: bool (default: True) - if True does causal masking automatically
        * max_seq_len: int (default: 512)
        * tie_weights: bool - if True target embedding weights are used for computation output projection
        * prenorm: bool - wether to use PreNorm or PostNorm
        * attn_bias: bool - wether to allow biases in attention projection layers
        * pad_idx: int - padding token id, required for autogeneration of padding mask
        * pos_enc: str from {'absolute', 'fixed', 'axial'} - type of positional encoding to use
        * axial_shape: tuple - [optional] should be factors of max_seq_len
        * axial_emb_dims: tuple - [optional] axial embedding components, should sum to d_model
    Inputs:
        * x - input ids, shape [bs, sl]
        * mask - optional boolean mask, shape [bs, sl]
    Returns:
        * logits - target token logits, shape [bs, sl, vocab_sz]
    """
    def __init__(self,
                 vocab_sz:int,
                 d_model:int,
                 n_layers:int=6,
                 n_heads:int=8,
                 d_ff:int=None,
                 attn_chunks:int=1,
                 attn_dropout:float=0.1,
                 ff_dropout:float=0.1,
                 emb_dropout:float=0.1,
                 tie_weights:bool=True,
                 causal:bool=True,
                 pos_enc:str='absolute',
                 max_seq_len:int=512,
                 axial_shape:tuple=None,
                 axial_emb_dims:tuple=None,
                 pad_idx:int=None,
                 prenorm:bool=False,
                 attn_bias:bool=False,
                 shared_qk:bool=False):
        store_attr()
        self.emb = TransformerEmbedding(vocab_sz, d_model, max_seq_len, dropout=emb_dropout,
                                        pos_enc=pos_enc, axial_shape=axial_shape,
                                        axial_emb_dims=axial_emb_dims)
        final_norm = nn.LayerNorm if prenorm else None
        self.encoder = LowMemEncoder(d_model, n_layers, n_heads, causal=causal, d_ff=d_ff,
                                    attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                    prenorm=prenorm, attn_bias=attn_bias, attn_chunks=attn_chunks,
                                    shared_qk=shared_qk, final_norm=final_norm)
        self.proj = nn.Linear(d_model, vocab_sz)
        if tie_weights: self.proj.weight = self.emb.emb.weight

    def forward(self, x, mask=None):
        x = self.emb(x)
        x = self.encoder(x, mask=mask)
        return self.proj(x)
