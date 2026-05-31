#!/usr/bin/env python3
"""Mercury Phase 2 — 准确率追踪：对比预报 vs METAR 实测

用法:
    python3 scripts/evaluate.py --city beijing
    python3 scripts/evaluate.py --all
    python3 scripts/evaluate.py --city beijing --window 30
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent


def load_config():
    with open(PROJECT_ROOT / "config" / "cities.yaml") as f:
        return yaml.safe_load(f)


def find_evaluable_dates(city_key: str) -> list[tuple[str, float, float]]:
    """找到所有同时有预报和实测的日期，返回 [(date, forecast, actual), ...]"""
    forecast_dir = PROJECT_ROOT / "data" / "forecasts" / city_key
    metar_dir = PROJECT_ROOT / "data" / "metar" / city_key

    if not forecast_dir.exists() or not metar_dir.exists():
        return []

    pairs = []
    for fc_date_dir in sorted(forecast_dir.iterdir()):
        if not fc_date_dir.is_dir():
            continue
        target_date = fc_date_dir.name
        fc_file = fc_date_dir / "latest.json"
        metar_file = metar_dir / f"{target_date}.json"

        if fc_file.exists() and metar_file.exists():
            with open(fc_file) as f:
                fc = json.load(f)
            with open(metar_file) as f:
                metar = json.load(f)

            t_ensemble = fc.get("t_ensemble")
            t_actual = metar.get("t_max")

            if t_ensemble is not None and t_actual is not None:
                pairs.append((target_date, t_ensemble, t_actual))

    return pairs


def evaluate_city(city_key: str, window: int = None) -> dict | None:
    """评估单个城市的预报准确率"""
    pairs = find_evaluable_dates(city_key)

    if not pairs:
        return None

    # 如果指定了窗口，只取最近 N 天
    if window and len(pairs) > window:
        pairs = pairs[-window:]

    errors = [fc - act for _, fc, act in pairs]
    abs_errors = [abs(e) for e in errors]
    n = len(pairs)

    mae = round(sum(abs_errors) / n, 2)
    bias = round(sum(errors) / n, 2)
    rmse = round((sum(e ** 2 for e in errors) / n) ** 0.5, 2)

    # ±1°C, ±2°C, ±3°C 命中率
    hit_1 = sum(1 for e in abs_errors if e <= 1.0) / n
    hit_2 = sum(1 for e in abs_errors if e <= 2.0) / n
    hit_3 = sum(1 for e in abs_errors if e <= 3.0) / n

    # 偏差方向
    overestimate = sum(1 for e in errors if e > 0)  # 预报偏高
    underestimate = sum(1 for e in errors if e < 0)  # 预报偏低
    exact = sum(1 for e in errors if e == 0)

    # 日误差明细（最近10天）
    daily = []
    for target_date, fc, act in pairs[-10:]:
        daily.append({
            "date": target_date,
            "forecast": fc,
            "actual": act,
            "error": round(fc - act, 1),
        })

    return {
        "city": city_key,
        "n_days": n,
        "date_range": [pairs[0][0], pairs[-1][0]],
        "mae": mae,
        "bias": bias,
        "rmse": rmse,
        "hit_rate_1c": round(hit_1, 3),
        "hit_rate_2c": round(hit_2, 3),
        "hit_rate_3c": round(hit_3, 3),
        "overestimate_pct": round(overestimate / n, 3) if n > 0 else 0,
        "underestimate_pct": round(underestimate / n, 3) if n > 0 else 0,
        "exact_pct": round(exact / n, 3) if n > 0 else 0,
        "daily": daily,
    }


def main():
    parser = argparse.ArgumentParser(description="Mercury 准确率评估")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key")
    group.add_argument("--all", action="store_true", help="所有城市")
    parser.add_argument("--window", type=int, default=None, help="滑动窗口天数（默认全部）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    config = load_config()

    if args.city:
        if args.city not in config["cities"]:
            print(f"❌ 未知城市: {args.city}")
            sys.exit(1)
        targets = [args.city]
    else:
        targets = list(config["cities"].keys())

    results = []
    for city_key in targets:
        r = evaluate_city(city_key, window=args.window)
        if r:
            results.append(r)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if not results:
        print("⚠️ 没有可评估的数据。请先运行 fetch_openmeteo.py 和 fetch_metar.py")
        return

    # 表格输出
    header = f"{'城市':12s} {'天数':>4s} {'MAE':>6s} {'Bias':>6s} {'±1°C':>6s} {'±2°C':>6s} {'高估':>6s} {'低估':>6s}"
    print(header)
    print("-" * len(header))

    # 按 MAE 排序
    results.sort(key=lambda r: r["mae"])

    for r in results:
        display = config["cities"][r["city"]].get("display_name", r["city"])
        print(f"{display:12s} {r['n_days']:>4d} {r['mae']:>5.1f}°C {r['bias']:>+5.1f}°C "
              f"{r['hit_rate_1c']:>5.0%} {r['hit_rate_2c']:>5.0%} "
              f"{r['overestimate_pct']:>5.0%} {r['underestimate_pct']:>5.0%}")

    # 汇总
    if len(results) > 1:
        avg_mae = sum(r["mae"] for r in results) / len(results)
        avg_hit1 = sum(r["hit_rate_1c"] for r in results) / len(results)
        avg_hit2 = sum(r["hit_rate_2c"] for r in results) / len(results)
        total_days = sum(r["n_days"] for r in results)
        print(f"\n{'汇总':12s} {total_days:>4d} {avg_mae:>5.1f}°C          "
              f"{avg_hit1:>5.0%} {avg_hit2:>5.0%}  ({len(results)}城)")


if __name__ == "__main__":
    main()
