"""
tools/gen_graphics.py — 把 tools/graphics/ 下的 HTML 渲染成 README 用的图

用法：python -m tools.gen_graphics            # 全部
      python -m tools.gen_graphics architecture.png   # 只渲染指定的

  banner.png             README 横幅，1600×380，2x 输出（与同作者另外两个仓库的横幅同尺寸）
  architecture.png       架构图（中文），1600×570，2x 输出（英文文字更长，画布 1600×600）
  architecture.en.png    架构图（英文），同一份 HTML 加 ?lang=en

依赖本机 Google Chrome，不进 requirements.txt——只在本地重新出图时需要。
字体（Outfit / IBM Plex Mono）从 Google Fonts 加载，与线上页面一致，所以渲染时需要联网。
横幅右侧的四个数字取自 docs/evaluation.md，改了评测数字要同步改 graphics/banner.html。
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SRC = ROOT / "tools/graphics"
OUT = ROOT / "docs/images"

# (输出文件名, 源 HTML, 查询串, 宽, 高)
TARGETS = [
    ("banner.png", "banner.html", "", 1600, 380),
    ("architecture.png", "architecture.html", "?lang=zh", 1600, 570),
    ("architecture.en.png", "architecture.html", "?lang=en", 1600, 600),
]


def render(name: str, src: str, query: str, w: int, h: int) -> None:
    out = OUT / name
    subprocess.run([
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2", f"--window-size={w},{h}",
        "--default-background-color=060a12ff", "--virtual-time-budget=8000",
        f"--screenshot={out}", f"file://{SRC / src}{query}",
    ], check=True, capture_output=True)
    print(f"  {out.relative_to(ROOT)}  {out.stat().st_size // 1024} KB")


def main() -> None:
    only = set(sys.argv[1:])
    for t in TARGETS:
        if not only or t[0] in only:
            render(*t)


if __name__ == "__main__":
    main()
