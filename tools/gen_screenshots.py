"""
tools/gen_screenshots.py — 生成 README / docs 里的产品界面截图

**这些图是驱动真实应用拍的，不是合成图、不是 DOM 拷贝。** 脚本用无头 Chrome 打开
本地跑着的 http://localhost:8000，填表、提交、等真实结果回来，再整页截图。
每张图对应什么输入写在 SHOTS 里，可以复现、可以核对。

用法：
    uvicorn app:app --reload          # 先起本地应用
    python -m tools.gen_screenshots            # 全部
    python -m tools.gen_screenshots hero.en.png abstain.en.png   # 只拍指定的

依赖（**都不进 requirements.txt**，理由同 gen_charts.py 的 matplotlib——
只在本地重新出图时需要，进了会被打进 Vercel 函数包）：
    pip install websocket-client pillow
    本机装有 Google Chrome

两个坑，都是实际踩过的：
  1. 卡片有入场动画（opacity/transform 渐显），不关掉会拍到半透明的中间帧
  2. 结果页很长，直接放 README 是一根竖长条。所以整页拍完切成左右双栏——
     切分线必须落在**卡片之间的空隙**上，50/50 硬切会从某张卡中间切开
"""

import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

import websocket
from PIL import Image

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs/images"
APP = "http://localhost:8000"
PORT = 9222
BG = (10, 14, 23)          # 页面底色 #0a0e17，拼接时填空白用
GUTTER = 48                # 双栏中间的间距（CSS px）
SCALE = 2                  # 设备像素比，2x 在 README 里缩放显示不糊

# 卡片选择器 —— 切分线只能落在这些元素之间的空隙上
CARDS = ".risk-hero, .info-card, .trace-panel, .submit-btn"

# 中英两张 hero 与 trace 用**同一条航线、同一天**：「跨海客滚航线」预设（烟台→大连，
# 英文界面自动填 Yantai→Dalian）+ 第 HERO_DAY 个日期按钮（0 = 今天，1 = 明天，2 = 后天）。
# 天气每天变，出图前先用规则引擎探一遍哪天有触发项（evidence.build_evidence + rules.required_level），
# 再改 HERO_DAY。2026-09-29 出图时选的是明天（2026-09-30，HIGH，5 条触发项含航线中点）。
HERO_DAY = 1
HERO_JS = (f"loadPreset('high-ship'); document.querySelectorAll('.date-btn')[{HERO_DAY}].click();"
           " submitAssess();")

# (输出文件名, 触发这个状态的 JS, 说明, 是否切成双栏)
SHOTS = [
    ("hero.png", HERO_JS,
     "烟台→大连 船只 · 完整结果页（含航线中点）", True),
    ("abstain.png",
     "loadPreset('low'); submitAssess();",
     "北京→西安 船只 · 证据不足第四态（确定性，任何时候都一样）", True),
    ("hero.en.png", "switchLang(); " + HERO_JS,
     "Yantai → Dalian · full result page (English UI)", True),
    ("abstain.en.png",
     "switchLang(); loadPreset('low'); submitAssess();",
     "Beijing → Xi'an by ship · insufficient evidence (English UI)", True),
    ("trace.png", HERO_JS,
     "真实执行轨迹 · 确定性预取 / 模型 / 规则引擎 三类步骤分开标注", False),
]

# trace 只截轨迹面板，其余截整个结果区
CLIP_SELECTOR = {"trace.png": ".trace-panel"}

KILL_ANIM = """(()=>{const s=document.createElement('style');
    s.textContent='*,*::before,*::after{animation:none!important;'+
      'transition:none!important;opacity:1!important;transform:none!important}';
    document.head.appendChild(s);})()"""

# 取所有卡片的上下边界（页面绝对坐标），供切分用。
# 过滤掉高度为 0 的 —— 表单里隐藏的元素 rect 全是 0，会在列表里造出一个假空隙。
CARD_BOUNDS = """(()=>[...document.querySelectorAll('%s')]
    .map(e=>e.getBoundingClientRect()).filter(r=>r.height>1)
    .map(r=>[r.top+scrollY, r.bottom+scrollY]))()""" % CARDS


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=120)
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr,
                      awaitPromise=await_promise, returnByValue=True)
        return r.get("result", {}).get("value")


