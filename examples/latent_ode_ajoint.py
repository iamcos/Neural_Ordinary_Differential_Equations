# odeint(func, z0, samp_ts)を読み解く


import os
import argparse
import logging
import time
import numpy as np
import numpy.random as npr
import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

# プログラム実行時にコマンドラインで引数を受け取る処理を簡単に実装できる標準ライブラリ
# https://qiita.com/kzkadc/items/e4fc7bc9c003de1eb6d0
parser = argparse.ArgumentParser()  # 2. パーサを作る

# 3. parser.add_argumentで受け取る引数を追加していく
parser.add_argument('--adjoint', type=eval, default=True)  # オプション引数（指定しなくても良い引数）を追加
parser.add_argument('--visualize', type=eval, default=True)  # default=False
parser.add_argument('--niters', type=int, default=2000)  # default=2000
parser.add_argument('--lr', type=float, default=0.01)
parser.add_argument('--gpu', type=int, default=0)
parser.add_argument('--train_dir', type=str, default=None)
args = parser.parse_args()  # 4. 引数を解析

if args.adjoint:
    from torchdiffeq import odeint_adjoint as odeint
else:
    from torchdiffeq import odeint


def generate_spiral2d(nspiral=1000,
                      ntotal=500,
                      nsample=100,
                      start=0.,
                      stop=1,  # approximately equal to 6pi
                      noise_std=.1,
                      a=0.,
                      b=1.,
                      savefig=True):
    """Parametric formula for 2d spiral is `r = a + b * theta`.

    Args:
      nspiral: number of spirals, i.e. batch dimension
      ntotal: total number of datapoints per spiral
      nsample: number of sampled datapoints for model fitting per spiral
      start: spiral starting theta value
      stop: spiral ending theta value
      noise_std: observation noise standard deviation
      a, b: parameters of the Archimedean spiral（アルキメデスの螺旋）
      savefig: plot the ground truth for sanity check（正当性のチェック）

    Returns: 
      Tuple where first element is true trajectory of size (nspiral, ntotal, 2),
      second element is noisy observations of size (nspiral, nsample, 2),
      third element is timestamps（日時の刻印） of size (ntotal,),
      and fourth element is timestamps of size (nsample,)
    """

    # add 1 all timestamps to avoid division by 0
    orig_ts = np.linspace(start, stop, num=ntotal)  # num 省略可能。デフォルト50。生成する配列(ndarray)の要素数を指定します。
    samp_ts = orig_ts[:nsample]

    # generate clock-wise and counter clock-wise spirals in observation space
    # with two sets of time-invariant（時不変系は、その出力が時間に明示的に依存していない系である。） latent dynamics
    # 時計回り（逆方向）
    zs_cw = stop + 1. - orig_ts  # 角度の回転と逆周りなので逆から考える，θを計算
    rs_cw = a + b * 50. / zs_cw  # rを計算
    xs, ys = rs_cw * np.cos(zs_cw) - 5., rs_cw * np.sin(zs_cw)  # 極座標をデカルト座標に変換
    orig_traj_cw = np.stack((xs, ys), axis=1)

    # 反時計回り（順方向）
    zs_cc = orig_ts
    rw_cc = a + b * zs_cc
    xs, ys = rw_cc * np.cos(zs_cc) + 5., rw_cc * np.sin(zs_cc)
    orig_traj_cc = np.stack((xs, ys), axis=1)

    if savefig:
        plt.figure()
        plt.plot(orig_traj_cw[:, 0], orig_traj_cw[:, 1], label='clock')#x座標,y座標
        plt.plot(orig_traj_cc[:, 0], orig_traj_cc[:, 1], label='counter clock')
        plt.legend()
        plt.savefig('./ground_truth.png', dpi=500)
        print('Saved ground truth spiral at {}'.format('./ground_truth.png'))

    # sample starting timestamps
    orig_trajs = []
    samp_trajs = []
    for _ in range(nspiral):
        # don't sample t0 very near the start or the end
        t0_idx = npr.multinomial(
            1, [1. / (ntotal - 2. * nsample)] * (ntotal - int(2 * nsample)))  # [0, 0, 0, ..1, ...0]
        t0_idx = np.argmax(t0_idx) + nsample  # 最大値のindexを返す

        cc = bool(npr.rand() > .5)  # uniformly select rotation，0<= npr.rand() <= 1
        orig_traj = orig_traj_cc if cc else orig_traj_cw
        orig_trajs.append(orig_traj)

        samp_traj = orig_traj[t0_idx:t0_idx + nsample, :].copy()
        cnt = samp_traj.shape  # (100,2) *をつけると100 2
        samp_traj += npr.randn(*cnt) * noise_std  # 標準正規分布 (平均0, 標準偏差1)
        samp_trajs.append(samp_traj)

    # batching for sample trajectories is good for RNN; batching for original
    # trajectories only for ease of indexing
    orig_trajs = np.stack(orig_trajs, axis=0)  # 3次元配列，nspiral*100*2
    samp_trajs = np.stack(samp_trajs, axis=0)  # 3次元配列，nspiral*100*2

    return orig_trajs, samp_trajs, orig_ts, samp_ts
    # orig_trajs: orig_traj_cc or orig_traj_cw　（アルキメデスの螺旋）　がnspiral個
    # sample_trajs: orig_trajsから取り出す．t0_idxからnsample個とる，ノイズも加える　がnspiral個
    # orig_ts: 時間間隔，startからstopまで
    # samp_ts: 時間間隔，orig_tsから取り出す．前半nsample個とる


