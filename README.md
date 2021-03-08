# Transformers Sandbox
> All transformers bricks and tricks.


## Install

`pip install git+git://github.com/aikindergarten/transformers_sandbox.git`

## How to use

wip...

```
import torch
from transformers_sandbox.xtransformer import *

model = XTransformerLM.from_config(XConfig(n_layers=2, residual_type='prenorm'))
inp = torch.randint(0, 256, (4, 128))
out = model(inp)
out.shape
```




    torch.Size([4, 128, 256])


