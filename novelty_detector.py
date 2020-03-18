# Copyright 2018-2020 Stanislav Pidhorskyi
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#  http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import print_function
import torch.utils.data
from torchvision.utils import save_image
from net import *
from torch.autograd import Variable
from utils.jacobian import compute_jacobian
import numpy as np
import logging
import scipy.optimize
import pickle
from dataloading import make_datasets, make_dataloader, create_set_with_outlier_percentage
from defaults import get_cfg_defaults
from evaluation import get_f1, evaluate
from utils.threshold_search import find_maximum
from utils.save_plot import save_plot
import matplotlib.pyplot as plt
import scipy.stats
from scipy.special import loggamma
from timeit import default_timer as timer
from scipy.optimize import minimize


def r_pdf(x, bins, counts):
    if bins[0] < x < bins[-1]:
        i = np.digitize(x, bins) - 1
        return max(counts[i], 1e-308)
    if x < bins[0]:
        return max(counts[0] * x / bins[0], 1e-308)
    return 1e-308


def extract_statistics(cfg, train_set, inliner_classes, E, G):
    zlist = []
    rlist = []

    data_loader = make_dataloader(train_set, cfg.TEST.BATCH_SIZE, torch.cuda.current_device())

    for label, x in data_loader:
        x = x.view(-1, cfg.MODEL.INPUT_IMAGE_SIZE * cfg.MODEL.INPUT_IMAGE_SIZE)
        z = E(x.view(-1, 1, cfg.MODEL.INPUT_IMAGE_SIZE, cfg.MODEL.INPUT_IMAGE_SIZE))
        recon_batch = G(z)
        z = z.squeeze()

        recon_batch = recon_batch.squeeze().cpu().detach().numpy()
        x = x.squeeze().cpu().detach().numpy()

        z = z.cpu().detach().numpy()

        for i in range(x.shape[0]):
            distance = np.linalg.norm(x[i].flatten() - recon_batch[i].flatten())
            rlist.append(distance)

        zlist.append(z)

    zlist = np.concatenate(zlist)

    counts, bin_edges = np.histogram(rlist, bins=30, normed=True)

    if cfg.MAKE_PLOTS:
        plt.plot(bin_edges[1:], counts, linewidth=2)
        save_plot(r"Distance, $\left \|\| I - \hat{I} \right \|\|$",
                  'Probability density',
                  r"PDF of distance for reconstruction error, $p\left(\left \|\| I - \hat{I} \right \|\| \right)$",
                  'mnist_%s_reconstruction_error.pdf' % ("_".join([str(x) for x in inliner_classes])))

    for i in range(cfg.MODEL.LATENT_SIZE):
        plt.hist(zlist[:, i], bins='auto', histtype='step')

    if cfg.MAKE_PLOTS:
        save_plot(r"$z$",
                  'Probability density',
                  r"PDF of embeding $p\left(z \right)$",
                  'mnist_%s_embedding.pdf' % ("_".join([str(x) for x in inliner_classes])))

    def fmin(func, x0, args, disp):
        x0 = [2.0, 0.0, 1.0]
        return scipy.optimize.fmin(func, x0, args, xtol=1e-12, ftol=1e-12, disp=0)

    gennorm_param = np.zeros([3, cfg.MODEL.LATENT_SIZE])
    for i in range(cfg.MODEL.LATENT_SIZE):
        betta, loc, scale = scipy.stats.gennorm.fit(zlist[:, i], optimizer=fmin)
        gennorm_param[0, i] = betta
        gennorm_param[1, i] = loc
        gennorm_param[2, i] = scale

    return counts, bin_edges, gennorm_param


