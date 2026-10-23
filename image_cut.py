#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image_cut —— 图像切割 / 拼接工具

把一个图像拆成 2 个子图（图形化拖线切割，也可直接输入数字），
改完之后再拼回原图。

关键设计：拆分时把"还原信息"同时写进两个子图内部（PNG iTXt 文本块）
和同目录的 sidecar JSON 里。即使子图被改名、被挪到别的目录、被 AI 工具
处理过，只要尺寸没变，拼接页就能自动认出它属于哪次拆分、该拼在哪一边。

零额外依赖：只需要 Pillow（tkinter 是 Python 自带的）。

直接运行：  python image_cut.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Literal, Optional

from PIL import Image, ImageOps, ImageTk, PngImagePlugin, UnidentifiedImageError

# --------------------------------------------------------------------------
# 第 1 层：核心逻辑（不碰 tkinter，可以单独跑）
# --------------------------------------------------------------------------

Axis = Literal["vertical", "horizontal"]   # vertical = 左|右，horizontal = 上|下
Box = tuple[int, int, int, int]            # (left, top, right, bottom)
Size = tuple[int, int]

META_KEY = "image_cut"
META_VERSION = 1
PART_RE = re.compile(r"_p([12])$", re.IGNORECASE)

AXIS_LABEL = {"vertical": "垂直（左 | 右）", "horizontal": "水平（上 | 下）"}


class ImageLoadError(Exception):
    """打不开图片时抛出，消息是给人看的。"""


class JoinError(Exception):
    """拼接失败。messages 里每一条都是一句可以直接给用户看的话。"""

    def __init__(self, messages: list[str]):
        super().__init__("\n".join(messages))
        self.messages = messages


