"""
Super-resolution of CelebA using Generative Adversarial Networks.
The dataset can be downloaded from: https://www.dropbox.com/sh/8oqt9vytwxb3s4r/AADIKlz8PR9zr6Y20qbkunrba/Img/img_align_celeba.zip?dl=0
(if not available there see if options are listed at http://mmlab.ie.cuhk.edu.hk/projects/CelebA.html)
Instrustion on running the script:
1. Download the dataset from the provided link
2. Save the folder 'img_align_celeba' to '../../data/'
4. Run the sript using command 'python3 srgan.py'
"""

import argparse
import os
import numpy as np
import math
import itertools
import sys
import logging

import torchvision.transforms as transforms
from torchvision.utils import save_image, make_grid

from torch.utils.data import DataLoader
from torch.autograd import Variable

from models import *
from datasets import *

import torch.nn as nn
import torch.nn.functional as F
import torch

import adios2 as ad2

from datetime import datetime
from pathlib import Path


def parse_rangestr(rangestr):
    _rangestr = rangestr.replace(" ", "")
    # Convert String ranges to list
    # Using sum() + list comprehension + enumerate() + split()
    res = sum(
        (
            (
                list(range(*[int(b) + c for c, b in enumerate(a.split("-"))]))
                if "-" in a
                else [int(a)]
            )
            for a in _rangestr.split(",")
        ),
        [],
    )
    return res


parser = argparse.ArgumentParser()
parser.add_argument(
    "--epoch",
    type=int,
    default=0,
    help="epoch to start training from (default: %(default)s)",
)
parser.add_argument(
    "--n_epochs",
    "-n",
    type=int,
    default=100,
    help="number of epochs of training (default: %(default)s)",
)
parser.add_argument(
    "--batch_size",
    type=int,
    default=16,
    help="size of the batches (default: %(default)s)",
)
parser.add_argument(
    "--lr",
    type=float,
    default=0.0002,
    help="adam: learning rate (default: %(default)s)",
)
parser.add_argument(
    "--b1",
    type=float,
    default=0.5,
    help="adam: decay of first order momentum of gradient (default: %(default)s)",
)
parser.add_argument(
    "--b2",
    type=float,
    default=0.999,
    help="adam: decay of first order momentum of gradient (default: %(default)s)",
)
parser.add_argument(
    "--decay_epoch",
    type=int,
    default=100,
    help="epoch from which to start lr decay (default: %(default)s)",
)
parser.add_argument(
    "--n_cpu",
    type=int,
    default=0,
    help="number of cpu threads to use during batch generation (default: %(default)s)",
)
# parser.add_argument("--hr_height", type=int, default=64, help="high res. image height (default: %(default)s)")
# parser.add_argument("--hr_width", type=int, default=80, help="high res. image width (default: %(default)s)")
# parser.add_argument("--channels", type=int, default=1, help="number of image channels (default: %(default)s)")
parser.add_argument(
    "--sample_interval",
    type=int,
    default=1000,
    help="interval between saving image samples (default: %(default)s)",
)
parser.add_argument(
    "--checkpoint_interval",
    "-x",
    type=int,
    default=10,
    help="interval between model checkpoints (default: %(default)s)",
)
parser.add_argument(
    "--nchannel", type=int, default=1, help="num. of channels (default: %(default)s)"
)
parser.add_argument("--modelfile", help="modelfile (default: %(default)s)")
parser.add_argument("--nofeatureloss", help="no feature loss", action="store_true")
parser.add_argument("--log", help="log", action="store_true")
parser.add_argument("--suffix", help="suffix")
group = parser.add_mutually_exclusive_group()
group.add_argument(
    "--VGG",
    help="use VGG 3-channel model",
    action="store_const",
    dest="model",
    const="VGG",
)
group.add_argument(
    "--N1024",
    help="use XGC 1-channel N1024 model",
    action="store_const",
    dest="model",
    const="N1024",
)
parser.set_defaults(model="N1024")
group = parser.add_mutually_exclusive_group()
group.add_argument(
    "--xgc", help="XGC dataset", action="store_const", dest="dataset", const="xgc"
)
group.add_argument(
    "--nstx", help="NSTX dataset", action="store_const", dest="dataset", const="nstx"
)
parser.set_defaults(dataset="nstx")
group = parser.add_argument_group("NSTX", "NSTX processing options")
group.add_argument("--gaussian", help="apply gaussian filter", action="store_true")
group.add_argument(
    "--nframes", type=int, default=16_000, help="number of frames to load"
)
group = parser.add_argument_group("XGC", "XGC processing options")
group.add_argument(
    "--datadir", help="data directory (default: %(default)s)", default="d3d_coarse_v2"
)
group.add_argument("--hr_datadir", help="HR data directory (default: %(default)s)")
group.add_argument("--surfid", help="flux surface index")
group.add_argument("--iphi", help="iphi", type=int, default=None)
group.add_argument("--istep", help="istep", type=int, default=420)
group.add_argument("--nodestride", help="nodestride", type=int, default=1)
opt = parser.parse_args()

