import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Adam
from torch.distributions.categorical import Categorical
from torch.optim.lr_scheduler import CosineAnnealingLR, CosineAnnealingWarmRestarts
from warmup_scheduler import GradualWarmupScheduler

#from pytorch_lightning.core.lightning import LightningModule
#import pytorch_lightning as pl
from models.resnet import resnet_small
import numpy as np

from models.misc import get_lr
from models.resnet import resnet_small


def sample_top_p(logits, top_p=0.3, filter_value=-float('Inf')):
    '''
    logits: single array of logits (N,)
    top_p: top cumulative probability to select

    return: new array of logits, same shape as logits (N,)
    '''
    sorted_logits, sorted_indices = torch.sort(logits, descending=True)
    cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

    # Remove tokens with cumulative probability above the threshold
    sorted_indices_to_remove = cumulative_probs > top_p
    # Shift the indices to the right to keep also the first token above the threshold
    sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
    sorted_indices_to_remove[..., 0] = 0

    indices_to_remove = sorted_indices[sorted_indices_to_remove]
    # dont modify the original logits
    sampled = logits.clone()
    sampled[indices_to_remove] = filter_value

    return sampled


class scene_transformer(nn.Module): #LightningModule

    def __init__(self, cfg):
        super(scene_transformer, self).__init__()
        # self.hparams = cfg
        # self.save_hyperparameters(cfg)
        self.cfg = cfg

        emb_dim = cfg["model"]["emb_dim"]

        self.cat_emb = nn.Embedding(
            cfg["model"]["cat"]["start_token"] + 1,
            emb_dim,
            padding_idx=cfg["model"]["cat"]["pad_token"],
        )
        self.pos_emb = nn.Embedding(cfg["model"]["max_seq_len"], emb_dim)

        self.x_coor_emb = nn.Embedding(
            cfg["model"]["coor"]["start_token"] + 1,
            emb_dim,
            padding_idx=cfg["model"]["coor"]["pad_token"],
        )
        self.y_coor_emb = nn.Embedding(
            cfg["model"]["coor"]["start_token"] + 1,
            emb_dim,
            padding_idx=cfg["model"]["coor"]["pad_token"],
        )
        self.z_coor_emb = nn.Embedding(
            cfg["model"]["coor"]["start_token"] + 1,
            emb_dim,
            padding_idx=cfg["model"]["coor"]["pad_token"],
        )

        self.orient_emb = nn.Embedding(
            cfg["model"]["orient"]["start_token"] + 1,
            emb_dim,
            padding_idx=cfg["model"]["orient"]["pad_token"],
        )

        self.shape_cond = cfg["model"]["cat"]["shape_cond"]
        self.text_cond = cfg["model"]["cat"]["text_cond"]

        self.emb_dim = emb_dim

        if self.shape_cond:
            print("Using shape conditioned model")
            self.x_emb = nn.Embedding(16, self.emb_dim)
            self.y_emb = nn.Embedding(16, self.emb_dim)
            self.img_encoder = resnet_small(layers=[1, 2, 2], num_input_channels=1, dim=self.emb_dim)

        if self.text_cond:
            print("Using text conditioned model")
            self.text_emb_linear = nn.Linear(50, self.emb_dim)

        if self.shape_cond or self.text_cond:
            layer = nn.TransformerDecoderLayer
            gen_model = nn.TransformerDecoder
        else:
            layer = nn.TransformerEncoderLayer
            gen_model = nn.TransformerEncoder

        d_layer = layer(
            d_model=self.emb_dim,
            nhead=cfg["model"]["num_heads"],
            dim_feedforward=cfg["model"]["dim_fwd"],
            dropout=cfg["model"]["dropout"],
        )

        self.generator = gen_model(d_layer, cfg['model']['num_blocks'])
        self.output_cat = nn.Linear(self.emb_dim, cfg['model']['cat']['start_token'])
        self.decoder_seq_len = cfg['model']['max_seq_len']

    def get_shape_memory(self, room_shape):
        """
        Get the transformer encoder memory for the room_shape condition images
        (similar to PolyGen image conditional model)

        room_shape: (bsize, input_channel, 512, 512)

        return: (16*16, bsize, embdim)
        """
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        features = self.img_encoder(room_shape)
        # dimension of condition image
        img_dim = features.shape[-1]
        # 0,1,2 .. img_dim
        ndx = torch.LongTensor(range(img_dim)).unsqueeze(0).to(device)
        # positional embedding in X and Y axes
        x_emb, y_emb = (
            self.x_emb(ndx).transpose(1, 2).unsqueeze(3),
            self.y_emb(ndx).transpose(1, 2).unsqueeze(2),
        )

        # add positional embedding
        tmp = features + x_emb + y_emb
        features_flat = tmp.reshape(tmp.shape[0], tmp.shape[1], -1)
        memory = features_flat.permute(2, 0, 1)

        return memory

    def get_text_memory(self, text_emb):
        """
        input: (batch_size, max_length, text_emb_dim=50)
        output: (max_length, bsize, embdim)
        """
        # get (bsize, max_length, emb_dim)
        new_emb = self.text_emb_linear(text_emb)
        memory = new_emb.transpose(0, 1)
        return memory

    def forward(
        self,
        cat_seq,
        x_loc_seq,
        y_loc_seq,
        z_loc_seq,
        orient_seq,
        text_emb=None,
        room_shape=None,
    ):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        cat_emb, pos_emb, x_emb, y_emb, z_emb, ori_emb = self.get_embedding(
            cat_seq, x_loc_seq, y_loc_seq, z_loc_seq, orient_seq
        )  # ,obj_emb

        joint_emb = cat_emb + pos_emb + x_emb + y_emb + z_emb + ori_emb

        tgt_padding_mask = self.get_padding_mask(cat_seq)[:, :-1].to(device)
        tgt_mask = self.generate_square_subsequent_mask(cat_seq.shape[1] - 1).to(device)

        tgt = joint_emb.transpose(1, 0)[:-1, :, :]
        # get condition vectors as memory
        if self.shape_cond:
            memory = self.get_shape_memory(room_shape) if self.shape_cond else None
        # TODO: handle both shape and text conditioning together
        elif self.text_cond:
            memory = self.get_text_memory(text_emb) if self.text_cond else None

        # condition on the memory in fwd pass
        if self.shape_cond or self.text_cond:
            out_embs = self.generator(
                tgt, memory, tgt_mask=tgt_mask, tgt_key_padding_mask=tgt_padding_mask
            )
        else:
            out_embs = self.generator(tgt, tgt_mask, tgt_padding_mask)

        out_embs = out_embs.transpose(1, 0)
        out_cat = self.output_cat(out_embs)
        logprobs_cat = F.log_softmax(out_cat, dim=-1)

        return logprobs_cat

    def get_embedding(self, cat_seq, x_loc_seq, y_loc_seq, z_loc_seq, orient_seq):
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        cat_emb = self.cat_emb(cat_seq)
        seq_len = cat_seq.shape[1]

        x_emb = self.x_coor_emb(x_loc_seq)
        y_emb = self.y_coor_emb(y_loc_seq)
        z_emb = self.z_coor_emb(z_loc_seq)

        ori_emb = self.orient_emb(orient_seq)

        pos_seq = torch.arange(0, seq_len).to(device)
        pos_emb = self.pos_emb(pos_seq)

        return cat_emb, pos_emb, x_emb, y_emb, z_emb, ori_emb

    def generate_square_subsequent_mask(self, sz):
        mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1)
        mask = (
            mask.float()
            .masked_fill(mask == 0, float("-inf"))
            .masked_fill(mask == 1, float(0.0))
        )
        return mask

    def get_padding_mask(self, seq):
        mask = torch.ByteTensor(np.zeros(seq.shape, dtype=np.uint8))
        mask[seq == self.cfg["model"]["cat"]["pad_token"]] = 1

        return mask.bool()

    def configure_optimizers(self):
        self.optim = Adam(
            self.parameters(),
            lr=self.cfg["train"]["lr"],
            weight_decay=self.cfg["train"]["l2"],
        )
        self.sched = CosineAnnealingLR(
            self.optim, T_max=self.cfg["train"]["lr_restart"]
        )
        self.warmup = GradualWarmupScheduler(
            self.optim,
            multiplier=1,
            total_epoch=self.cfg["train"]["warmup"],
            after_scheduler=self.sched,
        )

        return [self.optim], [self.warmup]

    def optimizer_step(
        self, current_epoch, batch_nb, optimizer, optimizer_i, second_order_closure=None
    ):
        optimizer.step()
        self.sched.step()
        optimizer.zero_grad()

    def general_step(self, batch):
        loss = 0
        cat_seq, x_loc_seq, y_loc_seq, z_loc_seq, orient_seq = (
            batch["cat_seq"],
            batch["x_loc_seq"],
            batch["y_loc_seq"],
            batch["z_loc_seq"],
            batch["orient_seq"],
        )

        room_shape = batch["floor"] if self.shape_cond else None
        text_emb = batch["desc_emb"] if self.text_cond else None

        logprobs_cat = self.forward(
            cat_seq,
            x_loc_seq,
            y_loc_seq,
            z_loc_seq,
            orient_seq,
            room_shape=room_shape,
            text_emb=text_emb,
        )

        loss_cat = F.nll_loss(
            logprobs_cat.transpose(1, 2),
            batch["cat_seq"][:, 1:],
            ignore_index=self.cfg["model"]["cat"]["pad_token"],
        )

        loss = loss_cat

        return loss

    def training_step(self, batch, batch_idx):
        loss = self.general_step(batch)
        lr = get_lr(self.optim)
        log = {"loss": {"train_loss": loss}, "lr": lr}
        return {"loss": loss, "log": log}

    def validation_step(self, batch, batch_idx):
        loss = self.general_step(batch)
        return {"val_loss": loss}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()

        log = {"loss": {"val": avg_loss}}

        return {"val_loss": avg_loss, "log": log}

    def decode_multi_model(
        self,
        out_ndx,
        cat_gen_seq,
        x_gen_seq,
        y_gen_seq,
        z_gen_seq,
        ori_gen_seq,
        probabilistic=True,
        nucleus=True,
        room_shape=None,
        text_emb=None,
    ):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        curr_cat_seq = cat_gen_seq + (self.decoder_seq_len - len(cat_gen_seq)) * [0]
        curr_cat_seq = torch.LongTensor(curr_cat_seq).view(1, -1).to(device)

        curr_x_seq = x_gen_seq + (self.decoder_seq_len - len(x_gen_seq)) * [0]
        curr_x_seq = torch.LongTensor(curr_x_seq).view(1, -1).to(device)

        curr_y_seq = y_gen_seq + (self.decoder_seq_len - len(y_gen_seq)) * [0]
        curr_y_seq = torch.LongTensor(curr_y_seq).view(1, -1).to(device)

        curr_z_seq = z_gen_seq + (self.decoder_seq_len - len(z_gen_seq)) * [0]
        curr_z_seq = torch.LongTensor(curr_z_seq).view(1, -1).to(device)

        curr_orient_seq = ori_gen_seq + (self.decoder_seq_len - len(ori_gen_seq)) * [0]
        curr_orient_seq = torch.LongTensor(curr_orient_seq).view(1, -1).to(device)

        cat_emb, pos_emb, x_emb, y_emb, z_emb, ori_emb = self.get_embedding(
            curr_cat_seq, curr_x_seq, curr_y_seq, curr_z_seq, curr_orient_seq
        )

        joint_emb = cat_emb + pos_emb + x_emb + y_emb + z_emb + ori_emb
        tgt = joint_emb.transpose(1, 0)
        tgt_mask = self.generate_square_subsequent_mask(tgt.shape[0]).to(device)
        tgt_padding_mask = self.get_padding_mask(curr_cat_seq).to(device)
        if self.shape_cond:
            room_shape = room_shape.unsqueeze(0).to(device)
            memory = self.get_shape_memory(room_shape) if self.shape_cond else None
            out_embs = self.generator(
                tgt, memory, tgt_mask=tgt_mask, tgt_key_padding_mask=tgt_padding_mask
            )
        if self.text_cond:
            memory = self.get_text_memory(text_emb) if self.text_cond else None
        # condition on the memory in fwd pass
        if self.shape_cond or self.text_cond:
            out_embs = self.generator(tgt, memory, tgt_mask=tgt_mask)
        else:
            out_embs = self.generator(tgt, tgt_mask, tgt_padding_mask)

        logits_cat = self.output_cat(out_embs)[out_ndx][0]

        if probabilistic and nucleus:
            logits_cat = sample_top_p(logits_cat)

        probs_cat = F.softmax(logits_cat, dim=-1)

        if probabilistic:
            cat_next_token = Categorical(probs=probs_cat).sample()

        else:
            _, cat_next_token = torch.max(probs_cat, dim=0)

        if cat_next_token == self.cfg["model"]["cat"]["stop_token"]:
            cat_next_token = 999
        return cat_next_token
