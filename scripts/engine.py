#!/usr/bin/env python3
"""Mercury Phase 2 — 预报引擎：集合聚合 + 偏差校准 + 离散桶概率

将多模型逐时温度曲线聚合为日最高温预测，输出连续分布和离散桶概率。

用法:
    python3 scripts/engine.py --city beijing --date 2026-06-01
    python3 scripts/engine.py --city beijing --date 2026-06-01 --bias
    python3 scripts/engine.py --all --date 2026-06-01
"""

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
MAE_SEED_PATH = PROJECT_ROOT / "data" / "models" / "mae_seed.json"


def load_config():
    with open(PROJECT_ROOT / "config" / "cities.yaml") as f:
        return yaml.safe_load(f)


def load_forecast(city_key: str, target_date: str) -> dict | None:
    """加载指定城市+日期的预报快照（latest）"""
    path = PROJECT_ROOT / "data" / "forecasts" / city_key / target_date / "latest.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def load_bias(city_key: str) -> dict | None:
    """加载城市偏差校准参数"""
    path = PROJECT_ROOT / "data" / "models" / f"{city_key}_bias.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def load_metar(city_key: str, target_date: str) -> dict | None:
    """加载 METAR 实测数据"""
    path = PROJECT_ROOT / "data" / "metar" / city_key / f"{target_date}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def load_mae_weights(city_key: str) -> tuple[dict[str, float], float]:
    """加载逐模型 MAE 权重和偏差校准值。
    
    Returns:
        (weights, bias) — weights 是 {model: weight}, bias 是累积偏差
        无数据时返回 ({}, 0.0)
    """
    if not MAE_SEED_PATH.exists():
        return {}, 0.0
    
    with open(MAE_SEED_PATH) as f:
        seed = json.load(f)
    
    cities = seed.get("cities", {})
    bias_data = seed.get("bias", {})
    
    mae_dict = cities.get(city_key, {})
    bias = bias_data.get(city_key, 0.0)
    
    if not mae_dict:
        return {}, bias
    
    # w_i = 1/MAE_i，然后归一化
    inv_mae = {}
    for model, mae in mae_dict.items():
        if mae > 0:
            inv_mae[model] = 1.0 / mae
    
    total = sum(inv_mae.values())
    if total == 0:
        return {}, bias
    
    weights = {m: v / total for m, v in inv_mae.items()}
    return weights, bias


def normal_cdf(x: float) -> float:
    """标准正态分布 CDF（Abramowitz and Stegun 近似）"""
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    # 使用 math.erf
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob_normal(t_ensemble: float, t_std: float, bucket: int) -> float:
    """正态分布近似：温度落入离散桶 [bucket-0.5, bucket+0.5) 的概率"""
    if t_std <= 0.01:
        # 确定性情况
        return 1.0 if abs(t_ensemble - bucket) < 0.5 else 0.0
    upper = normal_cdf((bucket + 0.5 - t_ensemble) / t_std)
    lower = normal_cdf((bucket - 0.5 - t_ensemble) / t_std)
    return max(0.0, upper - lower)


def bucket_prob_voting(model_tmax: dict[str, float]) -> dict[int, float]:
    """集合成员投票法：每个模型投一票给它的最接近整数桶"""
    votes = {}
    for m, t in model_tmax.items():
        bucket = round(t)
        votes[bucket] = votes.get(bucket, 0)
    # 归一化
    total = len(model_tmax)
    return {b: c / total for b, c in votes.items()}


def build_bucket_range(t_values: list[float], min_buckets: int = 5) -> list[int]:
    """根据温度范围构建离散桶列表"""
    t_min = min(t_values)
    t_max = max(t_values)
    center = round(sum(t_values) / len(t_values))

    # 扩展范围，确保覆盖
    low = min(int(t_min) - 2, center - min_buckets)
    high = max(int(t_max) + 2, center + min_buckets)
    return list(range(low, high + 1))