prefix = "srgan-%s-%s-ch%d" % (opt.dataset, opt.model, opt.nchannel)
if opt.suffix is not None:
    prefix = "%s-%s" % (prefix, opt.suffix)
Path(prefix).mkdir(parents=True, exist_ok=True)

handlers = [logging.StreamHandler()]
if opt.log:
    suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
    pid = os.getpid()
    fname = "%s/run-%s-%d.log" % (prefix, suffix, pid)
    handlers.append(logging.FileHandler(fname))
logging.basicConfig(
    format="[%(levelname)s] %(message)s", level=logging.DEBUG, handlers=handlers
)

logging.info("Command: {0}\n".format(" ".join([x for x in sys.argv])))
logging.debug("All settings used:")
for k, v in sorted(vars(opt).items()):
    logging.debug("\t{0}: {1}".format(k, v))
logging.debug("prefix: %s" % prefix)

cuda = torch.cuda.is_available()
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# %%
# ----------
#  Data
# ----------
if opt.dataset == "nstx":
    offset = 159065
    length = opt.nframes
    with ad2.open("nstx_data_ornl_demo_v2.bp", "r") as f:
        start = (offset, 0, 0)
        count = (length, 64, 80)
        gpiData = f.read("gpiData", start=start, count=count)
    logging.debug(gpiData.shape)

    X = gpiData.astype(np.float32)
    if opt.gaussian:
        from scipy.ndimage import gaussian_filter

        for i in range(len(X)):
            X[i, :] = gaussian_filter(X[i, :], sigma=2)

    xmin = np.min(X, axis=(1, 2))
    xmax = np.max(X, axis=(1, 2))
    X = (X - xmin[:, np.newaxis, np.newaxis]) / (xmax - xmin)[:, np.newaxis, np.newaxis]
    H = X

if opt.dataset == "xgc":
    ## XGC
    logging.getLogger().setLevel(logging.WARN)
    import xgc4py
    from vapor import read_f0_nodes

    logging.getLogger().setLevel(logging.DEBUG)
    xgcexp = xgc4py.XGC(opt.datadir, step=opt.istep, device=device)
    surfid_list = parse_rangestr(opt.surfid)
    node_list = list()
    for i in surfid_list:
        _nodes = xgcexp.mesh.surf_nodes(i)[:: opt.nodestride]
        logging.info(f"Surf idx, len: {i} {len(_nodes)}")
        node_list.extend(_nodes)
    nextnode_arr = xgcexp.nextnode_arr
    out = read_f0_nodes(
        opt.istep,
        node_list,
        expdir=opt.datadir,
        iphi=opt.iphi,
        nextnode_arr=nextnode_arr,
    )
    X = out[1].astype(np.float32)
    zmin = out[4]
    zmax = out[5]
    zlb = out[6]
    zlb = np.hstack([np.arange(len(zlb))[:, np.newaxis], zlb])
    logging.debug("data size: %s" % list(X.shape))
    # X = X[:opt.nframes,]

    if opt.hr_datadir is not None:
        node_list = list()
        for i in surfid_list:
            _nodes = xgcexp.mesh.surf_nodes(i)[:: opt.nodestride]
            logging.info(f"Surf idx, len: {i} {len(_nodes)}")
            node_list.extend(_nodes)
        nextnode_arr = xgcexp.nextnode_arr
        out = read_f0_nodes(
            opt.istep,
            node_list,
            expdir=opt.hr_datadir,
            iphi=opt.iphi,
            nextnode_arr=nextnode_arr,
        )
        H = out[1].astype(np.float32)
        logging.debug("data size: %s" % list(H.shape))
        # H = H[:opt.nframes,]
    else:
        H = X

