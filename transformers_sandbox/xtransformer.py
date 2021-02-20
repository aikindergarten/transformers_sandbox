# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/04x_models.xtransformer.ipynb (unless otherwise specified).

__all__ = ['wrap_sublayer', 'XEncoderBlock', 'XEncoder', 'XDecoderBlock', 'XDecoderBlockV2', 'XDecoder',
           'XTransformerLM', 'XTransformer', 'XConfig']

# Cell
from fastai.basics import *
from .core import *
from .layers import *
from .attention import *
from .transformer import LMMixin, EncDecMixin
from .config import ConfigBase

# Cell
def wrap_sublayer(sublayer:Module, method:str, d_model):
    """
    Wraps a sublayer with skip connection defined by method
    currently supported:
    * postnorm
    * prenorm
    * admin
    * ...
    """
    if method == 'postnorm':
        return PostNorm(d_model, Residual(sublayer))
    elif method == 'prenorm':
        return Residual(PreNorm(d_model, sublayer))
    elif method == 'admin':
        raise NotImplementedError
    else:
        raise NotImplementedError(f'{method} is not valid wrapping method.')

# Cell
class XEncoderBlock(Module):
    """
    Experimental encoder block
    """
    def __init__(self,
                 d_model:int,
                 n_heads:int = 8,
                 attn_module:Module = Attention,
                 ff_module:Module = FeedForward,
                 d_ff:int = None,
                 attn_dropout:float = 0.1,
                 ff_dropout:float = 0.1,
                 causal:bool = False,
                 attn_bias:bool = False,
                 residual_type:str = 'postnorm',
                 shared_qk:bool=False,
                 **kwargs):
        store_attr('attn_dropout') # mb separate argument attn_post_dropout
        attn = attn_module(d_model, n_heads=n_heads, causal=causal, dropout=attn_dropout, bias=attn_bias, shared_qk=shared_qk)
        ff = ff_module(d_model, d_ff=d_ff, dropout=ff_dropout)
        self.attn = wrap_sublayer(attn, method=residual_type, d_model=d_model)
        self.ff = wrap_sublayer(ff, method=residual_type, d_model=d_model)

    def forward(self, x, mask=None): #? more args
        out = self.attn(x, mask=mask)
        return self.ff(out)

# Cell
class XEncoder(Module):
    """Stack of XEncoderBlocks"""
    def __init__(self,
                 d_model,
                 n_layers=6,
                 n_heads=8,
                 d_ff=None,
                 ff_dropout=0.1,
                 attn_dropout=0.1,
                 attn_bias=False,
                 causal=False,
                 residual_type:str='postnorm',
                 shared_qk:bool=False,
                 final_norm=None,
                 **kwargs):
        store_attr('d_model')
        self.layers = nn.ModuleList([])
        for _ in range(n_layers):
            self.layers.append(XEncoderBlock(d_model, n_heads, causal=causal,
                                    d_ff=d_ff, attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                    residual_type=residual_type, attn_bias=attn_bias, shared_qk=shared_qk,
                                            **kwargs))
        self.norm = None if final_norm is None else final_norm(d_model)

    def forward(self, x, mask=None):
        for layer in self.layers: x = layer(x, mask=mask)
        if self.norm is not None: x = self.norm(x)
        return x

# Cell
class XDecoderBlock(Module):
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
                 residual_type='postnorm'):
        # mb separate argument attn_post_dropout
        attn = Attention(d_model, n_heads=n_heads, causal=True, dropout=attn_dropout, bias=attn_bias)
        cross = Attention(d_model, n_heads=n_heads, causal=False, dropout=attn_dropout, bias=attn_bias)
        ff = FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)
        self.attn = wrap_sublayer(attn, method=residual_type, d_model=d_model)
        self.cross = wrap_sublayer(cross, method=residual_type, d_model=d_model)
        self.ff = wrap_sublayer(ff, method=residual_type, d_model=d_model)

    def forward(self, x, context, mask=None, context_mask=None):
        out = self.attn(x, mask=mask)
        out = self.cross(out, context, mask=mask, context_mask=context_mask)
        return self.ff(out)

