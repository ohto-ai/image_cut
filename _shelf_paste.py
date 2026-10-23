# -*- coding: utf-8 -*-
"""把 微信图片_..._80_89.jpg 里的黑色壁挂架（连同上面的水平尺）抠出来，
透视校正后贴到两张白墙照片上做效果预览。

尺寸依据：源图里那块黑色双联 86 型面板，两个模块共 172mm ≈ 356px -> 2.07 px/mm，
据此推得架子实宽约 476mm。

贴合方式：目标照片是"斜切"的——插座面板的横线斜 5.7°，但它自己的竖线在照片里是正的。
所以不能只做旋转，要用单应变换把架子压成同样的平行四边形（长边随横线、端头随竖线），
再按照片里的透视收缩让右端略矮。
"""
import numpy as np
from PIL import Image, ImageFilter

SRC = '微信图片_20261004143517_80_89.jpg'
CUT = '_shelf_cut.png'

BAR_W_MM = 476.0        # 架子实宽（按源图 86 型面板标定）
HUP = 145               # 校正画布里，架子上沿以上的行数（留给水平尺）
SOFTEN = 0.9            # 整体柔化（源图压缩噪点太"锐"，和照片质感不搭）
# 注意：贴上去的内容必须是源图里真实存在的东西（架子 + 水平尺），
# 不要自己加螺丝之类的配件。


# ---------- 几何工具 ----------
def homography(src, dst):
    """求 3x3 矩阵 H，使 dst ~ H @ src（齐次坐标）。src/dst 为 4x2。"""
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    _, _, Vt = np.linalg.svd(np.asarray(A, float))
    return Vt[-1].reshape(3, 3)


def bilinear(img, xs, ys):
    """img: HxWxC float；xs/ys 浮点坐标，越界按边缘截断。"""
    H, W = img.shape[:2]
    xs = np.clip(xs, 0, W - 1.001)
    ys = np.clip(ys, 0, H - 1.001)
    x0, y0 = np.floor(xs).astype(int), np.floor(ys).astype(int)
    x1, y1 = x0 + 1, y0 + 1
    wx, wy = xs - x0, ys - y0
    out = np.zeros(xs.shape + (img.shape[2],), float)
    for c in range(img.shape[2]):
        out[..., c] = (img[y0, x0, c] * (1 - wx) * (1 - wy) + img[y0, x1, c] * wx * (1 - wy) +
                       img[y1, x0, c] * (1 - wx) * wy + img[y1, x1, c] * wx * wy)
    return out


def warp_homography(arr, Hm):
    """把 RGBA 数组按单应矩阵 Hm（源->目标）重采样，返回 (图像, 左上角坐标)。
    用预乘 alpha，避免透明边缘出现亮边。"""
    H, W = arr.shape[:2]
    pm = arr.astype(float).copy()
    pm[..., :3] *= pm[..., 3:4] / 255.0
    corners = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], float)
    ch = np.hstack([corners, np.ones((4, 1))])
    dc = (Hm @ ch.T).T
    dc = dc[:, :2] / dc[:, 2:3]
    lo = np.floor(dc.min(0))
    W2, H2 = int(np.ceil(dc.max(0)[0] - lo[0])) + 1, int(np.ceil(dc.max(0)[1] - lo[1])) + 1
    u, v = np.meshgrid(np.arange(W2), np.arange(H2))
    p = np.stack([u.ravel() + lo[0], v.ravel() + lo[1], np.ones(u.size)], 1)
    q = (np.linalg.inv(Hm) @ p.T).T
    q = q[:, :2] / q[:, 2:3]
    out = bilinear(pm, q[:, 0].reshape(H2, W2), q[:, 1].reshape(H2, W2))
    a = out[..., 3:4]
    out[..., :3] = np.where(a > 1e-3, out[..., :3] / np.maximum(a, 1e-3) * 255.0, 0)
    return out, lo


# ---------- 1. 抠图：架子 + 水平尺 + 螺丝 ----------
def _rectify(a, W, Hbar, Hup):
    """按架子四角把源图校正成正视。"""
    top = lambda x: 1285.0 + 0.0600 * (x - 170)
    bot = lambda x: 1393.0 + 0.0768 * (x - 170)
    xL, xR = 163, 1148
    H = Hup + Hbar
    quad = [(xL, top(xL)), (xR, top(xR)), (xR, bot(xR)), (xL, bot(xL))]
    rect = np.array([[0, Hup], [W - 1, Hup], [W - 1, Hup + Hbar - 1], [0, Hup + Hbar - 1]], float)
    Hm = homography(rect, np.asarray(quad, float))
    u, v = np.meshgrid(np.arange(W), np.arange(H))
    Q = Hm @ np.stack([u.ravel(), v.ravel(), np.ones(u.size)]).astype(float)
    return bilinear(a, (Q[0] / Q[2]).reshape(H, W), (Q[1] / Q[2]).reshape(H, W))


