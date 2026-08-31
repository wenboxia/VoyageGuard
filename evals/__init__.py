"""VoyageGuard 三层评测。

  L1 推理层  evals/l1_reasoning.py    全 mock，隔离网络，测 LLM 判断质量        花 token
  L2 集成层  evals/l2_integration.py  真实工具调用，测端到端结论成立与自洽      花 token + 网络
  L3 充分性  evals/l3_sufficiency.py  确定性断言，测该拿的数据到底拿到没有      零 token

为什么不能互相替代：L1 可以 100% 而 L3 是 0%——这正是本项目发生过的事。
浪高曾经是死代码（从天气描述文本里正则抠中文），而 L1 的 mock 恰好把
"有效浪高 3.0 米" 写进了描述串，于是 20 条涉浪用例全过。
"""