def split_point(bounds, total):
    """
    挑一条切分线：卡片之间的空隙里，最接近总高一半的那个。

    直接按 50/50 切会从某张卡片中间切开，所以只在空隙里挑。
    一个空隙都没有（比如只有一张卡）就退回正中间。
    """
    bounds = sorted(bounds)
    gaps = [(bounds[i][1], bounds[i + 1][0]) for i in range(len(bounds) - 1)
            if bounds[i + 1][0] > bounds[i][1]]
    if not gaps:
        return total / 2
    mid = total / 2
    lo, hi = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - mid))
    return (lo + hi) / 2


def two_column(path, cut_css):
    """整页图切成上下两半，再左右并排。cut_css 是 CSS px，图是 SCALE 倍。"""
    im = Image.open(path).convert("RGB")
    cut = int(cut_css * SCALE)
    left, right = im.crop((0, 0, im.width, cut)), im.crop((0, cut, im.width, im.height))
    gut = GUTTER * SCALE
    out = Image.new("RGB", (im.width * 2 + gut, max(left.height, right.height)), BG)
    out.paste(left, (0, 0))
    out.paste(right, (im.width + gut, 0))
    out.save(path)
    return out.size


def quantize(path):
    """深色 UI 色数很少，量化成调色板 PNG 几乎无损但体积小很多。"""
    im = Image.open(path).convert("RGB")
    im.quantize(colors=192, method=Image.MEDIANCUT, dither=Image.NONE).save(
        path, optimize=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}", "--no-first-run",
         "--remote-allow-origins=*", "--user-data-dir=/tmp/vg-chrome-shots",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)

        only = set(sys.argv[1:])                   # 可只拍指定的几张：python -m tools.gen_screenshots hero.en.png
        for name, trigger, desc, split in SHOTS:
            if only and name not in only:
                continue
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/json/new?{APP}", method="PUT")
            tab = json.load(urllib.request.urlopen(req, timeout=10))
            c = CDP(tab["webSocketDebuggerUrl"])
            c.send("Page.enable")
            c.send("Runtime.enable")
            c.send("Emulation.setDeviceMetricsOverride", width=900, height=1200,
                   deviceScaleFactor=SCALE, mobile=False)
            time.sleep(2.5)                                   # 等首屏脚本跑完
            c.js(trigger)
            c.js("new Promise(r=>setTimeout(r,32000))", await_promise=True)
            c.js("document.getElementById('trace-panel')?.classList.add('open')")
            c.js(KILL_ANIM)
            time.sleep(1.2)

            lvl = c.js("(document.querySelector('.risk-level-code')||{}).textContent")
            sel = CLIP_SELECTOR.get(name, "#result")
            box = c.js(f"""(()=>{{const e=document.querySelector({sel!r});
                if(!e) return null; const r=e.getBoundingClientRect(); const pad=16;
                return {{x:r.left+scrollX-pad, y:r.top+scrollY-pad,
                         width:r.width+pad*2, height:r.height+pad*2}};}})()""")
            shot = c.send("Page.captureScreenshot", format="png",
                          captureBeyondViewport=True,
                          **({"clip": {**box, "scale": 1}} if box else {}))
            dest = OUT / name
            dest.write_bytes(base64.b64decode(shot["data"]))

            if split:
                bounds = c.js(CARD_BOUNDS)
                # 卡片坐标是页面绝对坐标，换算成图内坐标（减去截图区域的起点）
                rel = [[t - box["y"], b - box["y"]] for t, b in bounds]
                size = two_column(dest, split_point(rel, box["height"]))
            else:
                size = Image.open(dest).size

            quantize(dest)
            print(f"  {name:14s} {str(size):14s} {dest.stat().st_size // 1024:4d} KB"
                  f"   [{lvl}]  {desc}")
            c.ws.close()
            urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json/close/{tab['id']}", timeout=10)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