X_lr, X_hr, = torch.tensor(
    X[:, np.newaxis, ::4, ::4]
), torch.tensor(H[:, np.newaxis, :, :])
if opt.nchannel == 3:
    X_lr = torch.cat((X_lr, X_lr, X_lr), axis=1)
    X_hr = torch.cat((X_hr, X_hr, X_hr), axis=1)
training_data = torch.utils.data.TensorDataset(X_lr, X_hr)
dataloader = torch.utils.data.DataLoader(
    training_data, batch_size=opt.batch_size, shuffle=True
)
validationloader = torch.utils.data.DataLoader(
    training_data, batch_size=opt.batch_size, shuffle=False
)

# ----------
#  Models
# ----------
hr_shape = (X.shape[-2], X.shape[-1])

# Initialize generator and discriminator
generator = GeneratorResNet(in_channels=opt.nchannel, out_channels=opt.nchannel)
discriminator = Discriminator(input_shape=(opt.nchannel, *hr_shape))
if opt.model == "VGG":
    assert opt.nchannel == 3
    feature_extractor = FeatureExtractor()
else:
    modelfile = (
        "nstx-vgg19-ch%d-%s.torch" % (opt.nchannel, opt.model)
        if opt.modelfile is None
        else opt.modelfile
    )
    feature_extractor = XGCFeatureExtractor(modelfile)

# Set feature extractor to inference mode
feature_extractor.eval()

# Losses
criterion_GAN = torch.nn.MSELoss()
criterion_content = torch.nn.L1Loss()
# criterion_content = torch.nn.MSELoss()

if cuda:
    generator = generator.cuda()
    discriminator = discriminator.cuda()
    feature_extractor = feature_extractor.cuda()
    criterion_GAN = criterion_GAN.cuda()
    criterion_content = criterion_content.cuda()

if opt.epoch != 0:
    # import glob
    # fnames = glob.glob('%s/generator_*.pth'%(prefix))
    # epoch_list = list()
    # for name in fnames:
    #     m = re.search(r'generator_([0-9]+).pth', name)
    #     epoch = int(m.group(1))
    #     epoch_list.append(epoch)
    # epoch = max(epoch_list)

    # Load pretrained models
    fname0 = "%s/generator_%d.pth" % (prefix, opt.epoch)
    fname1 = "%s/discriminator_%d.pth" % (prefix, opt.epoch)
    logging.debug("Loading: %s %s" % (fname0, fname1))
    generator.load_state_dict(torch.load(fname0))
    discriminator.load_state_dict(torch.load(fname1))

# Optimizers
optimizer_G = torch.optim.Adam(
    generator.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2)
)
optimizer_D = torch.optim.Adam(
    discriminator.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2)
)

Tensor = torch.cuda.FloatTensor if cuda else torch.Tensor

# ----------
#  Training
# ----------

