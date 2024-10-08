import torch
from torch import nn
from functools import partial
from typing import Optional, Tuple
from torch.nn.parameter import Parameter
from torch.nn.modules.linear import _LinearWithBias
from torch.nn.init import xavier_uniform_
from torch.nn.init import constant_
from torch.nn.init import xavier_normal_
from torch import Tensor
import torch.nn.functional as F

from .utils import pytorch_utils as pt_utils
from .backbone import EdgeConv

class AttributeDict(dict):
    def __getattr__(self, attr):
        return self[attr]
    def __setattr__(self, attr, value):
        self[attr] = value

NORM_DICT = {
    "batch_norm": nn.BatchNorm1d,
    "id": nn.Identity,
    "layer_norm": nn.LayerNorm,
}

ACTIVATION_DICT = {
    "relu": nn.ReLU,
    "gelu": nn.GELU,
    "leakyrelu": partial(nn.LeakyReLU, negative_slope=0.1),
}

WEIGHT_INIT_DICT = {
    "xavier_uniform": nn.init.xavier_uniform_,
}

EDGECONV_CFG = AttributeDict({
    "sample_method": 'FPS',
    'mlps': [128, 128, 128, 128],
    'use_xyz': True,
    'nsample': 32,
    're_knn_idx': False,
})