# Cell
class XDecoderBlockV2(Module):
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
                 residual_type='postnorm'):
        attn = AdditiveAttention(d_model, n_heads=n_heads, causal=True, dropout=attn_dropout, bias=attn_bias)
        ff = FeedForward(d_model, d_ff=d_ff, dropout=ff_dropout)
        self.attn = wrap_sublayer(attn, method=residual_type, d_model=d_model)
        self.ff = wrap_sublayer(ff, method=residual_type, d_model=d_model)

    def forward(self, x, context, mask=None, context_mask=None):
        out = self.attn(x, context, mask=mask, context_mask=context_mask)
        out = self.ff(out)
        return out

# Cell
class XDecoder(Module):
    """Stack of TransformerDecoder layers"""
    def __init__(self,
                 d_model,
                 n_layers=6,
                 n_heads=8,
                 d_ff=None,
                 attn_dropout=0.1,
                 ff_dropout=0.1,
                 residual_type='postnorm',
                 comb_attn=False,
                 attn_bias=False,
                 final_norm=None):
        store_attr('d_model')
        #TODO(Arto) refactor
        block = XDecoderBlockV2 if comb_attn else XDecoderBlock
        self.layers = nn.ModuleList([])
        for _ in range(n_layers):
            self.layers.append(block(d_model, n_heads, d_ff=d_ff, attn_dropout=attn_dropout,
                                     ff_dropout=ff_dropout, residual_type=residual_type, attn_bias=attn_bias))
        self.norm = None if final_norm is None else final_norm(d_model)

    def forward(self, x, context, mask=None, context_mask=None):
        for layer in self.layers: x = layer(x, context, mask, context_mask)
        if self.norm is not None: x = self.norm(x)
        return x

# Cell
class XTransformerLM(Module, LMMixin):
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
        * residual_type: str - one of {'postnorm', 'prenorm', 'admin', 'rezero'}
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
                 residual_type:str='postnorm',
                 attn_bias:bool=False,
                 shared_qk:bool=False):
        store_attr()
        self.emb = TransformerEmbedding(vocab_sz, d_model, max_seq_len, dropout=emb_dropout,
                                        pos_enc=pos_enc, axial_shape=axial_shape,
                                        axial_emb_dims=axial_emb_dims)
        final_norm = nn.LayerNorm if (residual_type in {'prenorm'}) else None
        self.encoder = XEncoder(d_model, n_layers, n_heads, causal=causal, d_ff=d_ff,
                                attn_dropout=attn_dropout, ff_dropout=ff_dropout,
                                residual_type=residual_type, attn_bias=attn_bias,
                                shared_qk=shared_qk, final_norm=final_norm)
        self.proj = nn.Linear(d_model, vocab_sz)
        if tie_weights: self.proj.weight = self.emb.emb.weight

    def forward(self, x, mask=None):
        x = self.emb(x)
        x = self.encoder(x, mask=mask)
        return self.proj(x)


# Cell
class XTransformer(Module, EncDecMixin):
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
# def transformer_splits(model):
#     "[v0] Splits Transformer `model` into groups for differential learning rates."
#     groups = L([nn.ModuleList([model.enc_emb, model.dec_emb])] + [l for l in model.encoder.layers] + [l for l in model.decoder.layers] + [model.proj])
#     return groups.map(params)

# Cell
class XConfig(ConfigBase):
    """
    Config for enwik8 Experiment.
    See https://arampacha.github.io/reformer_fastai/experiment.enwik8-baseline.html for details
    """
    _model = XTransformerLM
    _d = {
        'vocab_sz':256,
        'd_model':512,
        'n_layers':12,
        'n_heads':8,
        'attn_module':Attention,
        'ff_module':FeedForward,
        'd_ff':None,
        'attn_dropout':0.1,
        'ff_dropout':0.1,
        'emb_dropout':0.1,
        'tie_weights':True,
        'causal':True,
        'pos_enc':'absolute',
        'max_seq_len':512,
        'axial_shape':None,
        'axial_emb_dims':None,
        'pad_idx':None,
        'attn_bias':False,
        'shared_qk':False,
        'residual_type':'postnorm',
    }
    @update_sig(_d)
    def __init__(self, **kwargs):
        super().__init__(**kwargs)