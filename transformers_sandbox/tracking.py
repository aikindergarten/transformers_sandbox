# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/09_tracking.ipynb (unless otherwise specified).

__all__ = []

# Cell
from fastcore.basics import *

try:
    from fastai.callback.wandb import *
except ImportError as e:
    print(e)

from fastai.basics import *

from fastai.callback.hook import total_params

# Cell
@patch
def gather_args(self:Learner):
    "Gather config parameters accessible to the learner, adds model config logging on top of default wandb logging"
    # args stored by `store_attr`
    cb_args = {f'{cb}':getattr(cb,'__stored_args__',True) for cb in self.cbs}
    args = {'Learner':self, **cb_args}

    # Log model attrs
    model_attrs = getattr(self.model,'__stored_args__', None)
    if model_attrs is not None: args.update({f'model_{k}' : model_attrs[k] for k in model_attrs.keys()})

    # input dimensions
    try:
        n_inp = self.dls.train.n_inp
        args['n_inp'] = n_inp
        xb = self.dls.train.one_batch()[:n_inp]
        args.update({f'input {n+1} dim {i+1}':d for n in range(n_inp) for i,d in enumerate(list(detuplify(xb[n]).shape))})
    except: print(f'Could not gather input dimensions')
    # other useful information
    with ignore_exceptions():
        args['batch size'] = self.dls.bs
        args['batch per epoch'] = len(self.dls.train)
        args['model parameters'] = total_params(self.model)[0]
        args['device'] = self.dls.device.type
        args['frozen'] = bool(self.opt.frozen_idx)
        args['frozen idx'] = self.opt.frozen_idx
        args['dataset.tfms'] = f'{self.dls.dataset.tfms}'
        args['dls.after_item'] = f'{self.dls.after_item}'
        args['dls.before_batch'] = f'{self.dls.before_batch}'
        args['dls.after_batch'] = f'{self.dls.after_batch}'
    return args