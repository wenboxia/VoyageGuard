"""
tools/gen_banner.py — 把 tools/banner.html 渲染成 README 横幅 docs/images/banner.png

用法：python -m tools.gen_banner

横幅 1600×380，按 2x 输出 3200×760（与同作者另外两个仓库的横幅同尺寸）。
依赖本机 Google Chrome，不进 requirements.txt——只在本地重新出图时需要。
横幅右侧的四个数字取自 docs/evaluation.md，改了评测数字要同步改 banner.html。
"""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SRC = ROOT / "tools/banner.html"
OUT = ROOT / "docs/images/banner.png"


def main() -> None:
    subprocess.run([
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2", "--window-size=1600,380",
        "--default-background-color=0a0e17ff",
        f"--screenshot={OUT}", f"file://{SRC}",
    ], check=True, capture_output=True)
    print(f"  {OUT.relative_to(ROOT)}  {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
