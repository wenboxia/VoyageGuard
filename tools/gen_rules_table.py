"""
tools/gen_rules_table.py — 从 rules_sources.py 生成 README 的「规则来源表」

存在的理由：上一版 README 写的阈值和代码里的不一致，而且文档里说的"规则来源"
其实是项目自己的 PRD（等于自己引用自己）。表由代码生成就不会再漂移。

用法：python -m tools.gen_rules_table   然后把输出粘进 README 对应小节
"""

import rules_sources as S


def bft(ms: float) -> str:
    return f"{ms} m/s（{S.beaufort(ms)} 级）"


def main() -> None:
    print("| 判据 | 阈值 | 触发等级 | 来源类型 | 出处 |")
    print("|---|---|---|---|---|")

    print(f"| 小船禁止出海 | 海面风力 {bft(S.BAN_WIND_MS['small'])} | HIGH | `REG` 法规 "
          f"| [{S.SOURCES['small_craft_ban'].name_zh}]({S.SOURCES['small_craft_ban'].url}) |")
    print(f"| 大船不得允许旅客/车辆上船 | 海面风力 {bft(S.BAN_WIND_MS['large'])} | HIGH | `REG` 法规 "
          f"| [{S.SOURCES['passenger_boarding_ban'].name_zh}]({S.SOURCES['passenger_boarding_ban'].url}) |")
    print(f"| 航空 低能见度起飞门槛 | RVR < {S.LVTO_RVR_KM} km | HIGH | `REG` 规章 "
          f"| [{S.SOURCES['lvto'].name_zh}]({S.SOURCES['lvto'].url}) |")

    src = S.SOURCES["gale_warning"]
    for lv in S.WARNING_ORDER:
        thr = S.GALE_WARNING[lv]
        zh, _ = S.WARNING_LABEL[lv]
        level = "MEDIUM" if lv == "blue" else "HIGH"
        print(f"| 大风{zh}预警 | 平均风 ≥{bft(thr['mean_ms'])} **或** 阵风 ≥{bft(thr['gust_ms'])} "
              f"| {level} | `WARN` 官方预警 | [{src.name_zh}]({src.url}) |")

    src = S.SOURCES["wave_warning"]
    for lv in S.WARNING_ORDER:
        zh, _ = S.WARNING_LABEL[lv]
        level = "MEDIUM（大船）/ HIGH（小船，见下）" if lv == "blue" else "HIGH"
        print(f"| 近岸海浪{zh}预警 | 有效波高 ≥{S.WAVE_WARNING[lv]} m | {level} "
              f"| `WARN` 官方预警 | [{src.name_zh}]({src.url}) |")
    print(f"| 灾害性海浪 | 有效波高 > {S.DISASTROUS_WAVE_M} m | HIGH | `WARN` 官方定义 "
          f"| [{src.name_zh}]({src.url}) |")

    p = S.PROD_RULES["small_craft_wave"]
    print(f"| 小船遇海浪蓝色预警 | 有效波高 ≥{p['value']} m | HIGH | **`PROD` 本工具判定** | — |")
    p = S.PROD_RULES["aviation_wind"]
    print(f"| 航空 接近窄体机侧风限制 | 平均风 ≥{bft(p['value'])} | HIGH | **`PROD` 本工具判定** "
          f"| [{S.SOURCES['crosswind'].name_zh}]({S.SOURCES['crosswind'].url}) |")
    print(f"| 航空 雷暴 | 天气描述含雷暴 | MEDIUM | **`PROD` 本工具判定** | — |")

    print("\n### 两条 `PROD` 规则的理由\n")
    print(f"**小船遇海浪蓝色预警 → HIGH**（官方只给了分船型的风速禁航线，没给分船型的浪高线）\n")
    print(f"> {S.PROD_RULES['small_craft_wave']['reason_zh']}\n")
    print(f"**航空 平均风 ≥15 m/s → HIGH**\n")
    print(f"> {S.PROD_RULES['aviation_wind']['reason_zh']}\n")
    print(f"**航空雷暴 → MEDIUM**\n")
    print(f"> {S.THUNDER_NOTE_ZH}\n")

    print("### 每条来源的已知局限\n")
    for k in ("small_craft_ban", "passenger_boarding_ban", "gale_warning", "wave_warning",
              "lvto", "crosswind", "yangtze_ban", "mot_reply_6750"):
        s = S.SOURCES[k]
        print(f"- **[{s.name_zh}]({s.url})** — {s.caveat_zh}")


if __name__ == "__main__":
    main()
