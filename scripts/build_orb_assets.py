"""把 AE 渲出的帧序列做成唤醒球能播的雪碧图。

为什么是这条路：手写渲染器（`desktop/src/core.ts`）在 Canvas 2D 里复刻 Element 3D 走到了
边界 —— 没有逐像素 UV、没有逐像素法向、没有 z-buffer、没有线性色空间，而素材那团光的质感
全部来自这四样。使用者的判断是「不要重复造轮子了，直接用他渲染出的东西」。

**不要 alpha，要黑底。** `预合成 3` 渲出来是不透明的（底下压着纯色 vignette），但球本来
就是加色（`lighter`）合成的，而加色下「黑」= 不贡献 ≡ alpha 0。所以底色减干净之后，黑底
RGB 序列和带 alpha 的序列在屏幕上等价 —— PNG 小得多，也不用跟输出模块的通道设置纠缠
（`Channels`/`Depth` 在 `setSettings()` 里是只读的）。

**背景必须逐帧减，不能用「全序列最小值」。** `预合成 3` 的两个调整图层带 4 个曝光关键帧，
背景**不是静态的**：min 取到的是背景最暗那一刻，其余帧减完剩下的差值就是一圈同心环 ——
使用者会看到的正是它。所以用 `desktop/ae-bgonly.jsx` 另存一份「球层全禁」的工程、渲同样
的帧范围，逐帧相减。

用法：
    python scripts/build_orb_assets.py .vox-ref-ae/p3_*.tif \\
        --bg ".vox-bg-ae/bg_*.tif" --start 96 --count 96 --fade 8 --name flow
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

# 生产尺寸：球体 140px @2× DPI = 280。格子取 256 —— 略小于 280，放大 1.09× 看不出，
# 但解码内存少 26%（雪碧图整张常驻，96 格 @296 是 35 MB，@256 是 26 MB）。
CELL = 256
# 减背景之后，判定「这里有光」的阈值（0–255）
LUM_FLOOR = 10


def read_rgb(path: str) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def flatten_bg(bg: np.ndarray, keep: float, inner: int = 430, feather: int = 90,
               outer: int = 560) -> np.ndarray:
    """把背景帧中心那团光按比例抹掉，只留底色。

    为什么需要：`预合成 3` 的底色层 `深色 蓝色 纯色 2` 上挂着 **Optical Flares**
    （位置正中、大小 800、纯白），那团光**是球的一部分**，不是背景。整层禁掉会连底色一起
    丢；完全不处理就会把光核当背景减掉 —— 六态的中心全是一个黑洞，一眼就能看到。

    `keep` 是光核**在前景里保留多少**（0–1）：
      · 1.0 = 背景的中心完全用底色替代 ⇒ 光核 100% 留在前景。使用者的判断是这样太亮：
        下采到 256px 之后它成了一个边缘清楚的白盘，「中心亮点太亮太突兀」。
      · 0.0 = 背景保持原样 ⇒ 光核被完整减掉 ⇒ 黑洞。
      · 0.5 = 保留一半，中心仍然是亮的但不再压掉周围的片体。
    过渡必须**羽化**（`feather`）：硬边界会在 r=inner 处留下一道可见的圆环。
    """
    h, w = bg.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(xx - w / 2, yy - h / 2)
    ring = bg[(r >= inner + feather) & (r < outer)]
    if ring.size == 0:
        return bg
    fill = ring.reshape(-1, bg.shape[2]).mean(axis=0)
    # k = 该像素处「用底色替代原背景」的比例：中心为 keep，羽化带线性降到 0
    k = np.clip((inner + feather - r) / max(1, feather), 0.0, 1.0) * keep
    k = k[..., None]
    return (bg * (1 - k) + fill * k).astype(bg.dtype)


def soften_core(frame: np.ndarray, cx: float, cy: float,
                radius: float = 150.0, blur: float = 46.0, mix: float = 0.92) -> np.ndarray:
    """把中心那团光**化开**，而不是减掉它。

    这修的是一个被我调错过两次的东西。中心的光来自底色层上的 **Optical Flares**（位置正中、
    大小 800、纯白）——它是球的一部分（中心光核），但它是一个**边缘清楚的小白盘**，下采到
    256px 之后读作「贴上去的亮点」，使用者的判断是「中心那个亮点太亮太突兀」。
    我先按比例把它减掉（`--core-keep 0.26`），结果是六态中心全变成**黑洞** —— 减法治不了
    「形状不对」这个病，只会换一个更糟的病。

    正解是**保留亮度、去掉边界**：对整帧做一次大半径高斯模糊，只在中心区域按羽化权重换成
    模糊版。亮度守恒（模糊不改总能量），而「清楚的圆盘边」被抹平 —— 与第九代那条「白热核
    不许有实色平台」是同一条道理，只是这次实色平台来自 AE 而不是我的代码。
    """
    pil = Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8))
    soft = np.asarray(pil.filter(ImageFilter.GaussianBlur(blur))).astype(np.float32)
    h, w = frame.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(xx - cx, yy - cy)
    k = np.clip((radius - r) / radius, 0.0, 1.0) ** 0.85 * mix
    return frame * (1 - k[..., None]) + soft * k[..., None]


def load_pair(fg: list[str], bg: list[str] | None, core_keep: float,
              soften: tuple[float, float, float] | None = None) -> np.ndarray:
    """读成 (N, H, W, 3) int16 —— 减完可能为负，先留符号位再 clip。"""
    first = read_rgb(fg[0])
    out = np.empty((len(fg), *first.shape), dtype=np.int16)
    for i, p in enumerate(fg):
        out[i] = read_rgb(p)
    if bg is not None:
        if len(bg) < len(fg):
            raise SystemExit(f"背景帧不够：{len(bg)} < {len(fg)}")
        for i in range(len(fg)):
            b = read_rgb(bg[i]).astype(np.float32)
            d = np.clip(read_rgb(fg[i]).astype(np.float32) - flatten_bg(b, core_keep), 0, 255)
            if soften is not None:
                d = soften_core(d, out.shape[2] / 2, out.shape[1] / 2, *soften)
            out[i] = np.clip(d, 0, 255).astype(np.int16)
    else:
        # 退路：没有背景序列时用全序列最小值。会留同心环，只用于快速试跑。
        out -= out.min(axis=0)
    return np.clip(out, 0, 255)


def make_loop(lit: np.ndarray, fade: int) -> np.ndarray:
    """把 N+fade 帧交叉淡化成 N 帧循环。

    素材的表达式含 `time*45` 这类单调项与 `wiggle`，**严格循环不存在**。交叉淡化在这种
    没有可跟踪特征点的流动画面上看不出接缝。混合在加色数据上做线性插值是对的（黑底）。

    权重取法保证两个接缝都连续：`out[0] = frames[N]`（接上 `out[N-1]` 之后的那一帧）、
    `out[fade] = frames[fade]`（接上原本的 `frames[fade+1]`）。
    """
    if fade <= 0:
        return lit
    n = lit.shape[0] - fade
    if n <= fade:
        raise SystemExit(f"帧数不够做 {fade} 帧交叉淡化")
    out = lit[:n].astype(np.float32).copy()
    for i in range(fade):
        a = i / fade
        out[i] = out[i] * a + lit[n + i].astype(np.float32) * (1.0 - a)
    return np.clip(out, 0, 255).astype(np.uint8)


def union_square(lit: np.ndarray) -> tuple[int, int, int]:
    """全序列的并集包围盒 → 一个正方形 (x0, y0, size)。

    裁剪框对整段序列**固定**：逐帧跟着球裁会把「球在漫游」变成「球不动、背景在动」。
    """
    lum = lit.max(axis=3).max(axis=0)
    ys, xs = np.where(lum > LUM_FLOOR)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    size = max(x1 - x0, y1 - y0) + 1
    h, w = lum.shape
    size = min(size, 2 * min(cx, cy, w - cx, h - cy))
    return cx - size // 2, cy - size // 2, size


def build_sheet(lit: np.ndarray, box: tuple[int, int, int], gamma: float
                ) -> tuple[Image.Image, int, int]:
    """裁方 → 下采到 CELL → 拼雪碧图。**输出 RGBA，alpha 由亮度经 gamma 提升而来。**

    为什么必须带 alpha：`lighter` 的合成公式对 alpha 也是加法（`Sa + Da`），所以往一张
    透明画布上加色画一张**不透明**的黑底图，整个 drawImage 的矩形区域 alpha 会被推到 1
    —— 颜色对了（黑处 dst 不变），但那块方形从此不透明，桌面透不过来。渲染出来就是球外
    一个清楚的黑方块，浅色桌面上尤其明显。踩过一次。

    为什么 alpha 不直接取亮度：`alpha = max(R,G,B)` 让暗部几乎全透明，使用者的判断是
    「周围的看着太透明了，在深色背景下效果很不明显」—— 深色桌面上半透明的暗部等于不存在，
    球只剩中心一小团。`gamma < 1` 把中间调的 alpha 抬起来（0.62 时 lum=64 → alpha=108），
    而纯黑仍然精确地是 alpha 0，所以「黑 ≡ 透明」这条等价关系没有被破坏。
    """
    x0, y0, size = box
    n = lit.shape[0]
    cols = int(np.ceil(np.sqrt(n)))
    rows = int(np.ceil(n / cols))
    sheet = Image.new("RGBA", (cols * CELL, rows * CELL), (0, 0, 0, 0))
    for i in range(n):
        cut = lit[i, y0:y0 + size, x0:x0 + size]
        lum = cut.max(axis=2).astype(np.float32) / 255.0
        alpha = np.clip(np.power(lum, gamma) * 255.0, 0, 255)
        rgba = np.dstack([cut, alpha])
        cell = Image.fromarray(rgba.astype(np.uint8), "RGBA").resize(
            (CELL, CELL), Image.LANCZOS)
        sheet.paste(cell, ((i % cols) * CELL, (i // cols) * CELL))
    return sheet, cols, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", nargs="+")
    ap.add_argument("--bg", default=None, help="背景帧的 glob（ae-bgonly.jsx 渲出来的那一份）")
    ap.add_argument("--bg-offset", type=int, default=0,
                    help="背景序列的起始帧号偏移 —— 背景可以只渲用得到的那一段，不必从 0 渲起")
    ap.add_argument("--core-keep", type=float, default=0.5,
                    help="中心光核在前景里保留多少（0–1）。1 = 全留（下采后是个刺眼的白盘），"
                         "0 = 全减（中心成黑洞）。0.5 是使用者反馈「太亮太突兀」之后的落点。")
    ap.add_argument("--core-soft", default="150,46,0.92",
                    help="中心光核柔化 radius,blur,mix（源像素）。**不是减法** —— 减掉光核会得到"
                         "黑洞，柔化是把那个边缘清楚的白盘化开而亮度守恒。0,0,0 关闭。")
    ap.add_argument("--alpha-gamma", type=float, default=0.62,
                    help="alpha = (亮度)^gamma。<1 抬高暗部 alpha —— 深色桌面上半透明的暗部"
                         "等于不存在，球会只剩中心一团。纯黑仍然精确为 alpha 0。")
    ap.add_argument("--start", type=int, default=0, help="从第几帧起 —— 跳过淡入相")
    ap.add_argument("--count", type=int, default=96, help="要几帧（不含 fade 的余量）")
    ap.add_argument("--fade", type=int, default=8, help="交叉淡化的帧数")
    ap.add_argument("--name", default="flow")
    ap.add_argument("--crop", default=None,
                    help="x0,y0,size —— 多段序列必须共用同一个框，否则切段时球会跳大小。"
                         "素材的爆发相宽 640、平缓相宽 400，各自裁各自的包围盒会把爆发感抹平。")
    ap.add_argument("--out", default="desktop/public/orb")
    args = ap.parse_args()

    allfg = sorted(p for pat in args.frames for p in glob.glob(pat))
    if not allfg:
        raise SystemExit("没有匹配到帧")
    need = args.count + args.fade
    fg = allfg[args.start:args.start + need]
    if len(fg) < need:
        raise SystemExit(f"帧不够：要 {need}（{args.count}+{args.fade}），从第 {args.start} 帧起只有 {len(fg)}")
    bg = None
    if args.bg:
        allbg = sorted(glob.glob(args.bg))
        o = args.start - args.bg_offset
        if o < 0:
            raise SystemExit(f"背景序列从第 {args.bg_offset} 帧起，够不到 --start {args.start}")
        bg = allbg[o:o + need]
    print(f"core-keep     {args.core_keep}  core-soft {args.core_soft}   alpha-gamma {args.alpha_gamma}")
    print(f"frames        {len(fg)}  {os.path.basename(fg[0])} … {os.path.basename(fg[-1])}"
          f"   背景 {'逐帧' if bg else '全序列最小值(会留同心环)'}")

    cs = tuple(float(v) for v in args.core_soft.split(","))
    lit = load_pair(fg, bg, args.core_keep, None if cs[2] <= 0 else cs)
    print(f"source        {lit.shape[2]}x{lit.shape[1]}   减完 min/max {lit.min()}/{lit.max()}")

    lit = make_loop(lit, args.fade)
    print(f"loop          {lit.shape[0]} 帧（{args.fade} 帧交叉淡化）")

    if args.crop:
        px, py, ps = (int(v) for v in args.crop.split(","))
        box = (px, py, ps)
        print(f"crop          x0={px} y0={py} size={ps}（指定，多段共用）")
    else:
        box = union_square(lit)
        print(f"crop          x0={box[0]} y0={box[1]} size={box[2]}  球占源画幅 {box[2] / 2560:.1%} 宽")

    sheet, cols, rows = build_sheet(lit, box, args.alpha_gamma)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    png = outdir / f"{args.name}.png"
    sheet.save(png, optimize=True)
    kb = png.stat().st_size // 1024
    mem = sheet.width * sheet.height * 4 / 1e6  # RGBA 常驻位图
    print(f"sheet         {sheet.width}x{sheet.height}  {cols}x{rows} 格  {kb} KB  解码约 {mem:.0f} MB")

    meta = {"cell": CELL, "cols": cols, "rows": rows, "frames": int(lit.shape[0]), "fps": 24}
    (outdir / f"{args.name}.json").write_text(json.dumps(meta), encoding="utf-8")

    e = lit.reshape(lit.shape[0], -1).astype(np.float64).sum(axis=1)
    # 等效半径：亮度加权的到中心距离 —— 呼吸的载体是体积，这是它的读数
    c = CELL // 2
    yy, xx = np.mgrid[0:lit.shape[1], 0:lit.shape[2]]
    print(f"energy        min {e.min() / 1e6:.2f}M  max {e.max() / 1e6:.2f}M  摆动 {e.max() / max(e.min(), 1):.2f}×")
    d = np.abs(np.diff(lit.astype(np.int32), axis=0)).mean()
    print(f"frame delta   {d:.2f}/255  ← 逐帧平均差异，0 就是一张贴图")


if __name__ == "__main__":
    main()