epoch_end = opt.epoch + opt.n_epochs
for epoch in range(opt.epoch, epoch_end):
    abs_list = list()
    for i, imgs in enumerate(dataloader):

        # Configure model input
        imgs_lr = imgs[0].to(device)
        imgs_hr = imgs[1].to(device)
        nb, nc, nh, nw = imgs_hr.shape
        # print ('imgs_lr, imgs_hr:', list(imgs_lr.shape), list(imgs_hr.shape))

        # n2 = (64-imgs_lr.shape[3])//2
        # n1 = (64-imgs_lr.shape[2])//2
        # imgs_lr = F.pad(imgs_lr, (n2,n2,n1,n1), "constant", -mean/std)

        # n2 = (256-imgs_hr.shape[3])//2
        # n1 = (256-imgs_hr.shape[2])//2
        # imgs_hr = F.pad(imgs_hr, (n2,n2,n1,n1), "constant", -mean/std)
        # print (imgs_lr.shape, imgs_lr.min(), imgs_lr.max(), imgs_lr.mean())

        # Adversarial ground truths
        # output_shape = discriminator.output_shape
        # valid = Variable(Tensor(np.ones((imgs_lr.size(0), *output_shape))), requires_grad=False)
        # fake = Variable(Tensor(np.zeros((imgs_lr.size(0), *output_shape))), requires_grad=False)

        # ------------------
        #  Train Generators
        # ------------------

        optimizer_G.zero_grad()

        # Generate a high resolution image from low resolution input
        gen_hr = generator(imgs_lr)
        gen_hr = gen_hr[:, :, :nh, :nw]

        # print ('imgs_lr', imgs_lr.min().item(), imgs_lr.max().item(), imgs_lr.mean().item())
        # print ('gen_hr', gen_hr.min().item(), gen_hr.max().item(), gen_hr.mean().item())

        # Adversarial loss
        # valid.shape: torch.Size([16, 1, 16, 16])
        # fake.shape: torch.Size([16, 1, 16, 16])
        # discriminator(gen_hr).shape: torch.Size([16, 1, 16, 16])
        out = discriminator(gen_hr)

        nb, nc, nh, nw = out.shape
        output_shape = (nc, nh, nw)
        valid = Variable(
            Tensor(np.ones((imgs_lr.size(0), *output_shape))), requires_grad=False
        )
        fake = Variable(
            Tensor(np.zeros((imgs_lr.size(0), *output_shape))), requires_grad=False
        )

        loss_GAN = criterion_GAN(out, valid)

        # Content loss
        gen_features = feature_extractor(gen_hr)
        real_features = feature_extractor(imgs_hr)
        loss_content = criterion_content(gen_features, real_features.detach())
        # loss_content = criterion_content(gen_hr, imgs_hr)

        # Total loss
        if opt.nofeatureloss:
            loss_G = loss_GAN
        else:
            loss_G = loss_content + 1e-3 * loss_GAN

        loss_G.backward()
        optimizer_G.step()

        # ---------------------
        #  Train Discriminator
        # ---------------------

        optimizer_D.zero_grad()

        # Loss of real and fake images
        loss_real = criterion_GAN(discriminator(imgs_hr), valid)
        loss_fake = criterion_GAN(discriminator(gen_hr.detach()), fake)

        # Total loss
        loss_D = (loss_real + loss_fake) / 2

        loss_D.backward()
        optimizer_D.step()

        # --------------
        #  Log Progress
        # --------------
        _imgs_hr = imgs_hr.detach()
        _gen_hr = gen_hr.detach()
        _imgs_hr = torch.max(_imgs_hr, axis=1)[0]
        _gen_hr = torch.max(_gen_hr, axis=1)[0]

        abserr = torch.max(torch.abs(_gen_hr - _imgs_hr)).item()
        abs_list.append(abserr)
        logging.debug(
            "[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f] ABS: %f"
            % (
                epoch,
                epoch_end,
                i,
                len(dataloader),
                loss_D.item(),
                loss_G.item(),
                abserr,
            )
        )

        batches_done = epoch * len(dataloader) + i
        if batches_done % opt.sample_interval == 0:
            # Save image grid with upsampled inputs and SRGAN outputs
            nb, nc, nh, nw = imgs_hr.shape
            _imgs_lr = imgs_lr
            _imgs_hr = imgs_hr
            _gen_hr = gen_hr

            _imgs_lr = nn.functional.interpolate(
                _imgs_lr, scale_factor=4, mode="nearest"
            )
            _imgs_lr = _imgs_lr[:, :, :nh, :nw]

            _imgs_lr = torch.max(_imgs_lr, axis=1)[0].reshape(nb, 1, nh, nw)
            _imgs_hr = torch.max(_imgs_hr, axis=1)[0].reshape(nb, 1, nh, nw)
            _gen_hr = torch.max(_gen_hr, axis=1)[0].reshape(nb, 1, nh, nw)

            _gen_hr = make_grid(_gen_hr, nrow=1, normalize=True)
            _imgs_lr = make_grid(_imgs_lr, nrow=1, normalize=True)
            _imgs_hr = make_grid(_imgs_hr, nrow=1, normalize=True)
            img_grid = torch.cat((_imgs_lr, _imgs_hr, _gen_hr), -1)
            save_image(img_grid, "%s/%d.png" % (prefix, batches_done), normalize=False)

    logging.debug(
        "Epoch ABS error: %g %g %g"
        % (np.min(abs_list), np.mean(abs_list), np.max(abs_list))
    )
    if (epoch + 1) % opt.checkpoint_interval == 0:
        # Save model checkpoints
        fname0 = "%s/generator_%d.pth" % (prefix, epoch + 1)
        fname1 = "%s/discriminator_%d.pth" % (prefix, epoch + 1)
        torch.save(generator.state_dict(), fname0)
        torch.save(discriminator.state_dict(), fname1)
        logging.debug("Saved: %s %s" % (fname0, fname1))