def compute_engine(city_key: str, target_date: str, use_deb: bool = False) -> dict | None:
    """核心预报引擎：加载数据 → 聚合 → 离散桶概率
    
    Args:
        city_key: 城市标识
        target_date: 目标日期
        use_deb: 启用 DEB (Dynamic Ensemble Blending) — MAE加权+偏差校准
    """
    fc = load_forecast(city_key, target_date)
    if fc is None:
        return None

    t_max_models = fc.get("t_max", {})
    models_available = fc.get("models_available", [])
    t_ensemble = fc.get("t_ensemble")

    if not t_max_models or t_ensemble is None:
        return None

    # ── 加载 MAE 权重 ──
    mae_weights = {}
    bias_correction = 0.0
    bias_source = None
    
    if use_deb:
        mae_weights, bias_correction = load_mae_weights(city_key)
        if mae_weights:
            bias_source = f"polywx-bootstrap (bias={bias_correction:+.2f}°C)"
    
    # ── 加权均值 vs 等权均值 ──
    t_values = list(t_max_models.values())
    n = len(t_values)
    blend_method = "equal"
    
    if mae_weights and len(mae_weights) >= 2:
        # MAE 加权均值
        weighted_sum = 0.0
        weight_total = 0.0
        for m, t in t_max_models.items():
            w = mae_weights.get(m, 0.0)
            if w > 0:
                weighted_sum += w * t
                weight_total += w
        
        if weight_total > 0:
            t_mean = round(weighted_sum / weight_total, 1)
            
            # 加权标准差
            variance = 0.0
            for m, t in t_max_models.items():
                w = mae_weights.get(m, 0.0)
                if w > 0:
                    variance += w * (t - t_mean) ** 2
            t_std = round(math.sqrt(variance / weight_total), 2)
            blend_method = "deb_mae_weighted"
        else:
            t_mean = t_ensemble
            t_std = compute_std(t_values)
    else:
        t_mean = t_ensemble
        t_std = compute_std(t_values)
    
    t_range = (min(t_values), max(t_values))
    
    # ── 偏差校准 ──
    t_calibrated = round(t_mean - bias_correction, 1)
    
    # ── 离散桶概率 ──
    buckets = build_bucket_range(t_values + [t_calibrated])
    probs_normal = {}
    for b in buckets:
        p = bucket_prob_normal(t_calibrated, t_std if t_std > 0.01 else 1.0, b)
        if p > 0.001:
            probs_normal[b] = round(p, 4)

    probs_voting = bucket_prob_voting(t_max_models)
    
    # ── 加权投票（MAE 加权） ──
    probs_weighted_voting = None
    if mae_weights:
        probs_weighted_voting = bucket_prob_weighted_voting(t_max_models, mae_weights)

    # ── 模型明细 ──
    model_detail = {}
    for m in models_available:
        t = t_max_models.get(m)
        if t is not None:
            w = mae_weights.get(m, 1.0 / n if n > 0 else 0)
            model_detail[m] = {
                "t_max": t,
                "deviation": round(t - t_mean, 1),
                "bucket": round(t),
                "weight": round(w, 3),
            }

    # ── METAR 实测（如有） ──
    metar_actual = None
    metar = load_metar(city_key, target_date)
    if metar and metar.get("t_max") is not None:
        metar_actual = metar["t_max"]
        forecast_error = round(t_calibrated - metar_actual, 1)
    else:
        forecast_error = None

    return {
        "city": city_key,
        "target_date": target_date,
        "generated_at": fc.get("generated_at"),
        "generated_date": fc.get("generated_date"),
        "n_models": n,
        "models_available": models_available,
        "blend_method": blend_method,
        # 连续分布
        "t_ensemble": t_mean,           # 加权均值（DEB）或等权均值
        "t_std": t_std,
        "t_range": list(t_range),
        "t_calibrated": t_calibrated,   # 偏差校准后
        "bias_correction": round(bias_correction, 2),
        "bias_source": bias_source,
        # 离散桶概率
        "buckets_normal": probs_normal,
        "buckets_voting": probs_voting,
        "buckets_weighted_voting": probs_weighted_voting,
        "buckets_range": buckets,
        # 模型明细
        "models": model_detail,
        # 验证
        "metar_t_max": metar_actual,
        "forecast_error": forecast_error,
    }


def compute_std(t_values: list[float]) -> float:
    """计算等权标准差"""
    n = len(t_values)
    if n < 2:
        return 0.0
    mean = sum(t_values) / n
    variance = sum((v - mean) ** 2 for v in t_values) / (n - 1)
    return round(math.sqrt(variance), 2)


def bucket_prob_weighted_voting(model_tmax: dict[str, float], weights: dict[str, float]) -> dict[int, float]:
    """加权投票法：每个模型按其权重投票给最接近的整数桶"""
    votes = {}
    weight_total = 0.0
    for m, t in model_tmax.items():
        w = weights.get(m, 0.0)
        if w <= 0:
            continue
        bucket = round(t)
        votes[bucket] = votes.get(bucket, 0.0) + w
        weight_total += w
    
    if weight_total == 0:
        return {}
    return {b: round(c / weight_total, 4) for b, c in votes.items()}


def save_engine_output(city_key: str, target_date: str, output: dict):
    """保存引擎输出"""
    out_dir = PROJECT_ROOT / "data" / "engine" / city_key
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{target_date}.json"
    with open(path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Mercury 预报引擎")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key")
    group.add_argument("--all", action="store_true", help="所有城市")
    parser.add_argument("--date", type=str, required=True, help="目标日期 YYYY-MM-DD")
    parser.add_argument("--deb", action="store_true", help="启用 DEB: MAE加权融合 + 偏差校准")
    parser.add_argument("--save", action="store_true", help="保存结果到文件")
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
        display = config["cities"][city_key].get("display_name", city_key)
        output = compute_engine(city_key, args.date, use_deb=args.deb)

        if output is None:
            print(f"  {display:12s} ❌ 无预报数据")
            continue

        if args.save:
            save_engine_output(city_key, args.date, output)

        # 紧凑输出
        method = "DEB" if output["blend_method"] == "deb_mae_weighted" else "EQ"
        err = f" err={output['forecast_error']:+}°C" if output['forecast_error'] is not None else ""
        bias_str = f" bias={output['bias_correction']:+.1f}" if args.deb else ""
        top_buckets = sorted(output["buckets_normal"].items(), key=lambda x: -x[1])[:3]
        bucket_str = " ".join(f"{b}°C:{p:.0%}" for b, p in top_buckets)

        print(f"  {display:12s} [{method}] ens={output['t_ensemble']}°C±{output['t_std']} "
              f"cal={output['t_calibrated']}°C{bias_str}  top3:[{bucket_str}]{err}")

        results.append(output)

    if not results:
        print("⚠️ 没有城市有预报数据。请先运行 fetch_openmeteo.py --all")
        sys.exit(1)

    # 汇总
    if args.all and results:
        with_err = [r for r in results if r["forecast_error"] is not None]
        if with_err:
            errors = [r["forecast_error"] for r in with_err]
            mae = sum(abs(e) for e in errors) / len(errors)
            print(f"\n  共 {len(results)} 城, {len(with_err)} 城有实测")
            print(f"  MAE={mae:.1f}°C, 偏差={sum(errors)/len(errors):+.1f}°C")


if __name__ == "__main__":
    main()