def round_half_up(value: float) -> int:
    """四舍五入。

    不能用内置 round()——它是银行家舍入（round(0.5) == 0、round(1.5) == 2），
    拖动分割线时会出现"往右拖一格不动、再拖跳两格"的诡异手感。
    """
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def clamp_int(value, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def parse_number(raw: str) -> Optional[float]:
    """把输入框里的字符串解析成有限浮点数，解析不了就返回 None。

    用户在输入过程中会留下 "12."、"-"、"" 这类中间态；"inf"/"nan" 也能被
    float() 接受但会让后续取整炸掉，所以一并挡掉。
    """
    try:
        value = float(raw.strip())
    except (ValueError, AttributeError):
        return None
    return value if math.isfinite(value) else None


# ---------------------------- 打开图片 ------------------------------------

@dataclass
class LoadedImage:
    """一张已经规范化好、可以安全拿去显示和裁剪的图片。"""

    path: Path
    image: Image.Image            # 已 exif 转正 + 归一化，模式一定是 RGB 或 RGBA
    source_mode: str              # 文件本来的模式，用于在界面上说明"做过转换"
    exif_transposed: bool = False
    frame_count: int = 1
    icc: Optional[bytes] = None
    dpi: Optional[tuple] = None

    @property
    def size(self) -> Size:
        return self.image.size

    @property
    def was_converted(self) -> bool:
        return self.source_mode not in ("RGB", "RGBA")


def normalize_mode(im: Image.Image) -> Image.Image:
    """把任意模式转成 ImageTk 支持的 8bit RGB / RGBA。

    ImageTk.PhotoImage 只认 1/L/RGB/RGBA，其余会静默降级（16 位变 8 位灰度、
    CMYK 变 RGB、LA 丢 alpha），而且 P 模式会被它就地改写调色板。
    所以统一在这里转干净，后面所有环节都只跟 RGB/RGBA 打交道。
    """
    if im.mode in ("RGB", "RGBA"):
        return im
    if im.mode == "P":
        return im.convert("RGBA" if "transparency" in im.info else "RGB")
    if im.mode in ("LA", "PA", "La", "RGBa"):
        return im.convert("RGBA")
    return im.convert("RGB")


def open_image(path) -> LoadedImage:
    """打开图片并规范化。文件句柄在这里就释放掉。"""
    path = Path(path)
    try:
        # with + load + copy：Windows 上 Image.open 会一直占着文件句柄，
        # 不立刻释放的话用户随后在资源管理器里改名/覆盖这张图会失败。
        with Image.open(path) as im:
            frame_count = getattr(im, "n_frames", 1)
            if frame_count > 1:
                im.seek(0)
            orientation = 1
            try:
                orientation = im.getexif().get(0x0112, 1) or 1
            except Exception:
                pass
            icc = im.info.get("icc_profile")
            dpi = im.info.get("dpi")
            im.load()
            work = im.copy()
    except UnidentifiedImageError:
        raise ImageLoadError(f"认不出这个文件的图像格式：\n{path}")
    except Image.DecompressionBombError:
        raise ImageLoadError(
            f"图片太大了，出于安全考虑已拒绝加载：\n{path}\n\n"
            "（Pillow 默认上限约 1.79 亿像素）"
        )
    except FileNotFoundError:
        raise ImageLoadError(f"文件不存在：\n{path}")
    except PermissionError:
        raise ImageLoadError(f"没有权限读取这个文件（可能正被别的程序占用）：\n{path}")
    except OSError as exc:
        raise ImageLoadError(f"读取失败：\n{path}\n\n{exc}")

    source_mode = work.mode

    # EXIF 方向必须处理：否则预览是"躺着"的，而 AI 修图工具通常会自动转正，
    # 返回的图宽高互换，用户会撞上莫名其妙的尺寸不符。
    transposed = orientation not in (1, 0)
    if transposed:
        work = ImageOps.exif_transpose(work)

    work = normalize_mode(work)

    if not isinstance(dpi, (tuple, list)) or len(dpi) != 2:
        dpi = None
    else:
        try:
            dpi = (float(dpi[0]), float(dpi[1]))
        except (TypeError, ValueError):
            dpi = None

    return LoadedImage(
        path=path,
        image=work,
        source_mode=source_mode,
        exif_transposed=transposed,
        frame_count=frame_count,
        icc=icc,
        dpi=dpi,
    )


# ---------------------------- 拆分几何 ------------------------------------

def axis_length(axis: Axis, size: Size) -> int:
    """沿切割轴的长度，也就是 position 的取值范围 1..n-1。"""
    w, h = size
    return w if axis == "vertical" else h


def compute_boxes(axis: Axis, position: int, size: Size) -> tuple[Box, Box]:
    """前半块和后半块在原图里的坐标。position 是前半块沿轴的宽度。"""
    w, h = size
    if axis == "vertical":
        return (0, 0, position, h), (position, 0, w, h)
    return (0, 0, w, position), (0, position, w, h)


def part_sizes(axis: Axis, position: int, size: Size) -> tuple[Size, Size]:
    """两块的尺寸，用于界面上实时显示。"""
    w, h = size
    if axis == "vertical":
        return (position, h), (w - position, h)
    return (w, position), (w, h - position)


def split_image(image: Image.Image, axis: Axis, position: int) -> tuple[Image.Image, Image.Image]:
    box1, box2 = compute_boxes(axis, position, image.size)
    return image.crop(box1), image.crop(box2)


# ---------------------------- 拼接校验 ------------------------------------

@dataclass
class JoinCheck:
    ok: bool
    axis: Optional[Axis]
    reason: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def infer_axis(size1: Size, size2: Size) -> tuple[Optional[Axis], str]:
    """根据两块尺寸猜拼接方向。

    返回 (axis, 判定理由)。axis 为 None 表示完全判不出来。
    """
    w1, h1 = size1
    w2, h2 = size2
    same_h = h1 == h2
    same_w = w1 == w2
    if same_h and not same_w:
        return "vertical", f"两图高度相同（{h1}）→ 左右拼接"
    if same_w and not same_h:
        return "horizontal", f"两图宽度相同（{w1}）→ 上下拼接"
    if same_h and same_w:
        # 完全相同尺寸时两种猜测都成立，此时不该报错，给个默认值让用户自己改。
        return "vertical", f"两图尺寸完全相同（{w1}×{h1}）→ 默认按左右拼接，可手动切换"
    return None, f"两图尺寸对不上：{w1}×{h1} 与 {w2}×{h2}"


def harmonize_modes(
    im1: Image.Image, im2: Image.Image, name1: str, name2: str
) -> tuple[Image.Image, Image.Image, list[str]]:
    """把两张图统一到同一种模式。alpha 不一致只算提示，不算错误。"""
    warnings: list[str] = []
    m1, m2 = im1.mode, im2.mode
    if m1 == m2:
        return im1, im2, warnings

    target = "RGBA" if ("A" in m1.upper() or "A" in m2.upper()) else "RGB"
    if m1 != target:
        warnings.append(f"子图 1 已由 {m1} 转换为 {target}（原本没有 alpha 通道）")
        im1 = im1.convert(target)
    if m2 != target:
        warnings.append(f"子图 2 已由 {m2} 转换为 {target}（原本没有 alpha 通道）")
        im2 = im2.convert(target)
    return im1, im2, warnings


def validate_join(
    size1: Size,
    size2: Size,
    axis: Axis,
    meta1: Optional["SplitMeta"] = None,
    meta2: Optional["SplitMeta"] = None,
    name1: str = "子图 1",
    name2: str = "子图 2",
    path1: str = "",
    path2: str = "",
) -> JoinCheck:
    """拼接前的严格校验。

    尺寸不符 = 错误（阻止拼接）；元数据对不上 = 错误；alpha 不一致 = 提示。
    """
    errors: list[str] = []
    warnings: list[str] = []
    w1, h1 = size1
    w2, h2 = size2

    # 元数据交叉校验放在最前面：专门挡"拿错文件"，而且它往往才是真正的根因，
    # 后面那条尺寸不符只是它的表象。
    if meta1 is not None and meta2 is not None:
        if list(meta1.original_size) != list(meta2.original_size):
            o1 = "×".join(str(v) for v in meta1.original_size)
            o2 = "×".join(str(v) for v in meta2.original_size)
            errors.append(
                f"这两个文件不是同一次拆分的结果：\n"
                f"  {name1} 记录的原图是 {o1}\n"
                f"  {name2} 记录的原图是 {o2}"
            )
        elif meta1.axis != meta2.axis or meta1.position != meta2.position:
            errors.append(
                f"这两个文件记录的拆分参数不一致：\n"
                f"  {name1}：{AXIS_LABEL.get(meta1.axis, meta1.axis)}，位置 {meta1.position} px\n"
                f"  {name2}：{AXIS_LABEL.get(meta2.axis, meta2.axis)}，位置 {meta2.position} px"
            )
        else:
            # 顺序检查：几何尺寸完全合法但内容错位，只有 part 字段能发现。
            p1 = identify_part(meta1, path1, size1) if path1 else 0
            p2 = identify_part(meta2, path2, size2) if path2 else 0
            if p1 == 2 and p2 == 1:
                warnings.append(
                    "这两张图看起来左右顺序反了（选中的第一张其实是第 2 块）。"
                    "如果预览里的接缝对不上，点一下「⇄ 交换」。"
                )

    # 逐块跟拆分记录比对尺寸。有记录时这是最准的判据——因为只有它能发现
    # "沿着拼接方向被裁短了"这种情况：宽度明明都对得上，拼出来却悄悄变矮了。
    recorded_mismatch = False
    for idx, (meta, size, path, label) in enumerate(
        ((meta1, size1, path1, name1), (meta2, size2, path2, name2)), start=1
    ):
        if meta is None:
            continue
        part = identify_part(meta, path, size) if path else 0
        if not part:
            part = idx          # 认不出来就按用户选的顺序假定
        pm = meta.part(part)
        if pm is None or len(pm.size) != 2:
            continue
        expected = (int(pm.size[0]), int(pm.size[1]))
        actual = (int(size[0]), int(size[1]))
        if expected != actual:
            recorded_mismatch = True
            errors.append(
                f"{label} 的尺寸和拆分时对不上：\n"
                f"  期望（拆分记录）：{expected[0]}×{expected[1]}\n"
                f"  实际：{actual[0]}×{actual[1]}"
                f"（宽 {actual[0] - expected[0]:+d}，高 {actual[1] - expected[1]:+d}）"
            )

    # 几何校验：沿轴方向的尺寸必须一致，否则拼出来会错位或缺一块。
    if axis == "vertical":
        if h1 != h2 and not recorded_mismatch:
            errors.append(
                f"{name2} 的高度 {h2} ≠ {name1} 的高度 {h1}（差 {h2 - h1:+d} px），无法左右拼接。"
            )
        expect = f"{w1 + w2}×{h1}" if h1 == h2 else "—"
    else:
        if w1 != w2 and not recorded_mismatch:
            errors.append(
                f"{name2} 的宽度 {w2} ≠ {name1} 的宽度 {w1}（差 {w2 - w1:+d} px），无法上下拼接。"
            )
        expect = f"{w1}×{h1 + h2}" if w1 == w2 else "—"

    if meta1 is not None and axis != meta1.axis:
        warnings.append(
            f"{name1} 记录的拆分方向是 {AXIS_LABEL.get(meta1.axis, meta1.axis)}，"
            f"与当前选择的 {AXIS_LABEL.get(axis, axis)} 不一致，请确认。"
        )

    ok = not errors
    if ok:
        reason = f"{AXIS_LABEL.get(axis, axis)}拼接，输出 {expect}"
    else:
        reason = "校验未通过"
    return JoinCheck(ok=ok, axis=axis, reason=reason, errors=errors, warnings=warnings)


def join_images(im1: Image.Image, im2: Image.Image, axis: Axis) -> Image.Image:
    """把两块拼起来。尺寸不符直接抛 JoinError，不做任何自动对齐。"""
    w1, h1 = im1.size
    w2, h2 = im2.size
    if axis == "vertical":
        if h1 != h2:
            raise JoinError([f"高度不一致（{h1} vs {h2}），无法左右拼接。"])
        out = Image.new(im1.mode, (w1 + w2, h1))
        out.paste(im1, (0, 0))
        out.paste(im2, (w1, 0))
    else:
        if w1 != w2:
            raise JoinError([f"宽度不一致（{w1} vs {w2}），无法上下拼接。"])
        out = Image.new(im1.mode, (w1, h1 + h2))
        out.paste(im1, (0, 0))
        out.paste(im2, (0, h1))
    return out


# --------------------------------------------------------------------------
# 第 2 层：还原信息（元数据）的写入与四级回退读取
# --------------------------------------------------------------------------

@dataclass
class PartMeta:
    part: int                 # 1 或 2
    filename: str
    box: list[int]
    size: list[int]


@dataclass
class SplitMeta:
    version: int
    created: str
    original_name: str
    original_size: list[int]
    axis: str
    position: int             # 前半块沿轴的宽度
    source_mode: str
    output_mode: str
    exif_transposed: bool
    parts: list[PartMeta]

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "created": self.created,
            "original_name": self.original_name,
            "original_size": self.original_size,
            "axis": self.axis,
            "position": self.position,
            "source_mode": self.source_mode,
            "output_mode": self.output_mode,
            "exif_transposed": self.exif_transposed,
            "parts": [
                {"part": p.part, "filename": p.filename, "box": p.box, "size": p.size}
                for p in self.parts
            ],
        }
        # ensure_ascii=True：中文路径会被转义成 \uXXXX，顺便绕开 PNG tEXt 的
        # latin-1 限制，读取端 json.loads 会自动还原。
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data) -> Optional["SplitMeta"]:
        """宽容解析：字段缺失或非法一律返回 None，绝不抛异常。"""
        if not isinstance(data, dict):
            return None
        try:
            axis = str(data["axis"])
            if axis not in ("vertical", "horizontal"):
                return None
            raw_size = data["original_size"]
            if not isinstance(raw_size, (list, tuple)) or len(raw_size) != 2:
                return None
            w, h = int(raw_size[0]), int(raw_size[1])
            if w < 1 or h < 1:
                return None
            position = int(data["position"])
            n = w if axis == "vertical" else h
            if not (1 <= position <= n - 1):
                return None

            parts: list[PartMeta] = []
            for raw in data.get("parts") or []:
                if not isinstance(raw, dict):
                    continue
                box = raw.get("box")
                if not isinstance(box, (list, tuple)) or len(box) != 4:
                    continue
                parts.append(
                    PartMeta(
                        part=int(raw.get("part", 0)),
                        filename=str(raw.get("filename", "")),
                        box=[int(v) for v in box],
                        size=[int(v) for v in (raw.get("size") or [0, 0])],
                    )
                )

            return cls(
                version=int(data.get("version", 1)),
                created=str(data.get("created", "")),
                original_name=str(data.get("original_name", "")),
                original_size=[w, h],
                axis=axis,
                position=position,
                source_mode=str(data.get("source_mode", "")),
                output_mode=str(data.get("output_mode", "")),
                exif_transposed=bool(data.get("exif_transposed", False)),
                parts=parts,
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    @classmethod
    def from_json(cls, raw) -> Optional["SplitMeta"]:
        try:
            return cls.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            return None

    def part(self, index: int) -> Optional[PartMeta]:
        for p in self.parts:
            if p.part == index:
                return p
        return None


def _part_by_size(meta: SplitMeta, size: Size) -> int:
    """按记录里的尺寸反推是哪一块。

    只在尺寸接近时才算数——差得离谱说明这张图已经被大改过，反推不可信。
    """
    target = (int(size[0]), int(size[1]))
    exact = [p.part for p in meta.parts if len(p.size) == 2 and tuple(p.size) == target]
    if len(exact) == 1:
        return exact[0]

    best, best_diff = 0, None
    for p in meta.parts:
        if len(p.size) != 2:
            continue
        diff = abs(p.size[0] - target[0]) + abs(p.size[1] - target[1])
        if best_diff is None or diff < best_diff:
            best, best_diff = p.part, diff
    # 容差取 4px 或边长的 1%，取大者：既要容忍裁掉几像素，又不能胡乱认亲。
    if best_diff is not None and best_diff <= max(4, max(target) // 100):
        return best
    return 0


def identify_part(meta: SplitMeta, path, size: Optional[Size] = None) -> int:
    """判断这个文件是第 1 块还是第 2 块。0 表示认不出来。

    依次尝试：记录里写的文件名 → _p1/_p2 后缀 → 按记录尺寸反推。
    """
    name = Path(path).name
    for p in meta.parts:
        if p.filename and Path(p.filename).name == name:
            return p.part
    m = PART_RE.search(Path(path).stem)
    if m:
        return int(m.group(1))
    if size is not None:
        return _part_by_size(meta, size)
    return 0


def build_meta(
    loaded: LoadedImage, axis: Axis, position: int, part_names: list[str]
) -> SplitMeta:
    box1, box2 = compute_boxes(axis, position, loaded.size)
    return SplitMeta(
        version=META_VERSION,
        created=datetime.now().isoformat(timespec="seconds"),
        original_name=loaded.path.name,
        original_size=list(loaded.size),
        axis=axis,
        position=position,
        source_mode=loaded.source_mode,
        output_mode=loaded.image.mode,
        exif_transposed=loaded.exif_transposed,
        parts=[
            PartMeta(1, part_names[0], list(box1), [box1[2] - box1[0], box1[3] - box1[1]]),
            PartMeta(2, part_names[1], list(box2), [box2[2] - box2[0], box2[3] - box2[1]]),
        ],
    )


def save_png_with_meta(
    image: Image.Image,
    path,
    *,
    meta: Optional[SplitMeta] = None,
    icc: Optional[bytes] = None,
    dpi: Optional[tuple] = None,
    compress_level: int = 6,
) -> None:
    """存成 PNG，并把还原信息塞进 PNG 内部的 iTXt 文本块。"""
    pnginfo = None
    if meta is not None:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_itxt(META_KEY, meta.to_json())
    kwargs = {"compress_level": compress_level}
    if icc:
        kwargs["icc_profile"] = icc
    if dpi:
        kwargs["dpi"] = dpi
    image.save(path, format="PNG", pnginfo=pnginfo, **kwargs)


def read_png_meta(path) -> Optional[SplitMeta]:
    """从 PNG 内部的 iTXt 文本块读还原信息。"""
    try:
        with Image.open(path) as im:
            raw = im.info.get(META_KEY)
            if not raw:
                text = getattr(im, "text", None)
                if isinstance(text, dict):
                    raw = text.get(META_KEY)
            if not raw:
                return None
            return SplitMeta.from_json(raw)
    except Exception:
        return None


def sidecar_path_for(image_path) -> Path:
    image_path = Path(image_path)
    stem = PART_RE.sub("", image_path.stem)
    return image_path.with_name(stem + "_split.json")


def save_sidecar(meta: SplitMeta, path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(json.loads(meta.to_json()), fh, ensure_ascii=False, indent=2)


def _find_sidecar_by_scan(image_path: Path) -> Optional[Path]:
    """按名字找不到 sidecar 时，在同目录里找一个"属于这张图"的拆分记录。

    只认两种证据：记录里写明了这个文件名（改名了也认得出），或者记录的
    基名是当前文件名的前缀（覆盖 photo_p1_edited.png 这种改了名但没改来源的）。
    """
    try:
        candidates = sorted(image_path.parent.glob("*_split.json"))
    except OSError:
        return None

    best: Optional[Path] = None
    best_rank = (0, 0)
    for cand in candidates:
        base = PART_RE.sub("", cand.stem)
        if base.endswith("_split"):
            base = base[: -len("_split")]
        if not base:
            continue
        named = False
        try:
            with open(cand, encoding="utf-8") as fh:
                data = json.load(fh)
            for raw in (data.get("parts") or []):
                if isinstance(raw, dict) and \
                        Path(str(raw.get("filename", ""))).name == image_path.name:
                    named = True
                    break
        except (OSError, ValueError):
            continue
        if named:
            rank = (2, len(base))
        elif image_path.stem.startswith(base):
            rank = (1, len(base))
        else:
            continue
        if rank > best_rank:
            best, best_rank = cand, rank
    return best


def load_sidecar_for(image_path) -> Optional[SplitMeta]:
    image_path = Path(image_path)
    path = sidecar_path_for(image_path)
    if not path.exists():
        path = _find_sidecar_by_scan(image_path)
        if path is None:
            return None
    try:
        with open(path, encoding="utf-8") as fh:
            return SplitMeta.from_dict(json.load(fh))
    except (OSError, ValueError):
        return None


@dataclass
class MetaLookup:
    meta: Optional[SplitMeta]
    source: str = ""


def lookup_meta(path) -> MetaLookup:
    """四级回退：PNG 内嵌 → sidecar JSON → 文件名 → 纯几何（后两级在调用方）。"""
    meta = read_png_meta(path)
    if meta is not None:
        return MetaLookup(meta, "PNG 内嵌信息")
    meta = load_sidecar_for(path)
    if meta is not None:
        return MetaLookup(meta, "同名 sidecar JSON")
    return MetaLookup(None, "")


def guess_sibling(path) -> Optional[Path]:
    """已知一块，猜另一块在哪：优先用元数据里记的文件名，其次按 _p1/_p2 换号。"""
    path = Path(path)
    meta = lookup_meta(path).meta
    if meta is not None:
        mine = identify_part(meta, path)
        other = 2 if mine == 1 else 1
        target = meta.part(other)
        if target and target.filename:
            candidate = path.with_name(Path(target.filename).name)
            if candidate.exists() and candidate != path:
                return candidate
    m = PART_RE.search(path.stem)
    if m:
        num = int(m.group(1))
        other = 2 if num == 1 else 1
        candidate = path.with_name(PART_RE.sub(f"_p{other}", path.stem) + path.suffix)
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------
# 第 3 层：坐标变换
# --------------------------------------------------------------------------

class ViewTransform:
    """图片坐标 <-> 画布坐标的唯一换算入口。

    缩放后图片在画布里是居中留边的，所以只有 scale 是不够的，必须带上
    偏移量 ox/oy。业务代码里不要出现裸的 `* scale`。
    """

    __slots__ = ("scale", "ox", "oy")

    def __init__(self, scale: float = 1.0, ox: float = 0.0, oy: float = 0.0):
        self.scale = scale
        self.ox = ox
        self.oy = oy

    @classmethod
    def fit(cls, canvas_w: int, canvas_h: int, img_w: int, img_h: int, margin: int = 12) -> "ViewTransform":
        avail_w = max(canvas_w - 2 * margin, 1)
        avail_h = max(canvas_h - 2 * margin, 1)
        scale = min(avail_w / img_w, avail_h / img_h, 1.0)
        ox = (canvas_w - img_w * scale) / 2.0
        oy = (canvas_h - img_h * scale) / 2.0
        return cls(scale, ox, oy)

    def img_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return self.ox + x * self.scale, self.oy + y * self.scale

    def canvas_to_img(self, cx: float, cy: float) -> tuple[float, float]:
        """不 clamp，由调用方决定怎么处理越界。"""
        return (cx - self.ox) / self.scale, (cy - self.oy) / self.scale


# --------------------------------------------------------------------------
# 第 4 层：可复用的图像画布
# --------------------------------------------------------------------------

class ImageCanvas(ttk.Frame):
    """带分割线的图像预览控件。

    拆分页用它交互（拖线 / 点击吸附 / 放大镜），拼接页把 interactive 关掉
    只当预览用。
    """

    MARGIN = 12
    HIT_TOLERANCE = 6          # 判定"点中了分割线"的屏幕像素容差
    LOUPE_SRC = 48             # 放大镜取样区域边长（原图像素）
    LOUPE_ZOOM = 4             # 放大镜倍率
    LOUPE_GAP = 16             # 放大镜离画布边缘的距离

    def __init__(self, master, on_position_change: Optional[Callable[[int], None]] = None,
                 on_render: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg="#2b2b2b", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.on_position_change = on_position_change
        self.on_render = on_render

        self._image: Optional[Image.Image] = None     # 全分辨率原图（只读！）
        self._axis: Axis = "vertical"
        self._position: int = 1
        self._interactive = True

        self.view = ViewTransform()
        self._preview: Optional[Image.Image] = None
        self._photo: Optional[ImageTk.PhotoImage] = None   # 必须持有引用，否则被 GC
        self._image_item: Optional[int] = None
        self._line_id: Optional[int] = None
        self._border_id: Optional[int] = None

        self._loupe_photo: Optional[ImageTk.PhotoImage] = None
        self._loupe_ids: list[int] = []

        self._render_job = None
        self._last_size: tuple[int, int] = (0, 0)
        self._dragging = False
        self._preview_scale = 1.0

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Leave>", lambda _e: self._hide_loupe())
        self.canvas.bind("<Left>", lambda e: self._nudge(-1, e))
        self.canvas.bind("<Right>", lambda e: self._nudge(1, e))
        self.canvas.bind("<Up>", lambda e: self._nudge(-1, e))
        self.canvas.bind("<Down>", lambda e: self._nudge(1, e))

    # ------------------------------ 对外接口 ------------------------------

    def set_image(self, image: Optional[Image.Image], *, axis: Axis = "vertical",
                  interactive: bool = True, position: Optional[int] = None) -> None:
        self._image = image
        self._axis = axis
        self._interactive = interactive
        self._hide_loupe()
        self.view = ViewTransform()
        self._preview = None
        n = self.n()
        self._position = clamp_int(position if position is not None else n // 2, 1, max(1, n - 1))
        self._request_render(immediate=True)

    def set_axis(self, axis: Axis) -> None:
        if axis == self._axis:
            return
        self._axis = axis
        n = self.n()
        self._position = clamp_int(self._position, 1, max(1, n - 1))
        self._redraw_line()
        self._request_render()

    def position(self) -> int:
        return self._position

    def axis(self) -> Axis:
        return self._axis

    def n(self) -> int:
        """沿切割轴的长度，position 的合法范围是 1..n-1。"""
        if self._image is None:
            return 0
        return axis_length(self._axis, self._image.size)

    def set_position(self, value, *, notify: bool = False) -> int:
        n = self.n()
        if n < 2:
            return self._position
        new = clamp_int(value, 1, n - 1)
        if new != self._position:
            self._position = new
            self._redraw_line()
            if notify and self.on_position_change:
                self.on_position_change(new)
        return self._position

    def preview_scale(self) -> float:
        return self._preview_scale

    def request_fit(self) -> None:
        self._request_render(immediate=True)

    # ------------------------------ 渲染 ------------------------------

    def _on_configure(self, event) -> None:
        if event.widget is not self.canvas:
            return                                  # <Configure> 会从子控件冒泡上来
        size = (event.width, event.height)
        if size == self._last_size:
            return
        if event.width <= 1 or event.height <= 1:
            return                                  # 首次布局 / 隐藏标签页，尺寸还是 1
        self._last_size = size
        self._request_render()

    def _request_render(self, immediate: bool = False) -> None:
        if self._render_job is not None:
            try:
                self.after_cancel(self._render_job)
            except Exception:
                pass
            self._render_job = None
        delay = 0 if immediate else 120
        self._render_job = self.after(delay, self._do_render)

    def _clear_canvas(self) -> None:
        self.canvas.delete("all")
        self._image_item = None
        self._border_id = None
        self._line_id = None
        self._loupe_ids = []
        self._photo = None
        self._loupe_photo = None
        self._preview = None
        self._preview_scale = 1.0

    def _do_render(self) -> None:
        self._render_job = None
        if self._image is None:
            self._clear_canvas()
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return

        img_w, img_h = self._image.size
        self.view = ViewTransform.fit(cw, ch, img_w, img_h, self.MARGIN)

        # 预览只在尺寸变化时重建，拖动分割线不会走到这里。
        pw = max(1, round_half_up(img_w * self.view.scale))
        ph = max(1, round_half_up(img_h * self.view.scale))
        if self._preview is None or self._preview.size != (pw, ph):
            self._preview = self._image.resize(
                (pw, ph), Image.Resampling.LANCZOS, reducing_gap=2.0
            )
        self._preview_scale = self.view.scale

        # 先建新对象再覆盖旧引用，避免中间出现一帧空白。
        photo = ImageTk.PhotoImage(self._preview)
        self._photo = photo

        x0, y0 = self.view.img_to_canvas(0, 0)
        x1, y1 = self.view.img_to_canvas(img_w, img_h)

        if self._image_item is None:
            self._image_item = self.canvas.create_image(x0, y0, anchor="nw", image=photo)
            self._border_id = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline="#666", width=1
            )
        else:
            self.canvas.coords(self._image_item, x0, y0)
            self.canvas.itemconfigure(self._image_item, image=photo)
            self.canvas.coords(self._border_id, x0, y0, x1, y1)
            self.canvas.tag_raise(self._border_id)

        self._redraw_line()
        if self.on_render:
            self.on_render()

    def _redraw_line(self) -> None:
        if self._image is None or self._image_item is None:
            return
        img_w, img_h = self._image.size
        if self._axis == "vertical":
            cx, _ = self.view.img_to_canvas(self._position, 0)
            p1 = (cx, self.view.oy)
            p2 = (cx, self.view.oy + img_h * self.view.scale)
        else:
            _, cy = self.view.img_to_canvas(0, self._position)
            p1 = (self.view.ox, cy)
            p2 = (self.view.ox + img_w * self.view.scale, cy)

        if not self._interactive:
            if self._line_id is not None:
                self.canvas.itemconfigure(self._line_id, state="hidden")
            return

        if self._line_id is None:
            self._line_id = self.canvas.create_line(
                p1[0], p1[1], p2[0], p2[1], fill="#ff3b30", width=2
            )
        else:
            self.canvas.itemconfigure(self._line_id, state="normal")
            self.canvas.coords(self._line_id, p1[0], p1[1], p2[0], p2[1])
        self.canvas.tag_raise(self._line_id)

    # ------------------------------ 鼠标交互 ------------------------------

    def _line_canvas_x(self) -> float:
        return self.view.img_to_canvas(self._position, 0)[0]

    def _line_canvas_y(self) -> float:
        return self.view.img_to_canvas(0, self._position)[1]

    def _hit_line(self, cx: float, cy: float) -> bool:
        if self._axis == "vertical":
            return abs(cx - self._line_canvas_x()) <= self.HIT_TOLERANCE
        return abs(cy - self._line_canvas_y()) <= self.HIT_TOLERANCE

    def _inside_image(self, cx: float, cy: float) -> bool:
        if self._image is None:
            return False
        img_w, img_h = self._image.size
        x0, y0 = self.view.img_to_canvas(0, 0)
        x1, y1 = self.view.img_to_canvas(img_w, img_h)
        return x0 <= cx <= x1 and y0 <= cy <= y1

    def _position_from_canvas(self, cx: float, cy: float) -> int:
        ix, iy = self.view.canvas_to_img(cx, cy)
        return round_half_up(ix if self._axis == "vertical" else iy)

    def _on_motion(self, event) -> None:
        if self._image is None or not self._interactive:
            return
        near = self._hit_line(event.x, event.y)
        cursor = ""
        if near:
            cursor = "sb_h_double_arrow" if self._axis == "vertical" else "sb_v_double_arrow"
        elif self._inside_image(event.x, event.y):
            cursor = "crosshair"
        self.canvas.configure(cursor=cursor)
        self._update_loupe(event.x, event.y)

    def _on_press(self, event) -> None:
        if self._image is None or not self._interactive:
            return
        self.canvas.focus_set()                     # 方向键绑在 canvas 上，需要焦点
        if self._inside_image(event.x, event.y) or self._hit_line(event.x, event.y):
            self._dragging = True
            self._apply_drag(event.x, event.y)

    def _on_drag(self, event) -> None:
        if self._dragging:
            self._apply_drag(event.x, event.y)

    def _on_release(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._update_loupe(event.x, event.y)
        if self.on_position_change:
            self.on_position_change(self._position)

    def _apply_drag(self, cx: float, cy: float) -> None:
        self.set_position(self._position_from_canvas(cx, cy))
        self._update_loupe(cx, cy)
        if self.on_position_change:
            self.on_position_change(self._position)

    def _nudge(self, delta: int, event) -> str:
        if self._image is None or not self._interactive:
            return "break"
        step = 10 if (event.state & 0x0001) else 1     # Shift = 10px
        if self._axis == "vertical":
            delta = delta if event.keysym in ("Left", "Right") else 0
        else:
            delta = delta if event.keysym in ("Up", "Down") else 0
        if delta:
            self.set_position(self._position + delta * step, notify=True)
            self.canvas.focus_set()
        return "break"

    # ------------------------------ 放大镜 ------------------------------

    def _hide_loupe(self) -> None:
        for item in self._loupe_ids:
            self.canvas.itemconfigure(item, state="hidden")

    def _update_loupe(self, cx: float, cy: float) -> None:
        """在大图缩小显示时，1px 的调整在画布上根本看不见，靠放大镜才能对准。"""
        if not self._interactive or self._image is None or not self._inside_image(cx, cy):
            self._hide_loupe()
            return

        iw, ih = self._image.size
        ix, iy = self.view.canvas_to_img(cx, cy)
        if self._axis == "vertical":
            px, py = self._position, iy
        else:
            px, py = ix, self._position

        half = self.LOUPE_SRC // 2
        left = int(px) - half
        top = int(py) - half
        # 越界部分 PIL 会自动补黑/透明，正好当边界提示。
        patch = self._image.crop((left, top, left + self.LOUPE_SRC, top + self.LOUPE_SRC))
        size = self.LOUPE_SRC * self.LOUPE_ZOOM
        # 必须 NEAREST：LANCZOS 会把像素糊成渐变，放大镜就失去意义了。
        patch = patch.resize((size, size), Image.Resampling.NEAREST)

        photo = ImageTk.PhotoImage(patch)
        self._loupe_photo = photo

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        line_x = self._line_canvas_x()
        if line_x < cw / 2:
            lx = cw - size - self.LOUPE_GAP
        else:
            lx = self.LOUPE_GAP
        ly = ch - size - self.LOUPE_GAP

        cx0, cy0 = lx + size / 2, ly + size / 2
        text = f"{'x' if self._axis == 'vertical' else 'y'} = {self._position} px"

        if not self._loupe_ids:
            self._loupe_ids = [
                self.canvas.create_image(lx, ly, anchor="nw", image=photo),
                self.canvas.create_rectangle(lx, ly, lx + size, ly + size,
                                             outline="#ff3b30", width=2),
                # 十字准星：竖线永远压在切割点那一列上
                self.canvas.create_line(cx0, ly, cx0, ly + size, fill="#ff3b30", width=1),
                self.canvas.create_line(lx, cy0, lx + size, cy0, fill="#ff3b30", width=1),
                self.canvas.create_text(lx + size / 2, ly - 10, text=text,
                                        fill="#ff3b30", font=("Consolas", 10, "bold")),
            ]
        else:
            img_item, rect_item, vline, hline, label = self._loupe_ids
            self.canvas.itemconfigure(img_item, image=photo, state="normal")
            self.canvas.coords(img_item, lx, ly)
            self.canvas.coords(rect_item, lx, ly, lx + size, ly + size)
            self.canvas.itemconfigure(rect_item, state="normal")
            self.canvas.coords(vline, cx0, ly, cx0, ly + size)
            self.canvas.itemconfigure(vline, state="normal")
            self.canvas.coords(hline, lx, cy0, lx + size, cy0)
            self.canvas.itemconfigure(hline, state="normal")
            self.canvas.coords(label, lx + size / 2, ly - 10)
            self.canvas.itemconfigure(label, text=text, state="normal")

        for item in self._loupe_ids:
            self.canvas.tag_raise(item)


# --------------------------------------------------------------------------
# 第 5 层：可复制的报告窗口
# --------------------------------------------------------------------------

class ReportDialog(tk.Toplevel):
    """messagebox 里的文字没法选中复制，而用户经常需要把报错整段贴给 AI 问原因。"""

    def __init__(self, master, title: str, blocks: list[tuple[str, list[str]]]):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.resizable(True, True)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text=title, font=("Microsoft YaHei UI", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )

        self.text = tk.Text(self, width=86, height=14, wrap="word", relief="solid",
                            borderwidth=1, padx=10, pady=8)
        self.text.grid(row=1, column=0, sticky="nsew", padx=12)
        self.text.tag_configure("error", foreground="#c0392b")
        self.text.tag_configure("warn", foreground="#b9770e")
        self.text.tag_configure("ok", foreground="#1e8449")
        self.text.tag_configure("plain", foreground="#333333")
        self.text.tag_configure("head", foreground="#333333",
                                font=("Microsoft YaHei UI", 9, "bold"))

        self.plain = ""
        for heading, lines in blocks:
            if heading:
                self.text.insert("end", heading + "\n", "head")
            for line in lines:
                tag = "plain"
                if line.startswith("[错误] "):
                    tag, line = "error", line[len("[错误] "):]
                elif line.startswith("[提示] "):
                    tag, line = "warn", line[len("[提示] "):]
                elif line.startswith("[OK] "):
                    tag, line = "ok", line[len("[OK] "):]
                self.text.insert("end", line + "\n", tag)
                self.plain += line + "\n"
            self.plain += "\n"

        self.text.configure(state="disabled")

        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=12)
        ttk.Button(bar, text="复制全部", command=self._copy).pack(side="left")
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        self._center_on(master)

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.plain.strip())
        self.text.configure(state="normal")
        self.text.insert("end", "\n（已复制到剪贴板）\n", "ok")
        self.text.see("end")
        self.text.configure(state="disabled")

    def _center_on(self, master) -> None:
        try:
            master.update_idletasks()
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass


def show_report(master, title: str, blocks: list[tuple[str, list[str]]]) -> None:
    ReportDialog(master, title, blocks)


# --------------------------------------------------------------------------
# 第 6 层：拆分页
# --------------------------------------------------------------------------

class SplitTab(ttk.Frame):
    def __init__(self, master, app: "App"):
        super().__init__(master, padding=10)
        self.app = app
        self.loaded: Optional[LoadedImage] = None
        self._syncing = False
        self._out_dir: Optional[Path] = None
        self._build_ui()

    # ------------------------------ 界面 ------------------------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Button(top, text="打开图片…", command=self._open).pack(side="left")
        self.file_label = ttk.Label(top, text="未加载图片", foreground="#666666")
        self.file_label.pack(side="left", padx=10)
        self.scale_label = ttk.Label(top, text="", foreground="#888888")
        self.scale_label.pack(side="right")

        self.canvas = ImageCanvas(self, on_position_change=self._on_canvas_position,
                                  on_render=self._on_canvas_render)
        self.canvas.pack(fill="both", expand=True, pady=8)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x")

        row1 = ttk.Frame(ctrl)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="切割方向：").pack(side="left")
        self.axis_var = tk.StringVar(value="vertical")
        ttk.Radiobutton(row1, text="垂直（左 | 右）", value="vertical",
                        variable=self.axis_var, command=self._on_axis).pack(side="left")
        ttk.Radiobutton(row1, text="水平（上 | 下）", value="horizontal",
                        variable=self.axis_var, command=self._on_axis).pack(side="left", padx=(8, 0))
        ttk.Label(row1, text="也可以直接在预览图上拖动红色分割线",
                  foreground="#888888").pack(side="left", padx=14)

        row2 = ttk.Frame(ctrl)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="切割位置：").pack(side="left")
        self.px_var = tk.StringVar()
        self.px_entry = ttk.Entry(row2, textvariable=self.px_var, width=8, justify="right")
        self.px_entry.pack(side="left")
        ttk.Label(row2, text="px").pack(side="left", padx=(3, 12))
        self.pct_var = tk.StringVar()
        self.pct_entry = ttk.Entry(row2, textvariable=self.pct_var, width=7, justify="right")
        self.pct_entry.pack(side="left")
        ttk.Label(row2, text="%").pack(side="left", padx=(3, 14))
        self.size_label = ttk.Label(row2, text="", foreground="#1e8449")
        self.size_label.pack(side="left")

        self.px_entry.bind("<Return>", self._commit_px)
        self.px_entry.bind("<FocusOut>", self._commit_px)
        self.px_entry.bind("<KeyRelease>", self._on_px_key)
        self.pct_entry.bind("<Return>", self._commit_pct)
        self.pct_entry.bind("<FocusOut>", self._commit_pct)
        self.pct_entry.bind("<KeyRelease>", self._on_pct_key)

        row3 = ttk.Frame(ctrl)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="输出目录：").pack(side="left")
        self.outdir_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.outdir_var).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        ttk.Button(row3, text="浏览…", width=8, command=self._pick_outdir).pack(side="left")
        ttk.Label(row3, text="留空 = 原图所在目录　输出格式：PNG（无损，可内嵌还原信息）",
                  foreground="#888888").pack(side="left", padx=10)

        row4 = ttk.Frame(ctrl)
        row4.pack(fill="x", pady=(8, 0))
        self.split_btn = ttk.Button(row4, text="执行拆分", command=self._do_split, state="disabled")
        self.split_btn.pack(side="left")
        self.open_dir_btn = ttk.Button(row4, text="打开输出目录",
                                       command=self._open_outdir, state="disabled")
        self.open_dir_btn.pack(side="left", padx=8)
        self.status = ttk.Label(row4, text="先打开一张图片", foreground="#666666")
        self.status.pack(side="left", padx=12)

    # ------------------------------ 事件 ------------------------------

    def _open(self) -> None:
        path = filedialog.askopenfilename(
            title="选择要拆分的图片",
            initialdir=str(self.app.last_dir),
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif"),
                       ("所有文件", "*.*")],
        )
        if not path:
            return
        self.load_path(Path(path))

    def load_path(self, path: Path) -> None:
        try:
            loaded = open_image(path)
        except ImageLoadError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)
            return
        self.app.last_dir = path.parent
        self.loaded = loaded
        self._out_dir = None
        self.outdir_var.set("")

        notes = [f"{loaded.size[0]}×{loaded.size[1]}", loaded.source_mode]
        if loaded.was_converted:
            notes.append(f"已转为 {loaded.image.mode}（8bit）")
        if loaded.exif_transposed:
            notes.append("已按 EXIF 转正")
        if loaded.frame_count > 1:
            notes.append(f"仅取第 1 帧（共 {loaded.frame_count} 帧）")
        self.file_label.configure(text=f"{path.name}　·　" + "　·　".join(notes),
                                  foreground="#333333")

        axis: Axis = self.axis_var.get()            # type: ignore[assignment]
        n = axis_length(axis, loaded.size)
        if n < 2:
            self.canvas.set_image(loaded.image, axis=axis, interactive=False)
            self.split_btn.configure(state="disabled")
            self.status.configure(text=f"这张图沿该方向只有 {n} px，切不开", foreground="#c0392b")
            return

        self.canvas.set_image(loaded.image, axis=axis, interactive=True, position=n // 2)
        self.split_btn.configure(state="normal")
        self.status.configure(text="拖动红线，或直接输入数字", foreground="#666666")
        self._sync_display(update_px=True, update_pct=True)
        self.px_entry.focus_set()

    def _on_axis(self) -> None:
        axis: Axis = self.axis_var.get()            # type: ignore[assignment]
        if self.loaded is None:
            return
        n = axis_length(axis, self.loaded.size)
        if n < 2:
            self.canvas.set_image(self.loaded.image, axis=axis, interactive=False)
            self.split_btn.configure(state="disabled")
            self.status.configure(text=f"这张图沿该方向只有 {n} px，切不开", foreground="#c0392b")
            return
        # 切换方向时保留百分比：59% 在哪个轴上都是 59%。
        old_axis = self.canvas.axis()
        old_n = axis_length(old_axis, self.loaded.size) or 1
        pct = self.canvas.position() / old_n
        self.canvas.set_axis(axis)
        self.canvas.set_image(self.loaded.image, axis=axis, interactive=True,
                              position=clamp_int(round_half_up(pct * n), 1, n - 1))
        self.split_btn.configure(state="normal")
        self._sync_display(update_px=True, update_pct=True)

    def _on_canvas_position(self, _px: int) -> None:
        self._sync_display(update_px=True, update_pct=True)

    def _on_canvas_render(self) -> None:
        """缩放比例只有渲染完才知道，所以标签要等渲染回调来刷新。"""
        if self.loaded is None:
            return
        self.scale_label.configure(
            text=f"预览缩放 {self.canvas.preview_scale() * 100:.0f}%"
        )

    def _sync_display(self, *, update_px: bool, update_pct: bool) -> None:
        if self.loaded is None:
            return
        n = self.canvas.n()
        if n < 2:
            return
        pos = self.canvas.position()
        self._syncing = True
        try:
            if update_px:
                self.px_var.set(str(pos))
            if update_pct:
                self.pct_var.set(f"{pos / n * 100:.1f}")
            axis = self.canvas.axis()
            s1, s2 = part_sizes(axis, pos, self.loaded.size)
            if axis == "vertical":
                text = f"左 {s1[0]}×{s1[1]}　|　右 {s2[0]}×{s2[1]}"
            else:
                text = f"上 {s1[0]}×{s1[1]}　|　下 {s2[0]}×{s2[1]}"
            self.size_label.configure(text=text)
        finally:
            self._syncing = False

    def _on_px_key(self, _event=None) -> None:
        if self._syncing:
            return
        value = parse_number(self.px_var.get())
        if value is None:
            return                                   # 输入中间态（"12."、"-"）忽略
        self.canvas.set_position(round_half_up(value))
        self._sync_display(update_px=False, update_pct=True)

    def _commit_px(self, _event=None) -> None:
        if self._syncing:
            return
        self._on_px_key()
        self._sync_display(update_px=True, update_pct=True)

    def _on_pct_key(self, _event=None) -> None:
        if self._syncing or self.loaded is None:
            return
        n = self.canvas.n()
        if n < 2:
            return
        pct = parse_number(self.pct_var.get())
        if pct is None:
            return
        self.canvas.set_position(round_half_up(pct / 100.0 * n))
        self._sync_display(update_px=True, update_pct=False)

    def _commit_pct(self, _event=None) -> None:
        if self._syncing:
            return
        self._on_pct_key()
        self._sync_display(update_px=True, update_pct=True)

    # ------------------------------ 输出 ------------------------------

    def _pick_outdir(self) -> None:
        initial = str(self._out_dir or (self.loaded.path.parent if self.loaded else self.app.last_dir))
        chosen = filedialog.askdirectory(title="选择输出目录", initialdir=initial)
        if chosen:
            self._out_dir = Path(chosen)
            self.outdir_var.set(str(self._out_dir))

    def _effective_out_dir(self) -> Optional[Path]:
        raw = self.outdir_var.get().strip()
        if raw:
            return Path(raw)
        if self.loaded is not None:
            return self.loaded.path.parent
        return None

    def _open_outdir(self) -> None:
        target = self._effective_out_dir()
        if target and target.exists():
            open_in_explorer(target)

    def _do_split(self) -> None:
        if self.loaded is None:
            return
        axis: Axis = self.axis_var.get()            # type: ignore[assignment]
        position = self.canvas.position()
        out_dir = self._effective_out_dir()
        if out_dir is None:
            return
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("输出目录不可用", f"{out_dir}\n\n{exc}", parent=self)
            return

        stem = self.loaded.path.stem
        p1 = out_dir / f"{stem}_p1.png"
        p2 = out_dir / f"{stem}_p2.png"
        sidecar = out_dir / f"{stem}_split.json"

        existing = [p.name for p in (p1, p2, sidecar) if p.exists()]
        if existing:
            if not messagebox.askyesno(
                "文件已存在",
                "下列文件已存在，要覆盖吗？\n\n" + "\n".join(existing),
                parent=self,
            ):
                return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            im1, im2 = split_image(self.loaded.image, axis, position)
            meta = build_meta(self.loaded, axis, position, [p1.name, p2.name])
            # 源图被转换过（比如 16 位/CMYK）时不能透传它的 ICC，否则颜色会被解释错。
            icc = None if self.loaded.was_converted else self.loaded.icc
            dpi = self.loaded.dpi
            save_png_with_meta(im1, p1, meta=meta, icc=icc, dpi=dpi)
            save_png_with_meta(im2, p2, meta=meta, icc=icc, dpi=dpi)
            save_sidecar(meta, sidecar)
        except PermissionError as exc:
            messagebox.showerror(
                "写入失败",
                f"文件可能正被别的程序占用（比如图片查看器或 AI 工具没释放）：\n\n{exc}",
                parent=self,
            )
            return
        except OSError as exc:
            messagebox.showerror("写入失败", str(exc), parent=self)
            return
        except Exception as exc:                     # noqa: BLE001
            messagebox.showerror("拆分失败", f"{type(exc).__name__}: {exc}", parent=self)
            return
        finally:
            self.configure(cursor="")

        s1, s2 = part_sizes(axis, position, self.loaded.size)
        self.status.configure(
            text=f"已输出 {p1.name}（{s1[0]}×{s1[1]}）和 {p2.name}（{s2[0]}×{s2[1]}）",
            foreground="#1e8449",
        )
        self.open_dir_btn.configure(state="normal")
        self.app.join_tab.prefill(p1)


# --------------------------------------------------------------------------
# 第 7 层：拼接页
# --------------------------------------------------------------------------

class JoinTab(ttk.Frame):
    PREVIEW_CAP = 2000          # 预览用的最大边长，避免拼接时撑爆内存

    def __init__(self, master, app: "App"):
        super().__init__(master, padding=10)
        self.app = app
        self.path1: Optional[Path] = None
        self.path2: Optional[Path] = None
        self._cache: dict[tuple[str, float], LoadedImage] = {}
        self._out_is_auto = True
        self._build_ui()

    # ------------------------------ 界面 ------------------------------

    def _build_ui(self) -> None:
        pick = ttk.Frame(self)
        pick.pack(fill="x")

        ttk.Label(pick, text="子图 1：").grid(row=0, column=0, sticky="w")
        self.path1_var = tk.StringVar()
        ttk.Entry(pick, textvariable=self.path1_var, width=46).grid(
            row=0, column=1, sticky="ew", padx=(0, 6)
        )
        ttk.Button(pick, text="选择…", width=8,
                   command=lambda: self._pick(1)).grid(row=0, column=2)
        ttk.Button(pick, text="⇄ 交换", width=9, command=self._swap).grid(
            row=0, column=3, rowspan=2, padx=8
        )

        ttk.Label(pick, text="子图 2：").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.path2_var = tk.StringVar()
        ttk.Entry(pick, textvariable=self.path2_var, width=46).grid(
            row=1, column=1, sticky="ew", padx=(0, 6), pady=(4, 0)
        )
        ttk.Button(pick, text="选择…", width=8,
                   command=lambda: self._pick(2)).grid(row=1, column=2, pady=(4, 0))

        pick.columnconfigure(1, weight=1)

        row_dir = ttk.Frame(self)
        row_dir.pack(fill="x", pady=(8, 0))
        ttk.Label(row_dir, text="拼接方向：").pack(side="left")
        self.axis_var = tk.StringVar(value="auto")
        for value, text in (("auto", "自动判断"), ("vertical", "垂直（左 | 右）"),
                            ("horizontal", "水平（上 | 下）")):
            ttk.Radiobutton(row_dir, text=text, value=value, variable=self.axis_var,
                            command=self._refresh).pack(side="left", padx=(0, 6))
        ttk.Button(row_dir, text="重新校验", command=self._refresh).pack(side="left", padx=12)

        self.canvas = ImageCanvas(self)
        self.canvas.pack(fill="both", expand=True, pady=8)

        self.report = tk.Text(self, height=5, wrap="word", relief="solid", borderwidth=1,
                              padx=8, pady=6)
        self.report.pack(fill="x")
        self.report.tag_configure("error", foreground="#c0392b")
        self.report.tag_configure("warn", foreground="#b9770e")
        self.report.tag_configure("ok", foreground="#1e8449")
        self.report.tag_configure("plain", foreground="#333333")
        self.report.configure(state="disabled")

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, text="输出文件：").pack(side="left")
        self.out_var = tk.StringVar()
        out_entry = ttk.Entry(bottom, textvariable=self.out_var)
        out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        out_entry.bind("<KeyRelease>", lambda _e: setattr(self, "_out_is_auto", False))
        ttk.Button(bottom, text="浏览…", width=8, command=self._pick_out).pack(side="left")
        self.join_btn = ttk.Button(bottom, text="执行拼接", command=self._do_join, state="disabled")
        self.join_btn.pack(side="left", padx=8)
        ttk.Button(bottom, text="复制报告", command=self._copy_report).pack(side="left")

        self.status = ttk.Label(self, text="选择拆分出来的两个子图，改好之后在这里拼回去",
                                foreground="#666666")
        self.status.pack(fill="x", pady=(6, 0))

    # ------------------------------ 选择文件 ------------------------------

    def prefill(self, path1: Path) -> None:
        """拆分完成后自动把这块填进拼接页，并猜出另一块。"""
        self.path1 = path1
        self.path1_var.set(str(path1))
        sibling = guess_sibling(path1)
        if sibling is not None:
            self.path2 = sibling
            self.path2_var.set(str(sibling))
        self._refresh()

    def _pick(self, which: int) -> None:
        path = filedialog.askopenfilename(
            title=f"选择子图 {which}",
            initialdir=str(self.app.last_dir),
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                       ("所有文件", "*.*")],
        )
        if not path:
            return
        chosen = Path(path)
        self.app.last_dir = chosen.parent
        if which == 1:
            self.path1 = chosen
            self.path1_var.set(str(chosen))
            if self.path2 is None:
                sibling = guess_sibling(chosen)
                if sibling is not None:
                    self.path2 = sibling
                    self.path2_var.set(str(sibling))
        else:
            self.path2 = chosen
            self.path2_var.set(str(chosen))
            if self.path1 is None:
                sibling = guess_sibling(chosen)
                if sibling is not None:
                    self.path1 = sibling
                    self.path1_var.set(str(sibling))
        self._refresh()

    def _swap(self) -> None:
        self.path1, self.path2 = self.path2, self.path1
        self.path1_var.set(str(self.path1) if self.path1 else "")
        self.path2_var.set(str(self.path2) if self.path2 else "")
        self._refresh()

    def _pick_out(self) -> None:
        base = self.path1 or self.path2
        initial = base.parent if base else self.app.last_dir
        chosen = filedialog.asksaveasfilename(
            title="保存拼接结果",
            initialdir=str(initial),
            initialfile=Path(self.out_var.get()).name if self.out_var.get() else "joined.png",
            defaultextension=".png",
            filetypes=[("PNG 图片", "*.png")],
        )
        if chosen:
            self._out_is_auto = False
            self.out_var.set(chosen)

    # ------------------------------ 加载与校验 ------------------------------

    def _load_cached(self, path: Path) -> LoadedImage:
        """按 路径+mtime 缓存，避免每次刷新都重新解码大图。"""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = (str(path), mtime)
        cached = self._cache.get(key)
        if cached is None:
            cached = open_image(path)
            self._cache = {k: v for k, v in self._cache.items() if k[0] != str(path)}
            self._cache[key] = cached
        return cached

    def _refresh(self) -> None:
        self._set_report([("", ["选择两个子图后会自动校验。"])])
        self.canvas.set_image(None)
        self.join_btn.configure(state="disabled")

        if self.path1 is None or self.path2 is None:
            self.status.configure(text="还需要选择另一张子图", foreground="#666666")
            return

        try:
            loaded1 = self._load_cached(self.path1)
            loaded2 = self._load_cached(self.path2)
        except ImageLoadError as exc:
            self._set_report([("", [f"[错误] {line}" for line in str(exc).splitlines()])])
            self.status.configure(text="有文件打不开", foreground="#c0392b")
            return

        lookup1 = lookup_meta(self.path1)
        lookup2 = lookup_meta(self.path2)
        notes: list[str] = []

        axis, reason = self._resolve_axis(loaded1.size, loaded2.size, lookup1, lookup2)
        if axis is None:
            self._set_report([("无法自动判断拼接方向", [
                f"[错误] {reason}",
                "",
                "请手动选择拼接方向，或确认这两张图确实是从同一张图切出来的。",
            ])])
            self.status.configure(text="无法自动判断方向", foreground="#c0392b")
            return
        notes.append(f"方向：{reason}")

        # 元数据来源说明，让用户知道依据是什么。
        for idx, lookup, path in ((1, lookup1, self.path1), (2, lookup2, self.path2)):
            if lookup.meta is not None:
                part = identify_part(lookup.meta, path)
                part_text = f"，第 {part} 块" if part else ""
                notes.append(f"子图 {idx}：来自 {lookup.source}{part_text}（{path.name}）")
            else:
                notes.append(f"子图 {idx}：没有找到拆分信息，按图片尺寸推断（{path.name}）")

        # 输出路径在校验之前就刷新，这样即使校验没过，显示的也是当前这一对的路径。
        # 只在用户没手动改过时才自动填，否则会把人家填的覆盖掉。
        if self._out_is_auto:
            self.out_var.set(str(self._default_output_path()))

        check = validate_join(
            loaded1.size, loaded2.size, axis,
            meta1=lookup1.meta, meta2=lookup2.meta,
            name1="子图 1", name2="子图 2",
            path1=str(self.path1), path2=str(self.path2),
        )

        blocks: list[tuple[str, list[str]]] = []
        body: list[str] = list(notes)
        for line in check.errors:
            body.extend(f"[错误] {ln}" for ln in line.splitlines())
        for line in check.warnings:
            body.extend(f"[提示] {ln}" for ln in line.splitlines())
        if check.ok:
            body.append(f"[OK] {check.reason}")

        if not check.ok:
            body.append("")
            body.append(
                "把尺寸改回「期望」的那个值再来拼：AI 修图工具经常悄悄改变尺寸，"
                "裁掉多出来的几像素、或补足缺的那几像素即可。"
            )
        else:
            body.append("")
            body.append("修改过子图后尺寸如果有变，拼接会被拒绝——这是刻意的，"
                        "因为只有严丝合缝才能还原成原图。")

        blocks.append(("校验结果", body))
        self._set_report(blocks)

        if not check.ok:
            self.status.configure(text="校验未通过，已阻止拼接", foreground="#c0392b")
            return

        # 预览：先缩后拼，避免为了看一眼就生成一张巨图。
        try:
            preview1, preview2 = loaded1.image, loaded2.image
            w1, h1 = preview1.size
            w2, h2 = preview2.size
            joined_size = (w1 + w2, h1) if axis == "vertical" else (w1, h1 + h2)
            scale = min(1.0, self.PREVIEW_CAP / max(joined_size))
            if scale < 1.0:
                preview1 = preview1.resize(
                    (max(1, round_half_up(w1 * scale)), max(1, round_half_up(h1 * scale))),
                    Image.Resampling.LANCZOS, reducing_gap=2.0,
                )
                preview2 = preview2.resize(
                    (max(1, round_half_up(w2 * scale)), max(1, round_half_up(h2 * scale))),
                    Image.Resampling.LANCZOS, reducing_gap=2.0,
                )
                if axis == "vertical":
                    target_h = min(preview1.size[1], preview2.size[1])
                    preview1 = preview1.crop((0, 0, preview1.size[0], target_h))
                    preview2 = preview2.crop((0, 0, preview2.size[0], target_h))
                else:
                    target_w = min(preview1.size[0], preview2.size[0])
                    preview1 = preview1.crop((0, 0, target_w, preview1.size[1]))
                    preview2 = preview2.crop((0, 0, target_w, preview2.size[1]))
            p1, p2, mode_warnings = harmonize_modes(preview1, preview2, "子图 1", "子图 2")
            joined = join_images(p1, p2, axis)
        except (JoinError, OSError, ValueError) as exc:
            self.status.configure(text=f"预览失败：{exc}", foreground="#c0392b")
            return

        self.canvas.set_image(joined, axis=axis, interactive=False)
        self.join_btn.configure(state="normal")
        self.status.configure(
            text=f"可以拼接：{check.reason}", foreground="#1e8449"
        )

    def _default_output_path(self) -> Path:
        meta = None
        base = self.path1 or self.path2
        for path in (self.path1, self.path2):
            if path is None:
                continue
            found = lookup_meta(path).meta
            if found is not None:
                meta = found
                break
        if meta is not None and meta.original_name:
            stem = Path(meta.original_name).stem
        else:
            stem = PART_RE.sub("", (base.stem if base else "image"))
        parent = base.parent if base else self.app.last_dir
        return parent / f"{stem}_joined.png"

    def _resolve_axis(self, size1: Size, size2: Size, lookup1: MetaLookup,
                      lookup2: MetaLookup) -> tuple[Optional[Axis], str]:
        """定拼接方向：拆分记录优先于几何推断。

        记录是实打实的，几何只是猜——两张图宽度碰巧相同，不代表它们就该上下拼。
        把优先级搞反会放行"拼出来根本不是原图"的组合。
        """
        mode = self.axis_var.get()
        if mode != "auto":
            return mode, f"{AXIS_LABEL[mode]}（手动指定）"  # type: ignore[return-value]

        recorded = None
        for lookup in (lookup1, lookup2):
            if lookup.meta is not None:
                recorded = lookup.meta.axis
                break

        geo_axis, geo_reason = infer_axis(size1, size2)
        if recorded is not None:
            reason = "按拆分时记录的方向拼接"
            if geo_axis is not None and geo_axis != recorded:
                reason += f"（按尺寸看更像{AXIS_LABEL[geo_axis]}，要改就手动选）"
            return recorded, reason           # type: ignore[return-value]
        if geo_axis is not None:
            return geo_axis, geo_reason
        return None, geo_reason

    def _set_report(self, blocks: list[tuple[str, list[str]]]) -> None:
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        for heading, lines in blocks:
            if heading:
                self.report.insert("end", heading + "\n", "plain")
            for line in lines:
                tag = "plain"
                if line.startswith("[错误] "):
                    tag, line = "error", line[len("[错误] "):]
                elif line.startswith("[提示] "):
                    tag, line = "warn", line[len("[提示] "):]
                elif line.startswith("[OK] "):
                    tag, line = "ok", line[len("[OK] "):]
                self.report.insert("end", line + "\n", tag)
        self.report.configure(state="disabled")

    def _copy_report(self) -> None:
        text = self.report.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="校验报告已复制到剪贴板", foreground="#1e8449")

    # ------------------------------ 执行拼接 ------------------------------

    def _do_join(self) -> None:
        if self.path1 is None or self.path2 is None:
            return
        try:
            loaded1 = self._load_cached(self.path1)
            loaded2 = self._load_cached(self.path2)
        except ImageLoadError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)
            return

        lookup1 = lookup_meta(self.path1)
        lookup2 = lookup_meta(self.path2)
        axis, _reason = self._resolve_axis(loaded1.size, loaded2.size, lookup1, lookup2)
        if axis is None:
            messagebox.showerror("无法判断拼接方向", "请先手动选择拼接方向。", parent=self)
            return

        check = validate_join(
            loaded1.size, loaded2.size, axis,
            meta1=lookup1.meta, meta2=lookup2.meta,
            name1="子图 1", name2="子图 2",
            path1=str(self.path1), path2=str(self.path2),
        )
        if not check.ok:
            show_report(self, "尺寸不符，无法拼接", [
                ("拼接被阻止", [f"[错误] {ln}" for line in check.errors for ln in line.splitlines()]),
                ("怎么办", [
                    "拼接要求两张子图在垂直于接缝的方向上尺寸完全一致，",
                    "否则拼出来的图会有错位或缺失，也就不可能还原成原图。",
                    "",
                    "请把尺寸不对的那张改回去（裁掉多出来的几像素、或补足缺的），再重新拼接。",
                ]),
            ])
            return

        out_raw = self.out_var.get().strip()
        if not out_raw:
            out = self._default_output_path()
        else:
            out = Path(out_raw)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("输出目录不可用", f"{out.parent}\n\n{exc}", parent=self)
            return
        if out.exists() and not messagebox.askyesno(
            "文件已存在", f"要覆盖吗？\n\n{out}", parent=self
        ):
            return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            im1, im2, _warn = harmonize_modes(loaded1.image, loaded2.image, "子图 1", "子图 2")
            joined = join_images(im1, im2, axis)
            icc = loaded1.icc if (loaded1.icc and not loaded1.was_converted) else None
            dpi = loaded1.dpi
            save_png_with_meta(joined, out, icc=icc, dpi=dpi, compress_level=6)
        except JoinError as exc:
            show_report(self, "拼接失败", [("错误", [f"[错误] {ln}" for ln in exc.messages])])
            return
        except PermissionError as exc:
            messagebox.showerror(
                "写入失败",
                f"文件可能正被别的程序占用：\n\n{exc}",
                parent=self,
            )
            return
        except OSError as exc:
            messagebox.showerror("写入失败", str(exc), parent=self)
            return
        finally:
            self.configure(cursor="")

        self.status.configure(
            text=f"已输出 {out.name}（{joined.size[0]}×{joined.size[1]}）", foreground="#1e8449"
        )
        self.out_var.set(str(out))


# --------------------------------------------------------------------------
# 第 8 层：主窗口
# --------------------------------------------------------------------------

def open_in_explorer(path: Path) -> None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)                       # type: ignore[attr-defined]
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:                         # noqa: BLE001
        messagebox.showerror("打开失败", f"{path}\n\n{exc}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("图像切割 / 拼接工具")
        self.geometry("1120x800")
        self.minsize(860, 640)

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        self.last_dir = Path.home()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.split_tab = SplitTab(self.notebook, self)
        self.join_tab = JoinTab(self.notebook, self)
        self.notebook.add(self.split_tab, text="　拆分　")
        self.notebook.add(self.join_tab, text="　拼接　")


def enable_dpi_awareness() -> None:
    """Windows 高分屏下让预览更清晰。失败也无所谓，不影响功能。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> None:
    enable_dpi_awareness()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