# --------------
#  Recon
# --------------
generator.eval()
discriminator.eval()
gen_list = list()
abs_list = list()
with torch.no_grad():
    for i, imgs in enumerate(validationloader):
        # Configure model input
        imgs_lr = imgs[0].to(device)
        imgs_hr = imgs[1].to(device)
        nb, nc, nh, nw = imgs_hr.shape

        gen_hr = generator(imgs_lr)
        gen_hr = gen_hr[:, :, :nh, :nw]

        _gen_hr = gen_hr.detach()
        _gen_hr = torch.max(_gen_hr, axis=1)[0]
        _imgs_hr = imgs_hr.detach()
        _imgs_hr = torch.max(_imgs_hr, axis=1)[0]

        gen_list.append(_gen_hr.detach().cpu().numpy())

        abserr = torch.max(torch.abs(_gen_hr - _imgs_hr)).item()
        abs_list.append(abserr)
logging.debug(
    "Recon ABS error: %g %g %g"
    % (np.min(abs_list), np.mean(abs_list), np.max(abs_list))
)

Xbar = np.vstack(gen_list)
fname = "%s/recon.bp" % (prefix)

if opt.dataset == "nstx":
    with ad2.open(fname, "w") as fw:
        shape = Xbar.shape
        start = [
            0,
        ] * len(shape)
        count = shape
        fw.write("recon", Xbar.copy(), shape, start, count)
        shape = H.shape
        start = [
            0,
        ] * len(shape)
        count = shape
        fw.write("gpi", H.copy(), shape, start, count)

if opt.dataset == "xgc":
    ## Normalize
    xmin = np.min(Xbar, axis=(1, 2))
    xmax = np.max(Xbar, axis=(1, 2))
    Xbar = (Xbar - xmin[:, np.newaxis, np.newaxis]) / (xmax - xmin)[
        :, np.newaxis, np.newaxis
    ]

    ## Un-normalize
    X0 = (
        Xbar * ((zmax - zmin)[:, np.newaxis, np.newaxis])
        + zmin[:, np.newaxis, np.newaxis]
    )

    with ad2.open(fname, "w") as fw:
        shape = Xbar.shape
        start = [
            0,
        ] * len(shape)
        count = shape
        fw.write("Xbar", Xbar.copy(), shape, start, count)
        fw.write("X0", X0.copy(), shape, start, count)
        shape = zlb.shape
        start = [
            0,
        ] * len(shape)
        count = shape
        fw.write("zlb", zlb.copy(), shape, start, count)

logging.info("Recon saved: %s" % fname)
logging.info("Done.")
