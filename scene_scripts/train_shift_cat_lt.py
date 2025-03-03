import argparse

from torchvision.transforms import Compose
import torch
from torch.utils.data import DataLoader, Subset

#from pytorch_lightning.callbacks import ModelCheckpoint
# from pytorch_lightning import Trainer, seed_everything

from transforms.scene import (
    SeqToTensor,
    Augment_rotation,
    Augment_jitterring,
    Get_cat_shift_info,
    Padding_joint,
    Add_Relations,
    Add_Descriptions,
    Add_Glove_Embeddings,
)
from datasets.suncg_shift_seperate_dataset_deepsynth import SUNCG_Dataset
from separate_models.scene_shift_cat import scene_transformer
from utils.config import read_config

# seed_everything(1)


def log_metrics(self, metrics, step=None):
    for k, v in metrics.items():
        if isinstance(v, dict):
            self.experiment.add_scalars(k, v, step)
        else:
            if isinstance(v, torch.Tensor):
                v = v.item()
            self.experiment.add_scalar(k, v, step)


def monkeypatch_tensorboardlogger(logger):
    import types

    logger.log_metrics = types.MethodType(log_metrics, logger)


def run_training(cfg):
    cfg['data']['data_path'] = "/home/ubuntu/research/suncg/bedroom"
    transforms = [Augment_rotation(cfg['train']['aug']['rotation_list']), Augment_jitterring(cfg['train']['aug']['jitter_list']), Get_cat_shift_info(cfg)]

    if cfg["model"]["cat"]["text_cond"]:
        transforms += [
            Add_Relations(),
            Add_Descriptions(),
            Add_Glove_Embeddings(max_sentences=3, max_length=50),
        ]
    transforms.append(Padding_joint(cfg))
    transforms.append(SeqToTensor())
    t = Compose(transforms)


    trainval_set = SUNCG_Dataset(data_folder=cfg['data']['data_path'], list_path=cfg['data']['list_path'], transform=t)
    total_len = len(trainval_set)-2
    train_len = int(0.8 * total_len)

    train_set = Subset(trainval_set, range(train_len))
    val_set = Subset(trainval_set, range(train_len, total_len))

    train_loader = DataLoader(
        train_set, batch_size=cfg["train"]["batch_size"], shuffle=True, num_workers=4
    )
    val_loader = DataLoader(
        val_set, batch_size=cfg["train"]["batch_size"], num_workers=4
    )

    model = scene_transformer(cfg)
    # checkpoint_callback = ModelCheckpoint(save_last=True, save_top_k=5)

    print("Batch")
    print(next(iter(train_loader)))
    # trainer = Trainer(gpus=1,
    #                   gradient_clip_val=1.0,
    #                   # fast_dev_run=True,
    #                   max_epochs=cfg['train']['epochs'],
    #                   checkpoint_callback=None,
    #                   resume_from_checkpoint=cfg['train']['resume']
    #                   )


    monkeypatch_tensorboardlogger(trainer.logger)

    trainer.fit(model, train_dataloader=train_loader, val_dataloaders=val_loader)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cfg_path", help="Path to config file")
    args = parser.parse_args()
    cfg = read_config(args.cfg_path)

    run_training(cfg)
