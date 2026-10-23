# -*- coding: utf-8 -*-
"""GUI 端到端测试：真实创建窗口和控件，走完"打开 → 调线 → 拆分 → 拼接"全流程。

会短暂弹出程序窗口，跑完自动关闭。需要图形界面，无头环境跑不了。

跑法：  python test_gui.py
"""
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops

import image_cut as ic

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp(prefix="image_cut_gui_"))

# 造一张大图，模拟真实使用（4000x3000 是常见手机/相机尺寸）
src = tmp / "大图.png"
big = Image.new("RGB", (4000, 3000))
px = big.load()
for y in range(0, 3000, 3):
    for x in range(0, 4000, 3):
        px[x, y] = ((x // 3) % 256, (y // 3) % 256, ((x + y) // 6) % 256)
big.save(src)

app = ic.App()
app.update()
app.update_idletasks()

tab = app.split_tab
canvas = tab.canvas

t0 = time.time()
tab.load_path(src)
for _ in range(12):
    app.update()
    time.sleep(0.02)
load_ms = (time.time() - t0) * 1000

check("打开大图后画布已渲染出图像", canvas._image_item is not None)
check("分割线已画出", canvas._line_id is not None)
check("预览已缓存", canvas._preview is not None)
check("加载+首次渲染在 3 秒内", load_ms < 3000, f"{load_ms:.0f} ms")
print(f"        （4000x3000 加载+渲染耗时 {load_ms:.0f} ms，预览缩放 {canvas.preview_scale()*100:.1f}%）")

check("初始位置在中间", canvas.position() == 2000, str(canvas.position()))
check("px 输入框已同步", tab.px_var.get() == "2000", tab.px_var.get())
check("百分比已同步", tab.pct_var.get() == "50.0", tab.pct_var.get())
check("尺寸标签已显示", "左 2000×3000" in tab.size_label.cget("text"),
      tab.size_label.cget("text"))

# --- 拖动分割线 ---
x0, y0 = canvas.view.img_to_canvas(canvas.position(), 1500)
canvas._on_press(type("E", (), {"x": x0, "y": y0})())
check("按下后进入拖动状态", canvas._dragging)
canvas._on_drag(type("E", (), {"x": x0 + 100, "y": y0})())
check("拖动改变了位置", canvas.position() != 2000, str(canvas.position()))
check("拖动后 px 框跟着变", tab.px_var.get() == str(canvas.position()),
      f"{tab.px_var.get()} vs {canvas.position()}")
canvas._on_release(type("E", (), {"x": x0 + 100, "y": y0})())
check("松开后退出拖动状态", not canvas._dragging)

# --- 放大镜 ---
canvas._update_loupe(x0 + 100, y0)
check("放大镜已显示", bool(canvas._loupe_ids) and canvas._loupe_photo is not None)
check("放大镜十字准星压准切割列",
      abs(canvas.canvas.coords(canvas._loupe_ids[2])[0]
          - (canvas.canvas.coords(canvas._loupe_ids[0])[0] + ic.ImageCanvas.LOUPE_SRC
             * ic.ImageCanvas.LOUPE_ZOOM / 2)) < 0.01)
canvas._update_loupe(-500, -500)
check("鼠标移出图像后放大镜隐藏",
      canvas.canvas.itemcget(canvas._loupe_ids[0], "state") == "hidden")

# --- 数字输入 ---
tab.px_var.set("1234")
tab._commit_px()
check("输入 px 生效", canvas.position() == 1234, str(canvas.position()))
check("输入 px 后百分比同步", tab.pct_var.get() == "30.9", tab.pct_var.get())
tab.pct_var.set("80")
tab._commit_pct()
check("输入百分比生效", canvas.position() == 3200, str(canvas.position()))
check("输入百分比后 px 同步", tab.px_var.get() == "3200", tab.px_var.get())

# --- 越界钳制 ---
tab.px_var.set("0")
tab._commit_px()
check("输入 0 被钳到 1", canvas.position() == 1, str(canvas.position()))
tab.px_var.set("99999")
tab._commit_px()
check("超界被钳到 n-1", canvas.position() == 3999, str(canvas.position()))
tab.px_var.set("abc")
tab._commit_px()
check("非法输入不改变位置", canvas.position() == 3999, str(canvas.position()))

# --- 方向键微调 ---
from types import SimpleNamespace
canvas._nudge(-1, SimpleNamespace(state=0, keysym="Left"))
check("左方向键 -1px", canvas.position() == 3998, str(canvas.position()))
canvas._nudge(-1, SimpleNamespace(state=1, keysym="Left"))
check("Shift+左方向键 -10px", canvas.position() == 3988, str(canvas.position()))

# --- 切换方向 ---
tab.axis_var.set("horizontal")
tab._on_axis()
for _ in range(6):
    app.update()
check("切到水平方向", canvas.axis() == "horizontal")
check("切换方向后百分比保留（应仍约 99%）",
      abs(canvas.position() / 3000 - 3988 / 4000) < 0.01,
      f"{canvas.position()}/3000")
check("水平方向尺寸标签正确", "上 4000×" in tab.size_label.cget("text"),
      tab.size_label.cget("text"))

# --- 执行拆分（真的走一遍 UI 路径）---
tab.px_var.set("1200")
tab._commit_px()
out_dir = tmp / "out"
tab.outdir_var.set(str(out_dir))
tab._do_split()
app.update()

p1 = out_dir / "大图_p1.png"
p2 = out_dir / "大图_p2.png"
sidecar = out_dir / "大图_split.json"
check("拆分输出了 p1", p1.exists())
check("拆分输出了 p2", p2.exists())
check("拆分输出了 sidecar", sidecar.exists())
check("拆分后提示了输出结果", "已输出" in tab.status.cget("text"), tab.status.cget("text"))
check("拆分后打开了「打开输出目录」按钮",
      str(tab.open_dir_btn.cget("state")) == "normal")

im1 = Image.open(p1)
im2 = Image.open(p2)
check("p1 尺寸正确（水平切割，上块）", im1.size == (4000, 1200), str(im1.size))
check("p2 尺寸正确（水平切割，下块）", im2.size == (4000, 1800), str(im2.size))

# --- 切到拼接页（真实使用路径：隐藏页的 canvas 尺寸恒为 1，切过去才会渲染）---
jt = app.join_tab
app.notebook.select(jt)
for _ in range(10):
    app.update()
    time.sleep(0.02)
check("拼接页自动填入子图 1", str(jt.path1) == str(p1), str(jt.path1))
check("拼接页自动找到子图 2", str(jt.path2) == str(p2), str(jt.path2))
check("自动判断出拼接方向", jt.axis_var.get() == "auto")
check("校验通过后可执行拼接", str(jt.join_btn.cget("state")) == "normal",
      str(jt.join_btn.cget("state")))
check("切到拼接页后预览已渲染", jt.canvas._image_item is not None)
check("输出路径自动填好", jt.out_var.get().endswith("大图_joined.png"), jt.out_var.get())

# --- 执行拼接 ---
jt._do_join()
app.update()
joined_path = Path(jt.out_var.get())
check("拼接结果已输出", joined_path.exists(), str(joined_path))
joined = Image.open(joined_path)
check("拼接结果尺寸等于原图", joined.size == (4000, 3000), str(joined.size))
check("拼接结果与原图逐像素一致",
      ImageChops.difference(joined.convert("RGB"), big).getbbox() is None)

# --- 尺寸不符时必须报错并阻止 ---
# 故意改个认不出块号的名字，验证"按尺寸反推"的兜底
bad_p2 = out_dir / "大图_被AI改过.png"
im2.crop((0, 0, 4000, 1797)).save(bad_p2)
jt.path2 = bad_p2
jt.path2_var.set(str(bad_p2))
jt._refresh()
for _ in range(4):
    app.update()
check("尺寸不符时禁用拼接按钮", str(jt.join_btn.cget("state")) == "disabled",
      str(jt.join_btn.cget("state")))
check("尺寸不符时状态栏变红提示", "阻止" in jt.status.cget("text"), jt.status.cget("text"))
report = jt.report.get("1.0", "end")
check("报告里指明是尺寸和拆分记录对不上",
      "尺寸和拆分时对不上" in report, report[:250])
check("报告里给出期望尺寸和差值（认不出块号时按尺寸反推）",
      "4000×1800" in report and "-3" in report, report[:500])

# --- 关键回归：几何猜测不能盖过拆分记录 ---
# 在正中间竖切，两块宽度都是 2000。把第二块改矮 3px 且抹掉它自己的记录：
# 只看尺寸会以为"宽度相同 → 该上下拼"，从而放行一个根本不是原图的结果。
# 正确行为是听拆分记录的（左右拼），然后因为高度不等而拒绝。
half_dir = tmp / "halfsplit"
half_dir.mkdir()
h1, h2 = ic.split_image(big, "vertical", 2000)
hp1 = half_dir / "half_p1.png"
hp2 = half_dir / "half_p2_改过.png"
hmeta = ic.build_meta(ic.open_image(src), "vertical", 2000, ["half_p1.png", "half_p2.png"])
ic.save_png_with_meta(h1, hp1, meta=hmeta)
h2.crop((0, 0, 2000, 2997)).save(hp2)          # 不带任何记录
check("第二块确实读不到记录", ic.lookup_meta(hp2).meta is None)

jt.path1, jt.path2 = hp1, hp2
jt.path1_var.set(str(hp1))
jt.path2_var.set(str(hp2))
jt.axis_var.set("auto")
jt._refresh()
for _ in range(4):
    app.update()
check("自动模式听从拆分记录（垂直）而不是几何猜测（水平）",
      "垂直" in jt.status.cget("text") or "阻止" in jt.status.cget("text"),
      jt.status.cget("text"))
check("因此拒绝了这个会拼出错误尺寸的组合",
      str(jt.join_btn.cget("state")) == "disabled", str(jt.join_btn.cget("state")))
check("报告的根因里写明高度差",
      "-3" in jt.report.get("1.0", "end"), jt.report.get("1.0", "end")[:300])

# --- 交换按钮 ---
jt.path2 = p2
jt.path2_var.set(str(p2))
jt._refresh()
before1, before2 = jt.path1, jt.path2
jt._swap()
check("交换按钮生效", jt.path1 == before2 and jt.path2 == before1)

# --- 关闭再开，确认没有资源泄漏报错 ---
app.destroy()

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} 项失败：")
    for f in FAILS:
        print("  -", f)
else:
    print("全部通过")
sys.exit(1 if FAILS else 0)
