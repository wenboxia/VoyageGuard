"""
tools/gen_charts.py — 生成 README 里的两张评测结果图（中文 + 英文各一套，英文带 .en 后缀）

数据就是 L4 / L1 的实测数字，写死在下面。改了评测结果就改这里再重跑。

用法：python -m tools.gen_charts

注意：**matplotlib 不在 requirements.txt 里**，只有本地重新出图时才需要
（`pip install matplotlib`）。它进 requirements 会被打进 Vercel 函数包，白白增重，
而线上运行完全不需要画图。
"""

import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                              # noqa: E402
from matplotlib import font_manager                          # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs/images"

# 深色底，和产品界面一致
BG, FG, MUTED = "#0a0e17", "#e8edf5", "#8b96a8"
CYAN, GREEN, AMBER, RED = "#00c8e8", "#00e676", "#f0b429", "#ff5470"

# ── L4：真实停航记录回验（25 条已公布记录，琼州海峡 + 渤海海峡）──────────
# 每项：(中文标签, 英文标签, 数值, 颜色)
L4 = [
    ("误报率\n正常通航日判 HIGH",            "False-alarm rate\nnormal day → HIGH",          0.0,  GREEN),
    ("过度预警率\n正常通航日判 HIGH 或 MEDIUM", "Over-warning rate\nnormal day → HIGH/MEDIUM",  10.0, GREEN),
    ("逐日召回率\n停航日判 HIGH",             "Daily recall\nsuspension day → HIGH",         30.8, AMBER),
    ("事件级召回率\n停航窗口内任一天判 HIGH",   "Event recall\nany day in window → HIGH",      57.1, AMBER),
    ("预警覆盖率\n停航日判 HIGH 或 MEDIUM",    "Warning coverage\nsuspension day → HIGH/MED", 76.9, CYAN),
]

# ── L1：39 条标注用例，工具全 mock ──────────────────────────────────────
L1 = [
    ("deepseek-v4-pro\n（生产）", 89.7, 100.0),   # 英文图里标签换成 (production)
    ("kimi-k3",                   87.2, 100.0),
    ("glm-5",                     71.8, 100.0),
    ("qwen-plus",                 56.4, 100.0),
]


def _cjk():
    """找一个能显示中文的字体，找不到就退回默认（图还能出，只是中文变方块）。"""
    for name in ("PingFang SC", "Hiragino Sans GB", "Heiti SC",
                 "Songti SC", "Arial Unicode MS", "STHeiti"):
        try:
            font_manager.findfont(font_manager.FontProperties(family=name),
                                  fallback_to_default=False)
            return name
        except Exception:
            continue
    return None


def _style():
    f = _cjk()
    if f:
        plt.rcParams["font.family"] = f
    plt.rcParams["axes.unicode_minus"] = False


def _frame(ax):
    ax.set_facecolor(BG)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=MUTED, length=0, labelsize=10)


TEXT = {
    "zh": {
        "l4_title": "L4 真实世界回验 · 25 条已公布的停航/通航记录",
        "l4_note": "误报率 0% = 从没在正常运营的日子里喊过狼来了；召回率的缺口定位出「台风预警」这条轴还没建模",
        "l1_title": "L1 推理层 · 39 条标注用例，工具全 mock",
        "l1_net": "规则引擎安全网之后", "l1_raw": "模型裸判",
        "l1_note": "四家模型裸判从 56.4% 到 89.7%，安全网之后全部 100% —— 判据越细，模型越容易滑一格，确定性 verifier 的价值也就越大",
        "prod": "deepseek-v4-pro\n（生产）",
    },
    "en": {
        "l4_title": "L4 real-world backtest · 25 published service records",
        "l4_note": "0% false alarms on normal-service days; the recall gap points to typhoon warnings, an axis not modelled yet",
        "l1_title": "L1 reasoning · 39 labelled cases, tools mocked",
        "l1_net": "with verifier", "l1_raw": "model alone",
        "l1_note": "Raw accuracy ranges 56.4%–89.7% across four models; every model reaches 100% after the deterministic verifier",
        "prod": "deepseek-v4-pro\n(production)",
    },
}


def _out(name, lang):
    return OUT / (f"{name}.png" if lang == "zh" else f"{name}.en.png")


def chart_l4(lang="zh"):
    T = TEXT[lang]
    fig, ax = plt.subplots(figsize=(9, 4.2), facecolor=BG)
    _frame(ax)
    labels = [(zh if lang == "zh" else en) for zh, en, _, _ in L4][::-1]
    vals = [v for _, _, v, _ in L4][::-1]
    colors = [c for _, _, _, c in L4][::-1]

    bars = ax.barh(labels, vals, color=colors, height=0.55)
    for b, v in zip(bars, vals):
        ax.text(v + 1.5, b.get_y() + b.get_height() / 2, f"{v:g}%",
                va="center", color=FG, fontsize=12, fontweight="bold")
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0", "25%", "50%", "75%", "100%"])
    ax.grid(axis="x", color="#1c2433", linewidth=1)
    ax.set_axisbelow(True)
    ax.set_title(T["l4_title"],
                 color=FG, fontsize=13, pad=16, loc="left")
    fig.text(0.01, 0.02, T["l4_note"], color=MUTED, fontsize=9.5)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(_out("eval-l4", lang), dpi=160, facecolor=BG)
    plt.close(fig)


def chart_l1(lang="zh"):
    T = TEXT[lang]
    fig, ax = plt.subplots(figsize=(9, 4.2), facecolor=BG)
    _frame(ax)
    names = [n for n, _, _ in L1]
    names[0] = T["prod"]
    raw = [r for _, r, _ in L1]
    net = [n for _, _, n in L1]
    y = range(len(names))
    h = 0.34

    ax.barh([i + h / 2 for i in y], net, height=h, color=GREEN, label=T["l1_net"])
    ax.barh([i - h / 2 for i in y], raw, height=h, color=CYAN, label=T["l1_raw"])
    for i, (r, n) in enumerate(zip(raw, net)):
        ax.text(r + 1.5, i - h / 2, f"{r:g}%", va="center", color=FG, fontsize=11)
        ax.text(n + 1.5, i + h / 2, f"{n:g}%", va="center", color=FG,
                fontsize=11, fontweight="bold")
    ax.set_yticks(list(y))
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_xlim(0, 112)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0", "25%", "50%", "75%", "100%"])
    ax.grid(axis="x", color="#1c2433", linewidth=1)
    ax.set_axisbelow(True)
    ax.set_title(T["l1_title"],
                 color=FG, fontsize=13, pad=16, loc="left")
    # 图例放到绘图区上方，压在条上会挡住最后一行
    lg = ax.legend(loc="lower left", bbox_to_anchor=(0.62, 1.0),
                   ncol=2, frameon=False, fontsize=10)
    for t in lg.get_texts():
        t.set_color(MUTED)
    fig.text(0.01, 0.02, T["l1_note"], color=MUTED, fontsize=9.5)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(_out("eval-l1", lang), dpi=160, facecolor=BG)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _style()
    for lang in ("zh", "en"):
        chart_l4(lang)
        chart_l1(lang)
    for f in ("eval-l4.png", "eval-l1.png", "eval-l4.en.png", "eval-l1.en.png"):
        print(f"  {f}  {(OUT / f).stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