class CrossMultiheadAttention(nn.Module):
    bias_k: Optional[torch.Tensor]
    bias_v: Optional[torch.Tensor]

    def __init__(self, embed_dim, num_heads, dropout=0., bias=False, add_bias_kv=False, add_zero_attn=False, kdim=None, vdim=None):
        super(CrossMultiheadAttention, self).__init__()
        self.add_bias_kv = add_bias_kv
        self.embed_dim = embed_dim
        self.bias = bias
        self.kdim = kdim if kdim is not None else embed_dim
        self.vdim = vdim if vdim is not None else embed_dim
        self._qkv_same_embed_dim = self.kdim == embed_dim and self.vdim == embed_dim

        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * \
            num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"

        self.scale = self.head_dim ** -0.5

        self.q1 = nn.Linear(embed_dim, embed_dim //2, bias=bias)
        self.kv1 = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.q2 = nn.Linear(embed_dim, embed_dim //2, bias=bias)
        self.kv2 = nn.Linear(embed_dim, embed_dim, bias=bias)
        
        
        self.attn_drop = nn.Dropout(dropout)
        self.proj1 = nn.Linear(embed_dim // 2, embed_dim // 2)
        self.proj2 = nn.Linear(embed_dim // 2, embed_dim // 2)

        self._reset_parameters()

    def _reset_parameters(self):
        xavier_uniform_(self.q1.weight)
        xavier_uniform_(self.kv1.weight)
        xavier_uniform_(self.q2.weight)
        xavier_uniform_(self.kv2.weight)

        if self.proj1 is not None:
            constant_(self.proj1.bias, 0.)
            constant_(self.proj2.bias, 0.)
        if self.bias:
            xavier_normal_(self.kv1.bias)
            xavier_normal_(self.kv2.bias)


    def __setstate__(self, state):
        # Support loading old MultiheadAttention checkpoints generated by v1.1.0
        if '_qkv_same_embed_dim' not in state:
            state['_qkv_same_embed_dim'] = True

        super(CrossMultiheadAttention, self).__setstate__(state)

    def forward(self, query: Tensor, global_kv: Tensor, local_kv: Tensor, key_padding_mask: Optional[Tensor] = None,
                need_weights: bool = True, attn_mask: Optional[Tensor] = None) -> Tuple[Tensor, Optional[Tensor]]:
        Nq,B,C = query.shape
        Nkg = global_kv.shape[0]
        Nkl = local_kv.shape[0]
        q1 = self.q1(query).reshape(Nq, B, self.num_heads//2, self.head_dim)
        kv1 = self.kv1(global_kv).reshape(Nkg, B, 2, self.num_heads // 2, self.head_dim).permute(2,0,1,3,4)
        k1, v1 = kv1[0], kv1[1]
        q1 = q1.contiguous().view(Nq, B * self.num_heads // 2, self.head_dim).transpose(0, 1)
        k1 = k1.contiguous().view(Nkg, B * self.num_heads // 2, self.head_dim).transpose(0, 1)
        v1 = v1.contiguous().view(Nkg, B * self.num_heads // 2, self.head_dim).transpose(0, 1)
        attn1 = torch.bmm(q1, k1.transpose(1, 2)) * self.scale
        attn1 = attn1.softmax(dim=-1)
        attn1 = self.attn_drop(attn1)
        x1 = torch.bmm(attn1, v1).transpose(0, 1).contiguous().view(Nq, B, C // 2)
        x1 = self.proj1(x1)

        q2 = self.q2(query).reshape(Nq, B, self.num_heads//2, self.head_dim)
        kv2 = self.kv2(local_kv).reshape(Nkl, B, 2, self.num_heads // 2, self.head_dim).permute(2,0,1,3,4)
        k2, v2 = kv2[0], kv2[1]
        q2 = q2.contiguous().view(Nq, B * self.num_heads // 2, self.head_dim).transpose(0, 1)
        k2 = k2.contiguous().view(Nkl, B * self.num_heads // 2, self.head_dim).transpose(0, 1)
        v2 = v2.contiguous().view(Nkl, B * self.num_heads // 2, self.head_dim).transpose(0, 1)
        attn2 = torch.bmm(q2, k2.transpose(1, 2)) * self.scale
        attn2 = attn2.softmax(dim=-1)
        attn2 = self.attn_drop(attn2)
        x2 = torch.bmm(attn2, v2).transpose(0, 1).contiguous().view(Nq, B, C // 2)
        x2 = self.proj2(x2)
        
        attn1 = attn1.view(B, self.num_heads // 2, Nq, Nkg)
        attn2 = attn2.view(B, self.num_heads // 2, Nq, Nkl)
        
        return (x1, attn1.sum(dim=1) / (self.num_heads // 2)), (x2, attn2.sum(dim=1) / (self.num_heads // 2))
    

class SelfMultiheadAttention(nn.Module):
    bias_k: Optional[torch.Tensor]
    bias_v: Optional[torch.Tensor]

    def __init__(self, embed_dim, num_heads, dropout=0., bias=False, add_bias_kv=False, add_zero_attn=False, kdim=None, vdim=None):
        super(SelfMultiheadAttention, self).__init__()
        self.embed_dim = embed_dim
        self.kdim = kdim if kdim is not None else embed_dim
        self.vdim = vdim if vdim is not None else embed_dim
        self._qkv_same_embed_dim = self.kdim == embed_dim and self.vdim == embed_dim

        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * \
            num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"

        self.q_proj_weight = Parameter(torch.Tensor(embed_dim, embed_dim))
        self.k_proj_weight = Parameter(torch.Tensor(embed_dim, self.kdim))
        self.v_proj_weight = Parameter(torch.Tensor(embed_dim, self.vdim))
        self.register_parameter('in_proj_weight', None)

        if bias:
            self.in_proj_bias = Parameter(torch.empty(3 * embed_dim))
        else:
            self.register_parameter('in_proj_bias', None)

        self.out_proj = _LinearWithBias(embed_dim, embed_dim)

        if add_bias_kv:
            self.bias_k = Parameter(torch.empty(1, 1, embed_dim))
            self.bias_v = Parameter(torch.empty(1, 1, embed_dim))
        else:
            self.bias_k = self.bias_v = None

        self.add_zero_attn = add_zero_attn

        self._reset_parameters()

    def _reset_parameters(self):
        xavier_uniform_(self.q_proj_weight)
        xavier_uniform_(self.k_proj_weight)
        xavier_uniform_(self.v_proj_weight)

        if self.in_proj_bias is not None:
            constant_(self.in_proj_bias, 0.)
            constant_(self.out_proj.bias, 0.)
        if self.bias_k is not None:
            xavier_normal_(self.bias_k)
        if self.bias_v is not None:
            xavier_normal_(self.bias_v)

    def __setstate__(self, state):
        # Support loading old MultiheadAttention checkpoints generated by v1.1.0
        if '_qkv_same_embed_dim' not in state:
            state['_qkv_same_embed_dim'] = True

        super(SelfMultiheadAttention, self).__setstate__(state)

    def forward(self, query: Tensor, key: Tensor, vlaue: Tensor, key_padding_mask: Optional[Tensor] = None,
                need_weights: bool = True, attn_mask: Optional[Tensor] = None) -> Tuple[Tensor, Optional[Tensor]]:

        v1 = F.multi_head_attention_forward(
            query, key, vlaue, self.embed_dim, self.num_heads,
            self.in_proj_weight, self.in_proj_bias,
            self.bias_k, self.bias_v, self.add_zero_attn,
            self.dropout, self.out_proj.weight, self.out_proj.bias,
            training=self.training,
            key_padding_mask=key_padding_mask, need_weights=need_weights,
            attn_mask=attn_mask, use_separate_proj_weight=True,
            q_proj_weight=self.q_proj_weight, k_proj_weight=self.k_proj_weight,
            v_proj_weight=self.v_proj_weight)


        return v1


class TransformerLayer(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.s_attn = SelfMultiheadAttention(
            cfg.feat_dim, cfg.num_heads, cfg.attn_dropout)
        self.c_attn = CrossMultiheadAttention(
            cfg.feat_dim, cfg.num_heads, cfg.attn_dropout)

        self.proj_to_mem = (
            pt_utils.Seq(cfg.feat_dim)
            .batchnorm1d()
            .relu()
            .conv1d(cfg.feat_dim, bn=True)
            .conv1d(cfg.feat_dim, activation=None)
        )
        
        self.pre_norm1 = NORM_DICT[cfg.norm](cfg.feat_dim)
        self.pre_norm2 = NORM_DICT[cfg.norm](cfg.feat_dim)


        self.global_norm2 = NORM_DICT[cfg.norm](cfg.feat_dim)
        self.c_mem_norm = NORM_DICT[cfg.norm](cfg.feat_dim)

        self.SA_module = EdgeConv(cfg= EDGECONV_CFG)
        
        f1, f2, f3 = cfg.sp_cfg.f1,cfg.sp_cfg.f2,cfg.sp_cfg.f3
        self.f1 = nn.Linear(f1, 1)
        self.f2 = nn.Linear(f2, 1)
        self.f3 = nn.Linear(f3, 1)
        self.seq_norm = NORM_DICT[cfg.ffn_cfg.norm](cfg.feat_dim)
        if cfg.ffn_cfg:
            self.ffn = nn.Sequential(
                nn.Linear(cfg.feat_dim, cfg.ffn_cfg.hidden_dim,
                          bias=cfg.ffn_cfg.use_bias),
                ACTIVATION_DICT[cfg.ffn_cfg.activation](),
                nn.Dropout(cfg.ffn_cfg.dropout, inplace=True),
                nn.Linear(cfg.ffn_cfg.hidden_dim, cfg.feat_dim,
                          bias=cfg.ffn_cfg.use_bias)

            )
            self.pre_ffn_norm = NORM_DICT[cfg.ffn_cfg.norm](cfg.feat_dim)
            self.ffn_dropout = nn.Dropout(cfg.ffn_cfg.dropout, inplace=True)


        if cfg.pos_emb_cfg:
            self.q_pos_emb = (
                pt_utils.Seq(3)
                .conv1d(cfg.feat_dim, bn=True)
                .conv1d(cfg.feat_dim, activation=None)
            )
            self.pos_emb = (
                pt_utils.Seq(3)
                .conv1d(cfg.feat_dim, bn=True)
                .conv1d(cfg.feat_dim, activation=None)
            )
            self.after_pos_norm2 = NORM_DICT[cfg.ffn_cfg.norm](cfg.feat_dim)
            self.after_pos_norm3 = NORM_DICT[cfg.ffn_cfg.norm](cfg.feat_dim)
        else:
            self.after_pos_norm2 = nn.Identity()
            self.after_pos_norm3 = nn.Identity()

        
        self.c_dropout = nn.Dropout(cfg.dropout)
        self.s_dropout = nn.Dropout(cfg.dropout)
        
        self.cfg = cfg

    def with_pos_embed(self, tensor, pos):
        return tensor if pos is None else tensor + pos

    def with_mask_embed(self, tensor, mask):
        return tensor if mask is None else tensor + mask
    
    def forward(self, input):

        feat = input.pop('feat')  # b,c,n
        xyz = input.pop('xyz')  # b,n,3
        mem_feat = input.pop('mem_feat')  # b,c,t,n
        mem_xyz = input.pop('mem_xyz')  # b,t,n,3
        mem_mask = input.pop('mem_mask')  # b,1,t,n
        mem_alpha = input.pop('mem_alpha') # b,t,2
        B, C, memory_size, npts = mem_feat.shape
        total_npts = memory_size*npts

        x = feat.permute(2, 0, 1) # n,b,c
        norm_x = self.pre_norm1(x)
        
        proj_to_mem_feat = self.proj_to_mem(feat)

        if self.cfg.pos_emb_cfg:
            q_pe = xyz.permute(0, 2, 1).contiguous()
            q_pe = self.q_pos_emb(q_pe).permute(2, 0, 1)
        else:
            q_pe = None

        # mem_feat = interaction(mem_feat, mem_mask, mem_alpha)

        #global
        _ ,global_feat, _ = self.SA_module(mem_xyz.contiguous(), mem_feat.contiguous(), total_npts // 8)
        # b c n        
        q_s = self.with_pos_embed(norm_x, q_pe)

        global_feat = self.global_norm2(global_feat.permute(2,0,1).contiguous())
        mem_norm = self.c_mem_norm(mem_feat.permute(2,0,1))
        q_s = self.after_pos_norm2(q_s)
        (global_x, global_att), (local_x, local_att) = self.c_attn(q_s, global_feat, mem_norm) # N B C, B NQ NK

        x = x + self.c_dropout(torch.cat([global_x, local_x], dim = -1))

        xx = self.pre_norm2(x)
        global_mask_value = torch.mean(global_att.detach(), dim=2) # B Nq
        local_mask_value = torch.mean(local_att.detach(), dim=2) # B Nq
        att_mask = local_mask_value + global_mask_value
        att_sort, att_sort_idx = torch.sort(att_mask, dim=1)
        p1 = torch.gather(xx.permute(1,0,2), 1, att_sort_idx[:, :npts // 4].unsqueeze(-1).repeat(1, 1, C))  # B, N//4, C
        p2 = torch.gather(xx.permute(1,0,2), 1, att_sort_idx[:, npts // 4:npts // 4 * 3].unsqueeze(-1).repeat(1, 1, C))
        p3 = torch.gather(xx.permute(1,0,2), 1, att_sort_idx[:, npts // 4 * 3:].unsqueeze(-1).repeat(1, 1, C))
        token1, token2, token3 = self.cfg.sp_cfg.token1,self.cfg.sp_cfg.token2,self.cfg.sp_cfg.token3
        seq1 = torch.cat([self.f1(p1.permute(0, 2, 1).reshape(B, C, token1, -1)).squeeze(-1),
                        self.f2(p2.permute(0, 2, 1).reshape(B, C, token2, -1)).squeeze(-1),
                        self.f3(p3.permute(0, 2, 1).reshape(B, C, token3, -1)).squeeze(-1)], dim=-1).permute(2,0,1).contiguous()  # N B C
        
        # self-attn
        if self.cfg.pos_emb_cfg:
            pe = xyz.permute(0, 2, 1).contiguous()
            pe = self.pos_emb(pe).permute(2, 0, 1)
        else:
            pe = None

        q = self.with_pos_embed(xx, pe)
        q = self.after_pos_norm3(q)
        seq1 = self.seq_norm(seq1)
        xx, _= self.s_attn(q, seq1, seq1)
        x = x + self.s_dropout(xx)

        if self.cfg.ffn_cfg:
            xx = self.pre_ffn_norm(x)
            xx = self.ffn(xx)
            x = x + self.ffn_dropout(xx)
        feat = x.permute(1, 2, 0)
            
        
        output_dict = dict(
            feat=feat,
            xyz=xyz,
            mem_feat=proj_to_mem_feat,
        )

        return output_dict