def extract_shelf():
    a = np.asarray(Image.open(SRC).convert('RGB')).astype(float)
    W = int(round(1148 - 163))
    Hbar = int(round(((1393.0 - 1285.0) + (1466.0 - 1342.0)) / 2))
    rgb = _rectify(a, W, Hbar, HUP)
    H = HUP + Hbar
    lum = rgb.mean(axis=2)
    uu, vv = np.meshgrid(np.arange(W), np.arange(H))

    # --- 架子：亮度 -> alpha（墙亮=透明，黑架子=不透明，长圆孔透出墙面）---
    edge = np.minimum.reduce([uu, W - 1 - uu, vv, H - 1 - vv]).astype(float)
    a_bar = np.clip((150.0 - lum) / (150.0 - 95.0), 0, 1) * np.clip(edge / 1.5, 0, 1)
    a_bar[:HUP] = 0

    # --- 水平尺：逐列取轮廓（保留内部高光/水泡细节）---
    lv = lum[:HUP]
    cols_top = np.full(W, -1)
    cols_bot = np.full(W, -1)
    lim = HUP - 6
    for x in range(W):
        d = np.where(lv[:lim, x] < 205)[0]
        if len(d) >= 8:
            cols_top[x] = d.min() + 1          # 内缩 1px，丢掉源图里与白墙混色的抗锯齿边
            cols_bot[x] = d.max() - 1 if d.max() < lim - 1 else HUP - 1
    on = np.where(cols_top >= 0)[0]
    cols_top[on.min()] = -1                    # 左右两端同样内缩
    cols_top[on.max()] = -1
    yy = np.arange(HUP)[:, None]
    a_lv = ((yy >= cols_top[None, :]) & (yy <= cols_bot[None, :]) & (cols_top[None, :] >= 0))
    a_lv = np.asarray(Image.fromarray((a_lv * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(0.7))).astype(float) / 255.0

    # --- 锐度统一：水平尺在源图里比架子糊，先提一提；再整体柔化，压掉压缩噪点 ---
    sharp = np.asarray(Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).filter(
        ImageFilter.UnsharpMask(radius=2.2, percent=110, threshold=2))).astype(float)
    k = np.clip((HUP - np.arange(H)[:, None]) / 4.0, 0, 1)      # 只在水平尺区域生效
    rgb = rgb * (1 - k[..., None]) + sharp * k[..., None]
    rgb = np.asarray(Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(SOFTEN))).astype(float)

    alpha = np.maximum(a_bar, np.vstack([a_lv, np.zeros((Hbar, W))]))
    return rgb, alpha, Hbar


# ---------- 2. 贴到墙上 ----------
def paste_shelf(wall_path, out_path, anchor, bar_w_px, slope=-0.100, taper=0.07, shadow=0.30):
    """anchor: 架子顶边中点在照片上的坐标。
    slope: 长边在照片里的斜率；taper: 右端相对左端的高度收缩（照片的透视）。"""
    wall = Image.open(wall_path).convert('RGB')

    rgb, alpha, Hbar = extract_shelf()
    arr = Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8), 'RGBA')

    scale = bar_w_px / rgb.shape[1]
    W2, H2 = int(round(rgb.shape[1] * scale)), int(round(rgb.shape[0] * scale))
    arr = np.asarray(arr.resize((W2, H2), Image.LANCZOS)).astype(float)

    # 目标四边形：上沿按 slope 走，两条端边竖直，右端按 taper 收短
    x0 = anchor[0] - bar_w_px / 2.0
    y0 = anchor[1]
    x1 = x0 + bar_w_px
    hL = (H2 - 1 - HUP * scale)
    hR = hL * (1 - taper)
    quad = [(x0, y0), (x1, y0 + slope * bar_w_px),
            (x1, y0 + slope * bar_w_px + hR), (x0, y0 + hL)]
    src_rect = np.array([[0, HUP * scale], [W2 - 1, HUP * scale],
                         [W2 - 1, H2 - 1], [0, H2 - 1]], float)
    warped, lo = warp_homography(arr, homography(src_rect, np.asarray(quad, float)))

    layer = Image.new('RGBA', wall.size, (0, 0, 0, 0))
    wi = Image.fromarray(np.clip(warped, 0, 255).astype(np.uint8), 'RGBA')
    layer.paste(wi, (int(round(lo[0])), int(round(lo[1]))), wi)

    if shadow:
        a = layer.split()[3].filter(ImageFilter.GaussianBlur(6))
        s = Image.new('RGBA', wall.size, (0, 0, 0, 0))
        s.putalpha(a.point(lambda p: int(p * shadow)))
        s = s.transform(wall.size, Image.AFFINE, (1, 0, -2, 0, 1, -5), resample=Image.BILINEAR)
        layer = Image.alpha_composite(s, layer)

    res = Image.alpha_composite(wall.convert('RGBA'), layer).convert('RGB')
    res.save(out_path, quality=95)
    print('saved %s  %s  架子宽 %dpx (%.0fmm)' % (out_path, res.size, bar_w_px, BAR_W_MM))


if __name__ == '__main__':
    rgb, alpha, Hbar = extract_shelf()
    Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8),
                    'RGBA').save(CUT)
    print('cutout', rgb.shape[1], rgb.shape[0], 'Hbar', Hbar)