def main(folding_id, inliner_classes, ic, total_classes, mul, folds=5):
    cfg = get_cfg_defaults()
    cfg.merge_from_file('configs/mnist.yaml')
    cfg.freeze()

    logger = logging.getLogger("logger")

    torch.set_default_tensor_type('torch.cuda.FloatTensor')
    device = torch.cuda.current_device()
    print("Running on ", torch.cuda.get_device_name(device))

    train_set, valid_set, test_set = make_datasets(cfg, folding_id, inliner_classes)

    print('Validation set size: %d' % len(valid_set))
    print('Test set size: %d' % len(test_set))

    train_set.shuffle()

    G = Generator(cfg.MODEL.LATENT_SIZE, channels=cfg.MODEL.INPUT_IMAGE_CHANNELS)
    E = Encoder(cfg.MODEL.LATENT_SIZE, channels=cfg.MODEL.INPUT_IMAGE_CHANNELS)

    G.load_state_dict(torch.load("models/Gmodel_%d_%d.pkl" %(folding_id, ic)))
    E.load_state_dict(torch.load("models/Emodel_%d_%d.pkl" %(folding_id, ic)))

    G.eval()
    E.eval()

    sample = torch.randn(64, cfg.MODEL.LATENT_SIZE).to(device)
    sample = G(sample.view(-1, cfg.MODEL.LATENT_SIZE, 1, 1)).cpu()
    save_image(sample.view(64, cfg.MODEL.INPUT_IMAGE_CHANNELS, cfg.MODEL.INPUT_IMAGE_SIZE, cfg.MODEL.INPUT_IMAGE_SIZE), 'sample.png')

    counts, bin_edges, gennorm_param = extract_statistics(cfg, train_set, inliner_classes, E, G)

    N = (cfg.MODEL.INPUT_IMAGE_SIZE * cfg.MODEL.INPUT_IMAGE_SIZE - cfg.MODEL.LATENT_SIZE)
    logC = loggamma(N / 2.0) - (N / 2.0) * np.log(2.0 * np.pi)

    def run_novely_prediction_on_dataset(dataset, percentage, concervative=False):
        dataset.shuffle()
        dataset = create_set_with_outlier_percentage(dataset, inliner_classes, percentage, concervative)

        result = []
        gt_novel = []

        data_loader = make_dataloader(dataset, cfg.TEST.BATCH_SIZE, torch.cuda.current_device())

        include_jacobian = True

        N = cfg.MODEL.INPUT_IMAGE_CHANNELS * cfg.MODEL.INPUT_IMAGE_SIZE * cfg.MODEL.INPUT_IMAGE_SIZE - cfg.MODEL.LATENT_SIZE
        logC = loggamma(N / 2.0) - (N / 2.0) * np.log(2.0 * np.pi)

        def logPe_func(x):
            # p_{\|W^{\perp}\|} (\|w^{\perp}\|)
            # \| w^{\perp} \|}^{m-n}
            return logC - (N - 1) * np.log(x), np.log(r_pdf(x, bin_edges, counts))

        for label, x in data_loader:
            x = x.view(-1, cfg.MODEL.INPUT_IMAGE_CHANNELS * cfg.MODEL.INPUT_IMAGE_SIZE * cfg.MODEL.INPUT_IMAGE_SIZE)
            x = Variable(x.data, requires_grad=True)

            z = E(x.view(-1, cfg.MODEL.INPUT_IMAGE_CHANNELS, cfg.MODEL.INPUT_IMAGE_SIZE, cfg.MODEL.INPUT_IMAGE_SIZE))
            recon_batch = G(z)
            z = z.squeeze()

            if include_jacobian:
                # J = compute_jacobian(x, z)
                # J = J.cpu().numpy()

                with torch.no_grad():
                    J = torch.zeros(cfg.MODEL.LATENT_SIZE, x.shape[0], N, requires_grad=False)
                    J += recon_batch.view(1, -1, N)

                    epsilon = 1e-3
                    for i in range(cfg.MODEL.LATENT_SIZE):
                        z_onehot = np.zeros([1, cfg.MODEL.LATENT_SIZE], dtype=np.float32)
                        z_onehot[0, i] = epsilon
                        z_onehot = torch.tensor(z_onehot, dtype=torch.float32)
                        _z = z + z_onehot
                        d_recon_batch = G(_z.view(-1, cfg.MODEL.LATENT_SIZE, 1, 1))
                        J[i] -= d_recon_batch.view(-1, N)

                    J /= epsilon

                    J = torch.transpose(J, dim0=0, dim1=1)
                    J = torch.transpose(J, dim0=1, dim1=2)

                    J = J.cpu().numpy()

            z = z.cpu().detach().numpy()

            recon_batch = recon_batch.squeeze().cpu().detach().numpy()
            x = x.squeeze().cpu().detach().numpy()

            for i in range(x.shape[0]):
                if include_jacobian:
                    u, s, vh = np.linalg.svd(J[i, :, :], full_matrices=False)
                    logD = -np.sum(np.log(np.abs(1.0 / s)))  # | \mathrm{det} S^{-1} |
                    # logD = np.log(np.abs(1.0/(np.prod(s))))
                else:
                    logD = 0

                p = scipy.stats.gennorm.pdf(z[i], gennorm_param[0, :], gennorm_param[1, :], gennorm_param[2, :])
                logPz = np.sum(np.log(p))

                # Sometimes, due to rounding some element in p may be zero resulting in Inf in logPz
                # In this case, just assign some large negative value to make sure that the sample
                # is classified as unknown.
                if not np.isfinite(logPz):
                    logPz = -1000

                distance = np.linalg.norm(x[i].flatten() - recon_batch[i].flatten())

                logPe_p1, logPe_p2 = logPe_func(distance)

                result.append((logD, logPz, logPe_p1, logPe_p2))
                gt_novel.append(label[i].item() in inliner_classes)

        result = np.asarray(result, dtype=np.float32)
        ground_truth = np.asarray(gt_novel, dtype=np.float32)
        return result, ground_truth

    def compute_threshold_coeffs(valid_set, percentage):
        y_scores_components, y_true = run_novely_prediction_on_dataset(valid_set, percentage, concervative=True)

        y_scores_components = np.asarray(y_scores_components, dtype=np.float32)

        def evaluate(threshold, alpha):
            coeff = np.asarray([[1, 1, alpha, 1]], dtype=np.float32)
            y_scores = (y_scores_components * coeff).mean(axis=1)

            y_false = np.logical_not(y_true)

            y = np.greater(y_scores, e)
            true_positive = np.sum(np.logical_and(y, y_true))
            false_positive = np.sum(np.logical_and(y, y_false))
            false_negative = np.sum(np.logical_and(np.logical_not(y), y_true))
            return get_f1(true_positive, false_positive, false_negative)

        def func(x):
            threshold, alpha = x
            return -evaluate(threshold, alpha)

        res = minimize(func, [0, 0.2], method='Nelder-Mead', options={
            'disp': True,
            'maxiter': None,
            'xatol': 0.01,
            'fatol': 0.0001,
            'maxfev': 10000
        })

        threshold, alpha = res.x

        best_f1 = evaluate(threshold, alpha)

        logger.info("Best e: %f Best a: %f best f1: %f" % (threshold, alpha, best_f1))
        return alpha, threshold

    def test(test_set, percentage, threshold, alpha):
        y_scores_components, y_true = run_novely_prediction_on_dataset(test_set, percentage, concervative=True)
        y_scores_components = np.asarray(y_scores_components, dtype=np.float32)
        coeff = np.asarray([[1, 1, alpha, 1]], dtype=np.float32)
        y_scores = (y_scores_components * coeff).mean(axis=1)

        return evaluate(logger, percentage, inliner_classes, y_scores, threshold, y_true)

    # percentages = cfg.DATASET.PERCENTAGES
    percentages = [50]

    results = {}

    for p in percentages:
        plt.figure(num=None, figsize=(8, 6), dpi=180, facecolor='w', edgecolor='k')
        a, e = compute_threshold_coeffs(valid_set, p)
        results[p] = test(test_set, p, e, a)

    return results

