"""WaveLink logo: remove the off-white background (keeping the logo's colours exact and its edges
clean), then write the full logo for light and dark backgrounds, the mark alone, and app icons.

    python3 tools/make_logo.py          (source: assets/wavelink-logo-source.png, the team's logo)

Matting: a pixel far from the background keeps its colour at full opacity.  An edge pixel (within a
few pixels of the background) is a blend of the background and the nearest solid colour S, so its
coverage is the projection of (white - P) on (white - S) and its colour is S.  The near-white
background, the white halo around the shapes and the white insides of the network nodes all become
transparent.
"""
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "assets", "wavelink-logo-source.png")
OUT_WEB = os.path.join(ROOT, "wavelink", "web", "static")
OUT_DOC = os.path.join(ROOT, "assets")

BG_T = 0.08            # 'distance from white' below this is background (measured: 99.9 % of it < 0.036,
                       # a few tinted specks at the top edge up to 0.067)
GAP_ROWS = (493, 525)  # empty band between the mark and the word "WaveLink"
WAVE_END_X = 468       # gap between "Wave" and "Link" (x 462-473)


def matte(rgb):
    W = 255.0
    d = (W - rgb).max(-1) / W                    # 0 for white, 1 for a fully saturated or dark colour
    bg = d < BG_T
    shape = ~bg
    # the most 'inked' pixel in each 5 x 5 neighbourhood: the solid colour an edge pixel is blended from
    best_d = np.full(d.shape, -1.0, np.float32)
    best_c = np.zeros_like(rgb)
    H, Wd = d.shape
    pad = 2
    dp = np.pad(d, pad, constant_values=0)
    cp = np.pad(rgb, ((pad, pad), (pad, pad), (0, 0)), constant_values=255)
    for dy in range(-pad, pad + 1):
        for dx in range(-pad, pad + 1):
            dd = dp[pad + dy:pad + dy + H, pad + dx:pad + dx + Wd]
            cc = cp[pad + dy:pad + dy + H, pad + dx:pad + dx + Wd]
            m = dd > best_d
            best_d[m] = dd[m]
            best_c[m] = cc[m]
    near_bg = ndimage.binary_dilation(bg, iterations=3)
    edge = shape & near_bg
    alpha = np.where(shape, 1.0, 0.0).astype(np.float32)
    color = rgb.copy()
    v = W - rgb[edge]
    s = W - best_c[edge]
    cov = np.clip((v * s).sum(-1) / np.maximum((s * s).sum(-1), 1e-6), 0, 1)
    alpha[edge] = cov
    color[edge] = best_c[edge]
    alpha[alpha < 0.04] = 0
    # specks: anything smaller than a dozen pixels is noise, not logo
    lab, n = ndimage.label(alpha > 0)
    if n:
        sizes = ndimage.sum(np.ones_like(alpha), lab, range(1, n + 1))
        alpha[np.isin(lab, 1 + np.where(sizes < 12)[0])] = 0
    color[alpha == 0] = 0                        # (compresses better)
    return color, alpha


def to_img(color, alpha):
    a = np.dstack([np.clip(color, 0, 255), np.clip(alpha * 255, 0, 255)]).round().astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def bbox(alpha, y0=0, y1=None, pad=6):
    sub = alpha[y0:y1]
    ys, xs = np.where(sub > 0.02)
    return (max(0, xs.min() - pad), y0 + max(0, ys.min() - pad), xs.max() + 1 + pad, y0 + ys.max() + 1 + pad)


def save(img, path, size=None):
    if size:
        img = img.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, optimize=True)
    print(f"  {os.path.relpath(path, ROOT)}  {img.size[0]}x{img.size[1]}  {os.path.getsize(path) // 1024} KB")


def square(img, side=None):
    w, h = img.size
    s = max(w, h)
    sq = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sq.paste(img, ((s - w) // 2, (s - h) // 2))
    return sq if not side else sq.convert("RGBa").resize((side, side), Image.LANCZOS).convert("RGBA")


def main():
    src = np.asarray(Image.open(SRC).convert("RGB")).astype(np.float32)
    color, alpha = matte(src)
    # dark-background version: the word "Wave" (navy) in near white; the mark keeps its colours
    # (the page puts a soft glow behind it on a dark background)
    dark = color.copy()
    word = np.zeros(alpha.shape, bool)
    word[GAP_ROWS[1]:, :WAVE_END_X] = True
    dark[word & (alpha > 0)] = (234, 241, 255)
    light_img, dark_img = to_img(color, alpha), to_img(dark, alpha)

    full = bbox(alpha, pad=8)
    mark = bbox(alpha, 0, GAP_ROWS[0], pad=4)
    print("full logo box", full, "mark box", mark)
    fw, fh = full[2] - full[0], full[3] - full[1]
    web = (520, round(fh * 520 / fw))
    save(light_img.crop(full), os.path.join(OUT_WEB, "wavelink-logo-light.png"), web)
    save(dark_img.crop(full), os.path.join(OUT_WEB, "wavelink-logo-dark.png"), web)
    save(square(light_img.crop(mark), 384), os.path.join(OUT_WEB, "wavelink-mark.png"))
    save(square(light_img.crop(mark), 192), os.path.join(OUT_WEB, "wavelink-icon-192.png"))
    save(square(light_img.crop(mark), 64), os.path.join(OUT_WEB, "wavelink-favicon-64.png"))
    save(light_img.crop(full), os.path.join(OUT_DOC, "wavelink-logo.png"), None)
    save(dark_img.crop(full), os.path.join(OUT_DOC, "wavelink-logo-dark.png"), None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