class LatentODEfunc(nn.Module):

    def __init__(self, latent_dim=4, nhidden=20):
        super(LatentODEfunc, self).__init__()  # LatentODEfuncクラスのスーパークラスnn.Moduleを再利用
        self.elu = nn.ELU(inplace=True)  # 活性化関数
        # 線形結合を計算，回帰係数や切片項などのパラメータ,parameters=w
        self.fc1 = nn.Linear(latent_dim, nhidden)  # ノードからノードへの移り変わり
        self.fc2 = nn.Linear(nhidden, nhidden)
        self.fc3 = nn.Linear(nhidden, latent_dim)
        self.nfe = 0

    def forward(self, t, x):
        self.nfe += 1
        out = self.fc1(x)
        out = self.elu(out)
        out = self.fc2(out)
        out = self.elu(out)
        out = self.fc3(out)
        return out


class RecognitionRNN(nn.Module):

    def __init__(self, latent_dim=4, obs_dim=2, nhidden=25, nbatch=1):  # obs_dim = len(x, y)
        super(RecognitionRNN, self).__init__()
        self.nhidden = nhidden
        self.nbatch = nbatch
        self.i2h = nn.Linear(obs_dim + nhidden, nhidden)  # nn.Linearがいい感じに学習する
        self.h2o = nn.Linear(nhidden, latent_dim * 2)

    def forward(self, x, h):  # x = nspiral*2
        combined = torch.cat((x, h), dim=1)  # nn.Linearがいい感じに学習する
        h = torch.tanh(self.i2h(combined))
        out = self.h2o(h)
        return out, h

    def initHidden(self):
        return torch.zeros(self.nbatch, self.nhidden)


class Decoder(nn.Module):

    def __init__(self, latent_dim=4, obs_dim=2, nhidden=20):
        super(Decoder, self).__init__()
        self.relu = nn.ReLU(inplace=True)
        self.fc1 = nn.Linear(latent_dim, nhidden)
        self.fc2 = nn.Linear(nhidden, obs_dim)

    def forward(self, z):
        out = self.fc1(z)
        out = self.relu(out)
        out = self.fc2(out)
        return out


class RunningAverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, momentum=0.99):
        self.momentum = momentum
        self.reset()

    def reset(self):
        self.val = None
        self.avg = 0

    def update(self, val):
        if self.val is None:
            self.avg = val
        else:
            self.avg = self.avg * self.momentum + val * (1 - self.momentum)
        self.val = val


def log_normal_pdf(x, mean, logvar):  # samp_trajs, pred_x, noise_logvar
    const = torch.from_numpy(np.array([2. * np.pi])).float().to(x.device)
    const = torch.log(const)
    return -.5 * (const + logvar + (x - mean) ** 2. / torch.exp(logvar))


