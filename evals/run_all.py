"""
evals/run_all.py — 按代价从低到高依次跑三层，前一层失败就停。

  L3  确定性断言，零 token       —— 改动之后的第一道检查
  L2  真实网络 + 真实模型调用     —— 端到端结论是否成立且自洽
  L1  全 mock 模型横评           —— 最贵，只在需要重新选型时跑

运行：python -m evals.run_all [--with-l1]
"""

import sys


def main(argv):
    from evals import l2_integration, l3_sufficiency

    print("\n▶ L3 · 数据充分性层")
    if l3_sufficiency.main() != 0:
        print("\n✗ L3 未通过 —— 数据管线本身不成立，后面两层的结果没有意义。停止。")
        return 1

    print("\n▶ L2 · 集成层")
    if l2_integration.main() != 0:
        print("\n✗ L2 未通过 —— 端到端链路有问题。停止。")
        return 1

    if "--with-l1" in argv:
        print("\n▶ L1 · 推理层（模型横评）")
        from evals import l1_reasoning
        return l1_reasoning.main([])

    print("\n✓ L3 + L2 全部通过。模型横评用 python -m evals.run_all --with-l1")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
