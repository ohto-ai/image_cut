# -*- coding: utf-8 -*-
"""核心逻辑回归测试（不需要图形界面）。

重点验证这个工具的核心承诺：拆分再拼接之后，结果与原图**逐像素一致**。
另外覆盖还原信息的四级回退、以及尺寸不符时的严格校验。

跑法：  python test_core.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

import image_cut as ic

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_image(mode, size=(301, 197)):
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
    if mode == "RGB":
        return im
    if mode == "RGBA":
        out = im.convert("RGBA")
        out.putalpha(Image.new("L", size, 128))
        return out
    if mode == "P":
        return im.convert("P", palette=Image.Palette.ADAPTIVE)
    if mode == "I;16":
        return im.convert("L").convert("I;16")
    if mode == "CMYK":
        return im.convert("CMYK")
    raise ValueError(mode)


def identical(a, b):
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


tmp = Path(tempfile.mkdtemp(prefix="image_cut_test_"))
print(f"工作目录: {tmp}\n")

# ---------------------------------------------------------------- 往返校验
print("=== 1. 往返像素校验（拆分 → 拼接 必须与原图逐像素相同）===")
for mode in ("RGB", "RGBA", "P", "I;16", "CMYK"):
    # PNG 存不了 CMYK，这种源图用 TIFF
    ext = ".tif" if mode == "CMYK" else ".png"
    src = tmp / f"src_{mode.replace(';', '')}{ext}"
    make_image(mode).save(src)
    loaded = ic.open_image(src)
    for axis in ("vertical", "horizontal"):
        n = ic.axis_length(axis, loaded.size)
        for pos in (1, n // 2, n - 1, max(1, n // 3)):
            im1, im2 = ic.split_image(loaded.image, axis, pos)
            joined = ic.join_images(im1, im2, axis)
            check(
                f"{mode} {axis} pos={pos} 往返一致",
                joined.size == loaded.size and identical(joined, loaded.image),
                f"{joined.size} vs {loaded.size}",
            )

# ------------------------------------------------------------ 元数据回退链
print("\n=== 2. 元数据四级回退 ===")
src = tmp / "photo.png"
make_image("RGB", (400, 300)).save(src)
loaded = ic.open_image(src)
out = tmp / "out"
out.mkdir()
im1, im2 = ic.split_image(loaded.image, "vertical", 150)
p1, p2 = out / "photo_p1.png", out / "photo_p2.png"
meta = ic.build_meta(loaded, "vertical", 150, [p1.name, p2.name])
ic.save_png_with_meta(im1, p1, meta=meta)
ic.save_png_with_meta(im2, p2, meta=meta)
ic.save_sidecar(meta, out / "photo_split.json")

m = ic.read_png_meta(p1)
check("iTXt 内嵌可读回", m is not None and m.position == 150 and m.axis == "vertical",
      repr(m))
check("内嵌 meta 分量正确", m is not None and m.part(1).size == [150, 300] and m.part(2).size == [250, 300])
check("sidecar 可读回", ic.load_sidecar_for(p2) is not None)
check("lookup 优先内嵌", ic.lookup_meta(p1).source == "PNG 内嵌信息", ic.lookup_meta(p1).source)

# 改名：内嵌信息应该仍然生效
renamed = out / "我的猫.png"
shutil.copy(p1, renamed)
lk = ic.lookup_meta(renamed)
check("改名后靠内嵌信息仍能识别", lk.meta is not None and lk.source == "PNG 内嵌信息", lk.source)

# 抹掉内嵌信息（模拟 AI 工具重新导出），只剩 sidecar
out2 = tmp / "stripped"
out2.mkdir()
stripped = out2 / "photo_p1.png"
Image.open(p1).save(stripped)
shutil.copy(out / "photo_split.json", out2 / "photo_split.json")
check("抹掉 iTXt 后读不到内嵌", ic.read_png_meta(stripped) is None)
check("抹掉后回退到 sidecar", ic.lookup_meta(stripped).source == "同名 sidecar JSON",
      ic.lookup_meta(stripped).source)

# 既抹掉内嵌、又改了名，靠 sidecar 里的文件名记录认出来
renamed_stripped = out2 / "photo_p1_改过.png"
Image.open(p1).save(renamed_stripped)
check("抹掉+改名后仍能靠 sidecar 认出",
      ic.lookup_meta(renamed_stripped).meta is not None,
      ic.lookup_meta(renamed_stripped).source)
check("认出来时是第 1 块",
      ic.identify_part(ic.lookup_meta(p1).meta, p1) == 1)
check("认出来时是第 2 块",
      ic.identify_part(ic.lookup_meta(p2).meta, p2) == 2)

# 两个都没有 → 靠几何推断
bare1 = out / "bare_a.png"
bare2 = out / "bare_b.png"
im1.save(bare1)
im2.save(bare2)
check("无元数据时读不到", ic.lookup_meta(bare1).meta is None)
axis, reason = ic.infer_axis(Image.open(bare1).size, Image.open(bare2).size)
check("无元数据时几何推断方向", axis == "vertical", f"{axis} / {reason}")

same1 = Image.new("RGB", (100, 100))
same2 = Image.new("RGB", (100, 100))
axis, reason = ic.infer_axis(same1.size, same2.size)
check("尺寸相同的歧义情况不报错、给默认值", axis == "vertical", f"{axis} / {reason}")

axis, reason = ic.infer_axis((100, 50), (80, 60))
check("尺寸完全对不上时返回 None", axis is None, f"{axis} / {reason}")

# -------------------------------------------------------------- 严格校验
print("\n=== 3. 严格校验（尺寸不符必须报错）===")
lm1 = ic.open_image(p1)
lm2 = ic.open_image(p2)
lk1, lk2 = ic.lookup_meta(p1), ic.lookup_meta(p2)

ok = ic.validate_join(lm1.size, lm2.size, "vertical", lk1.meta, lk2.meta,
                      path1=str(p1), path2=str(p2))
check("正常情况校验通过", ok.ok, str(ok.errors))

cropped = out / "photo_p2_cropped.png"
lm2.image.crop((0, 0, 250, 298)).save(cropped)     # 高度少了 2px
lc = ic.open_image(cropped)
bad = ic.validate_join(lm1.size, lc.size, "vertical", lk1.meta, ic.lookup_meta(cropped).meta,
                       name1="子图 1", name2="子图 2", path1=str(p1), path2=str(cropped))
check("高度差 2px 被拒绝", not bad.ok, str(bad.errors))
check("错误信息给出期望尺寸和差值",
      bool(bad.errors) and "250×300" in bad.errors[0] and "-2" in bad.errors[0],
      bad.errors[0] if bad.errors else "")
check("有拆分记录时不再重复报几何错误（避免信息冗余）",
      len(bad.errors) == 1, str(bad.errors))

# 没有元数据时，几何校验必须顶上来
geo = ic.validate_join((250, 300), (250, 298), "vertical", None, None)
check("无元数据时仍能靠几何拒绝", not geo.ok, str(geo.errors))
check("无元数据的错误信息指出是哪条边",
      bool(geo.errors) and "高度" in geo.errors[0] and "-2" in geo.errors[0],
      geo.errors[0] if geo.errors else "")
try:
    ic.join_images(lm1.image, lc.image, "vertical")
    check("join_images 也拒绝拼尺寸不符的图", False, "居然没抛异常")
except ic.JoinError:
    check("join_images 也拒绝拼尺寸不符的图", True)

# 拿错文件：另一张原图的子图
other = tmp / "other.png"
make_image("RGB", (500, 480)).save(other)
ol = ic.open_image(other)
o1, o2 = ic.split_image(ol.image, "vertical", 200)
op1 = out / "other_p1.png"
om = ic.build_meta(ol, "vertical", 200, [op1.name, "other_p2.png"])
ic.save_png_with_meta(o1, op1, meta=om)
mismatch = ic.validate_join(lm1.size, ic.open_image(op1).size, "vertical",
                            lk1.meta, ic.lookup_meta(op1).meta,
                            path1=str(p1), path2=str(op1))
check("不同次拆分的两块被拒绝", not mismatch.ok, str(mismatch.errors))
check("根因（不是同一次拆分）排在第一条",
      bool(mismatch.errors) and "不是同一次拆分" in mismatch.errors[0],
      mismatch.errors[0] if mismatch.errors else "")

# 顺序反了
swapped = ic.validate_join(lm2.size, lm1.size, "vertical", lk2.meta, lk1.meta,
                           name1="子图 1", name2="子图 2",
                           path1=str(p2), path2=str(p1))
check("顺序反了会给出提示", any("顺序反了" in w for w in swapped.warnings), str(swapped.warnings))

# ---------------------------------------------------------------- 猜兄弟文件
print("\n=== 4. 自动找另一块 ===")
sib = ic.guess_sibling(p1)
check("按元数据找到另一块", sib is not None and sib.name == p2.name, str(sib))

# 改过名，但内嵌信息还在（记录里写着原名，找不到就退回文件名规则）
renamed_dir = tmp / "renamed"
renamed_dir.mkdir()
shutil.copy(p1, renamed_dir / "我的猫_p1.png")
shutil.copy(p2, renamed_dir / "我的猫_p2.png")
sib = ic.guess_sibling(renamed_dir / "我的猫_p1.png")
check("改名后仍能找到另一块", sib is not None and sib.name == "我的猫_p2.png", str(sib))

# 彻底没有元数据，只能靠 _p1/_p2 文件名
bare_dir = tmp / "barepair"
bare_dir.mkdir()
im1.save(bare_dir / "x_p1.png")
im2.save(bare_dir / "x_p2.png")
sib = ic.guess_sibling(bare_dir / "x_p2.png")
check("纯靠文件名找到另一块", sib is not None and sib.name == "x_p1.png", str(sib))

# ------------------------------------------------------------ 元数据健壮性
print("\n=== 5. 元数据健壮性 ===")
check("垃圾 JSON 不炸", ic.SplitMeta.from_json("这不是 json") is None)
check("缺字段不炸", ic.SplitMeta.from_dict({"axis": "vertical"}) is None)
check("非法 position 被拒", ic.SplitMeta.from_dict(
    {"axis": "vertical", "original_size": [100, 50], "position": 0}) is None)
check("非法 position=100 被拒", ic.SplitMeta.from_dict(
    {"axis": "vertical", "original_size": [100, 50], "position": 100}) is None)
check("中文文件名能往返", ic.SplitMeta.from_json(meta.to_json()).original_name
      == meta.original_name)
meta_cn = ic.build_meta(loaded, "vertical", 150, ["中文名_p1.png", "中文名_p2.png"])
check("中文文件名进 JSON 后仍可读", ic.SplitMeta.from_json(meta_cn.to_json()).part(1).filename
      == "中文名_p1.png")

# ------------------------------------------------------------------ 边界
print("\n=== 6. 边界 ===")
check("round_half_up 处理 .5", ic.round_half_up(0.5) == 1 and ic.round_half_up(1.5) == 2
      and ic.round_half_up(2.5) == 3)
check("parse_number 挡掉中间态和 inf/nan",
      ic.parse_number("12.") == 12.0 and ic.parse_number("-") is None
      and ic.parse_number("") is None and ic.parse_number("abc") is None
      and ic.parse_number("inf") is None and ic.parse_number("nan") is None
      and ic.parse_number(" 42 ") == 42.0,
      f'{ic.parse_number("inf")}, {ic.parse_number("nan")}')
check("part_sizes 左右之和等于原宽",
      sum(s[0] for s in ic.part_sizes("vertical", 150, (400, 300))) == 400)
check("part_sizes 上下之和等于原高",
      sum(s[1] for s in ic.part_sizes("horizontal", 120, (400, 300))) == 300)
vt = ic.ViewTransform.fit(800, 600, 4000, 3000, 12)
cx, cy = vt.img_to_canvas(2000, 1500)
ix, iy = vt.canvas_to_img(cx, cy)
check("ViewTransform 往返一致", abs(ix - 2000) < 1e-6 and abs(iy - 1500) < 1e-6,
      f"{ix},{iy}")
check("缩放后居中留边（ox>0）", vt.ox >= 0 and vt.oy >= 0, f"{vt.ox},{vt.oy}")

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} 项失败：")
    for f in FAILS:
        print("  -", f)
else:
    print("全部通过")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if FAILS else 0)