def normal_kl(mu1, lv1, mu2, lv2):  # qz0_mean, qz0_logvar, pz0_mean, pz0_logvar
    v1 = torch.exp(lv1)
    v2 = torch.exp(lv2)
    lstd1 = lv1 / 2.
    lstd2 = lv2 / 2.

    kl = lstd2 - lstd1 + ((v1 + (mu1 - mu2) ** 2.) / (2. * v2)) - .5
    return kl


if __name__ == '__main__':
    latent_dim = 4
    nhidden = 20  # 隠れユニット数
    rnn_nhidden = 25  # RNNの隠れユニット数
    obs_dim = 2
    nspiral = 999  # 1
    start = 0.
    stop = 6 * np.pi
    noise_std = .3
    a = 0.
    b = .3
    ntotal = 1000
    nsample = 400
    cnt = 0
    cri = 10
    # Tensor用にdeviceを定義
    device = torch.device('cuda:' + str(args.gpu)
                          if torch.cuda.is_available() else 'cpu')

    # generate toy spiral data

    # orig_trajs: orig_traj_cc or orig_traj_cw　（アルキメデスの螺旋）　がnspiral個
    # sample_trajs: orig_trajsから取り出す．t0_idxからnsample個とる，ノイズも加える　がnspiral個
    # orig_ts: 時間間隔，startからstopまで
    # samp_ts: 時間間隔，orig_tsから取り出す．前半nsample個とる

    orig_trajs, samp_trajs, orig_ts, samp_ts = generate_spiral2d(
        nspiral=nspiral,
        ntotal=ntotal,
        nsample=nsample,
        start=start,
        stop=stop,
        noise_std=noise_std,
        a=a, b=b
    )
    orig_trajs = torch.from_numpy(orig_trajs).float().to(device)  # Creates a Tensor from a numpy.ndarray.
    samp_trajs = torch.from_numpy(samp_trajs).float().to(device)
    samp_ts = torch.from_numpy(samp_ts).float().to(device)

    # model
    func = LatentODEfunc(latent_dim, nhidden).to(device)
    rec = RecognitionRNN(latent_dim, obs_dim, rnn_nhidden, nspiral).to(device)
    dec = Decoder(latent_dim, obs_dim, nhidden).to(device)
    f_p, d_p, r_p = list(func.parameters()), list(dec.parameters()), list(rec.parameters())
    params = (f_p + d_p + r_p)
    # f_p, d_p, r_pを最適化していく
    optimizer = optim.Adam(params, lr=args.lr)
    loss_meter = RunningAverageMeter()

    if args.train_dir is not None:
        if not os.path.exists(args.train_dir):
            os.makedirs(args.train_dir)
        ckpt_path = os.path.join(args.train_dir, 'ckpt.pth')
        if os.path.exists(ckpt_path):
            checkpoint = torch.load(ckpt_path)
            func.load_state_dict(checkpoint['func_state_dict'])
            rec.load_state_dict(checkpoint['rec_state_dict'])
            dec.load_state_dict(checkpoint['dec_state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            orig_trajs = checkpoint['orig_trajs']
            samp_trajs = checkpoint['samp_trajs']
            orig_ts = checkpoint['orig_ts']
            samp_ts = checkpoint['samp_ts']
            print('Loaded ckpt from {}'.format(ckpt_path))

    try:
        # w = net.parameters, func = net
        for itr in range(1, args.niters + 1):  # epoch数args.niters
            optimizer.zero_grad()
            # backward in time to infer q(z_0)
            h = rec.initHidden().to(device)  # h…層
            for t in reversed(range(samp_trajs.size(1))):  # バッチ数100，100回行う，外から内に行くので逆から
                obs = samp_trajs[:, t, :]  # その時刻をまとめて計算
                out, h = rec.forward(obs, h)  # 順伝播していく，ここでitrごとに値が変わっている
            # outはt = 0以外使っていない，hは前のものを伝播している

            # このあたりが謎（q:RNNの出力）-------------------------------------------------
            qz0_mean, qz0_logvar = out[:, :latent_dim], out[:, latent_dim:]
            epsilon = torch.randn(qz0_mean.size()).to(device)  # ε
            # z0:nspiral*latent_dim(4)
            z0 = epsilon * torch.exp(.5 * qz0_logvar) + qz0_mean
            # ---------------------------------------------------------------

            # forward in time and solve ode for reconstructions
            pred_z = odeint(func, z0, samp_ts).permute(1, 0, 2)  # odeint(func, y0, t)，ODESolverを使う
            # pred_x:nspiral*obs_dim(2)
            pred_x = dec(pred_z)  # デコードする

            # compute loss
            # このあたりが謎（何かしらのlossを計算している）-----------------
            noise_std_ = torch.zeros(pred_x.size()).to(device) + noise_std
            noise_logvar = 2. * torch.log(noise_std_).to(device)
            logpx = log_normal_pdf(
                samp_trajs, pred_x, noise_logvar).sum(-1).sum(-1)

            pz0_mean = pz0_logvar = torch.zeros(z0.size()).to(device)
            analytic_kl = normal_kl(qz0_mean, qz0_logvar,
                                    pz0_mean, pz0_logvar).sum(-1)
            # ---------------------------------------------------------------
            loss = torch.mean(-logpx + analytic_kl, dim=0)
            # lossのnet.parametersによる微分を計算
            loss.backward()
            # 勾配を計算する
            optimizer.step()
            loss_meter.update(loss.item())

            print('Iter: {}, running avg elbo: {:.4f}'.format(itr, -loss_meter.avg))

            if args.visualize:
                if itr % 100 == 0:
                    cnt += 1
                    cri = loss_meter.avg
                    with torch.no_grad():
                        # sample from trajectorys' approx. posterior
                        h = rec.initHidden().to(device)
                        for t in reversed(range(samp_trajs.size(1))):
                            obs = samp_trajs[:, t, :]
                            out, h = rec.forward(obs, h)
                        qz0_mean, qz0_logvar = out[:, :latent_dim], out[:, latent_dim:]
                        epsilon = torch.randn(qz0_mean.size()).to(device)
                        z0 = epsilon * torch.exp(.5 * qz0_logvar) + qz0_mean
                        if cnt == 1:
                            orig_ts = torch.from_numpy(orig_ts).float().to(device)

                        # take first trajectory for visualization
                        z0 = z0[0]

                        ts_pos = np.linspace(0., 2. * np.pi, num=2000)  # 正の予想（予測）
                        ts_neg = np.linspace(-2. * np.pi, 0., num=2000)[::-1].copy()  # 負の予想（外挿）
                        ts_pos = torch.from_numpy(ts_pos).float().to(device)
                        ts_neg = torch.from_numpy(ts_neg).float().to(device)

                        zs_pos = odeint(func, z0, ts_pos)
                        zs_neg = odeint(func, z0, ts_neg)

                        xs_pos = dec(zs_pos)  # 正の予測
                        # [::-1]
                        xs_neg = torch.flip(dec(zs_neg), dims=[0])  # 負の予測

                    xs_pos = xs_pos.cpu().numpy()
                    xs_neg = xs_neg.cpu().numpy()
                    orig_traj = orig_trajs[0].cpu().numpy()
                    samp_traj = samp_trajs[0].cpu().numpy()

                    plt.figure()
                    plt.plot(orig_traj[:, 0], orig_traj[:, 1], "g", label='true trajectory')
                    plt.plot(xs_pos[:, 0], xs_pos[:, 1], 'r',
                             label='learned trajectory (t>0)')
                    plt.plot(xs_neg[:, 0], xs_neg[:, 1], 'c',
                             label='learned trajectory (t<0)')
                    plt.scatter(samp_traj[:, 0], samp_traj[:, 1], label='sampled data', s=3, c='#ff7f00')
                    plt.legend()

                    plt.savefig('./400nsample/ajoint{}_{}.png'.format(itr, loss_meter.avg), dpi=500)


                    print('Saved visualization figure at {}'.format('./vis' + str(itr) + 'new.png'))

    except KeyboardInterrupt:
        if args.train_dir is not None:
            ckpt_path = os.path.join(args.train_dir, 'ckpt.pth')
            torch.save({
                'func_state_dict': func.state_dict(),
                'rec_state_dict': rec.state_dict(),
                'dec_state_dict': dec.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'orig_trajs': orig_trajs,
                'samp_trajs': samp_trajs,
                'orig_ts': orig_ts,
                'samp_ts': samp_ts,
            }, ckpt_path)
            print('Stored ckpt at {}'.format(ckpt_path))
    print('Training complete after {} iters.'.format(itr))