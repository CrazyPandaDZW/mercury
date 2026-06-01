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
    
    # 权重公式: w_i ∝ 1/MAE_i² (平方倒数，强偏好低误差模型)
    # ICON 主导策略: 确保 ICON 最低占 50% 权重
    ICON_MIN_WEIGHT = 0.0  # 不设最低保障，完全由数据决定
    POWER = 2.0  # 1/MAE^p, p=2 比 p=1 更激进地偏向低 MAE 模型
    
    inv_mae = {}
    for model, mae in mae_dict.items():
        if model.startswith("_"):  # 跳过元数据字段
            continue
        if mae > 0:
            inv_mae[model] = 1.0 / (mae ** POWER)
    
    total_raw = sum(inv_mae.values())
    if total_raw == 0:
        return {}, bias
    
    # 先算原始权重
    raw_weights = {m: v / total_raw for m, v in inv_mae.items()}
    
    # ICON 最低权重保障
    icon_w = raw_weights.get("icon_seamless", 0.0)
    if icon_w < ICON_MIN_WEIGHT:
        # 把 ICON 提到 50%，其他模型等比压缩
        scale = (1.0 - ICON_MIN_WEIGHT) / max(0.001, 1.0 - icon_w)
        weights = {}
        for m, w in raw_weights.items():
            if m == "icon_seamless":
                weights[m] = ICON_MIN_WEIGHT
            else:
                weights[m] = w * scale
    else:
        weights = raw_weights
    
    return weights, bias


def load_own_bias(city_key: str, target_date: str, max_days: int = 7) -> tuple[float, int]:
    """从自有引擎历史输出中读取累积偏差。
    
    读取 target_date 之前最多 max_days 天的结算误差，取均值作为偏差。
    样本不足时不强行修正。
    
    Returns:
        (bias, n_days) — bias 为平均预报误差(正=预报偏高)，n_days 为样本天数
    """
    engine_dir = PROJECT_ROOT / "data" / "engine" / city_key
    if not engine_dir.exists():
        return 0.0, 0
    
    errors = []
    for fpath in sorted(engine_dir.glob("*.json")):
        fdate = fpath.stem  # "2026-05-31"
        if fdate >= target_date:
            continue
        try:
            with open(fpath) as f:
                d = json.load(f)
            err = d.get("forecast_error")
            if err is not None:
                errors.append(err)
        except (json.JSONDecodeError, KeyError):
            continue
    
    if len(errors) < 2:
        return 0.0, len(errors)
    
    # 取最近 max_days 天的均值
    recent = errors[-max_days:]
    bias = round(sum(recent) / len(recent), 2)
    return bias, len(recent)


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


def _interpolate_temp(hourly: list, hour_frac: float, model: str) -> float | None:
    """线性插值：逐时曲线中指定模型在 hour_frac 时刻的温度"""
    h0 = int(hour_frac)
    h1 = h0 + 1
    frac = hour_frac - h0

    t0 = t1 = None
    for entry in hourly:
        if entry["hour"] == h0:
            t0 = entry["temps"].get(model)
        if entry["hour"] == h1:
            t1 = entry["temps"].get(model)
        if t0 is not None and t1 is not None:
            break

    if t0 is None and t1 is None:
        return None
    if t0 is None:
        return t1
    if t1 is None:
        return t0
    return t0 + (t1 - t0) * frac


def _parse_metar_local_hour(time_utc: str, tz_str: str) -> float | None:
    """UTC 时间 → 本地 hour_frac"""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(time_utc, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("UTC"))
        local = dt.astimezone(ZoneInfo(tz_str))
        return local.hour + local.minute / 60.0
    except Exception:
        return None


def compute_live_weights(hourly: list, metar_records: list, 
                          model_keys: list, tz_str: str,
                          ref_time: datetime | None = None) -> tuple[dict | None, dict | None, int]:
    """基于时间衰减加权 RMSE 计算各模型实时权重。

    每个 METAR 点的贡献按 e^(-λ·Δt) 衰减，半衰期 6 小时，窗口 24 小时。
    无需硬阈值——前一天数据低权重补足，平滑过渡。

    Args:
        ref_time: 参考时间（默认当前 UTC），用于计算 Δt
    
    Returns:
        (weights, rmse, n_points) — weights=None 表示数据不足
    """
    HALF_LIFE = 4.0         # 半衰期（小时）— 重点当天，昨日轻量平滑
    WINDOW_HOURS = 24.0     # 最大回溯窗口
    LAMBDA = math.log(2) / HALF_LIFE
    
    if ref_time is None:
        ref_time = datetime.now(timezone.utc)
    
    # 提取 METAR 实测点，计算时间衰减权重
    metar_pts = []  # [(hour_frac, temp_c, decay_weight, delta_hours)]
    total_raw = 0
    
    for r in metar_records:
        if r.get("temp_c") is None:
            continue
        utc = r.get("time_utc", "")
        h_frac = _parse_metar_local_hour(utc, tz_str)
        if h_frac is None:
            continue
        
        # 计算时间距离
        try:
            dt_utc = datetime.strptime(utc, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        delta_h = (ref_time - dt_utc).total_seconds() / 3600.0
        if delta_h < 0 or delta_h > WINDOW_HOURS:
            continue  # 跳过未来数据或超过窗口
        
        w = math.exp(-LAMBDA * delta_h)
        metar_pts.append((h_frac, r["temp_c"], w, delta_h))
        total_raw += 1
    
    if len(metar_pts) < 3:  # 3个点即够（含前一天低权重数据）
        return None, None, len(metar_pts)
    
    # 时间衰减加权 RMSE
    model_rmse = {}
    for m in model_keys:
        weighted_sq_err = 0.0
        weight_sum = 0.0
        for h_frac, actual, w, _ in metar_pts:
            pred = _interpolate_temp(hourly, h_frac, m)
            if pred is not None:
                weighted_sq_err += w * (pred - actual) ** 2
                weight_sum += w
        
        if weight_sum > 0:
            model_rmse[m] = round(math.sqrt(weighted_sq_err / weight_sum), 2)
    
    if len(model_rmse) < 2:
        return None, None, len(metar_pts)
    
    # 1/RMSE² → 归一化权重
    inv = {m: 1.0 / max(r ** 2, 0.01) for m, r in model_rmse.items()}
    total = sum(inv.values())
    weights = {m: round(v / total, 4) for m, v in inv.items()}
    
    return weights, model_rmse, len(metar_pts)


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
    hourly = fc.get("hourly", [])   # 逐时多模型预报曲线

    if not t_max_models or t_ensemble is None:
        return None

    # ── 加载 METAR（当天 + 前一天，用于时间衰减加权） ──
    metar = load_metar(city_key, target_date)
    metar_records = list(metar.get("records", [])) if metar else []

    # 前一天 METAR
    from datetime import timedelta
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_metar = load_metar(city_key, prev_date)
    if prev_metar:
        metar_records.extend(prev_metar.get("records", []))

    # ── 实时权重（时间衰减加权 RMSE，半衰 6h，窗口 24h） ──
    live_weights = None
    live_rmse = {}
    live_n_points = 0

    if metar_records:
        if hourly:
            city_cfg = load_config().get("cities", {}).get(city_key, {})
            tz = city_cfg.get("tz", "UTC")
            live_weights, live_rmse, live_n_points = compute_live_weights(
                hourly, metar_records, models_available, tz
            )

    # ── 权重选择：实时 > MAE > 等权 ──
    active_weights = {}
    bias_correction = 0.0
    bias_source = None
    blend_method = "equal"

    if live_weights and len(live_weights) >= 2:
        active_weights = live_weights
        blend_method = "live_rmse_weighted"
        # 使用自有历史偏差（前N天结算误差均值）
        if use_deb:
            own_bias, own_n = load_own_bias(city_key, target_date)
            if own_n >= 2:
                bias_correction = own_bias
                bias_source = f"own-track (n={own_n}d, bias={bias_correction:+.2f}°C)"
            else:
                # 自有数据不足，不校准（宁可无偏不用错误偏差）
                bias_correction = 0.0
                bias_source = f"own-track (n={own_n}d, insufficient)"
    elif use_deb:
        mae_weights, bias_correction = load_mae_weights(city_key)
        active_weights = mae_weights
        if active_weights and len(active_weights) >= 2:
            blend_method = "deb_mae_weighted"
            bias_source = f"polywx-bootstrap (bias={bias_correction:+.2f}°C)"

    # ── 加权均值 ──
    t_values = list(t_max_models.values())
    n = len(t_values)

    if active_weights and len(active_weights) >= 2:
        weighted_sum = 0.0
        weight_total = 0.0
        for m, t in t_max_models.items():
            w = active_weights.get(m, 0.0)
            if w > 0:
                weighted_sum += w * t
                weight_total += w

        if weight_total > 0:
            t_mean = round(weighted_sum / weight_total, 1)
            # 加权标准差
            variance = 0.0
            for m, t in t_max_models.items():
                w = active_weights.get(m, 0.0)
                if w > 0:
                    variance += w * (t - t_mean) ** 2
            t_std = round(math.sqrt(variance / weight_total), 2)
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

    # ── 加权投票 ──
    probs_weighted_voting = None
    if active_weights:
        probs_weighted_voting = bucket_prob_weighted_voting(t_max_models, active_weights)

    # ── 模型明细 ──
    model_detail = {}
    for m in models_available:
        t = t_max_models.get(m)
        if t is not None:
            w = active_weights.get(m, 1.0 / n if n > 0 else 0)
            entry = {
                "t_max": t,
                "deviation": round(t - t_mean, 1),
                "bucket": round(t),
                "weight": round(w, 3),
            }
            if live_rmse and m in live_rmse:
                entry["live_rmse"] = live_rmse[m]
            model_detail[m] = entry

    # ── L1 融合曲线快照（权重加权，逐时，冻结用） ──
    l1_curve = {}
    if active_weights and hourly:
        for h_data in hourly:
            h = h_data.get("hour")
            temps = h_data.get("temps", {})
            if h is None:
                continue
            w_sum = 0.0
            w_total = 0.0
            for m, w in active_weights.items():
                t = temps.get(m)
                if t is not None and w > 0:
                    w_sum += t * w
                    w_total += w
            if w_total > 0:
                l1_curve[str(h)] = round(w_sum / w_total, 1)

    # 引擎运行时当地小时（用于判断哪些小时该冻结）
    snapshot_hour = None
    if fc.get("generated_at"):
        try:
            gen_dt = datetime.fromisoformat(fc["generated_at"])
            from zoneinfo import ZoneInfo
            city_cfg = load_config().get("cities", {}).get(city_key, {})
            tz_name = city_cfg.get("tz", "UTC")
            local_dt = gen_dt.astimezone(ZoneInfo(tz_name))
            snapshot_hour = local_dt.hour
        except Exception:
            pass

    # ── METAR 实测 ──
    metar_actual = None
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
        # 实时权重信息
        "live_weights": live_weights is not None,
        "live_n_points": live_n_points,
        "live_rmse_summary": live_rmse if live_rmse else None,
        # L1 融合曲线快照（逐时冻结用）
        "l1_curve": l1_curve,
        "snapshot_hour": snapshot_hour,
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
        method_map = {"live_rmse_weighted": "LIVE", "deb_mae_weighted": "DEB"}
        method = method_map.get(output["blend_method"], "EQ")
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


def snapshot_prediction(city_key: str, target_date: str) -> dict | None:
    """基于最新 METAR 计算预测快照并记录到 snapshots.jsonl。
    
    每次新 METAR 数据写入后调用，桶变化或距上次≥30分钟时记录。
    
    Returns:
        快照 dict（含 t_predicted, bucket 等），或 None（数据不足时）
    """
    from datetime import datetime as dt_mod
    from datetime import timezone as tz_mod
    from zoneinfo import ZoneInfo

    
    # 加载 forecast
    fc = load_forecast(city_key, target_date)
    if fc is None:
        return None
    hourly = fc.get("hourly", [])
    model_keys = fc.get("models_available", [])
    if not hourly or not model_keys:
        return None
    
    # 加载 METAR（当天 + 前一天）
    metar_records = []
    for d in [target_date, _prev_date_str(target_date)]:
        m = load_metar(city_key, d)
        if m:
            metar_records.extend(m.get("records", []))
    
    if len(metar_records) < 3:
        return None
    
    # 城市时区
    cfg = load_config()
    city_cfg = cfg.get("cities", {}).get(city_key, {})
    tz_name = city_cfg.get("tz", "UTC")
    try:
        now_local = dt_mod.now(tz_mod.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        now_local = dt_mod.now(tz_mod.utc)
    
    # 实时权重
    live_w, live_rmse, n_pts = compute_live_weights(
        hourly, metar_records, model_keys, tz_name, ref_time=dt_mod.now(tz_mod.utc)
    )
    
    if not live_w or len(live_w) < 2:
        return None
    
    # 构建 L1 曲线：过去小时冻结，未来小时用新权重
    # 加载 engine 输出获取 frozen_l1 快照
    frozen_l1 = {}
    engine_path = PROJECT_ROOT / "data" / "engine" / city_key / f"{target_date}.json"
    if engine_path.exists():
        try:
            with open(engine_path) as f:
                eng_out = json.load(f)
            frozen_l1 = eng_out.get("l1_curve", {})
        except Exception:
            pass
    
    l1_vals = []
    current_hour = now_local.hour
    for h_data in hourly:
        h = h_data.get("hour")
        temps = h_data.get("temps", {})
        if h is None:
            continue
        
        if h < current_hour:
            # 过去小时：用冻结快照（不随新权重变化）
            fv = frozen_l1.get(str(h))
            if fv is not None:
                l1_vals.append(fv)
        else:
            # 未来小时：用最新权重
            w_sum, w_total = 0.0, 0.0
            for m, w in live_w.items():
                t = temps.get(m)
                if t is not None and w > 0:
                    w_sum += t * w
                    w_total += w
            if w_total > 0:
                l1_vals.append(round(w_sum / w_total, 1))
    
    # 如果未来时段缺失，用冻结值补
    for h_str, v in frozen_l1.items():
        if v is not None:
            try:
                h_int = int(h_str)
            except ValueError:
                continue
            if h_int >= current_hour and h_int not in {d.get("hour") for d in hourly if d.get("hour") is not None}:
                l1_vals.append(v)
    
    if not l1_vals:
        return None
    
    t_predicted = round(max(l1_vals), 1)
    current_bucket = round(t_predicted)
    now_iso = now_local.isoformat()
    
    # 检查是否需要记录
    snap_dir = PROJECT_ROOT / "data" / "engine" / city_key
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_path = snap_dir / f"{target_date}_snapshots.jsonl"
    
    should_record = True
    if snap_path.exists():
        try:
            with open(snap_path) as f:
                lines = f.readlines()
            if lines:
                last = json.loads(lines[-1].strip())
                last_bucket = last.get("bucket")
                last_ts = last.get("time", "")
                if last_bucket == current_bucket:
                    try:
                        last_dt = dt_mod.fromisoformat(last_ts)
                        now_dt = dt_mod.fromisoformat(now_iso)
                        if (now_dt - last_dt).total_seconds() / 60 < 30:
                            should_record = False
                    except Exception:
                        pass
        except Exception:
            pass
    
    if not should_record:
        return {"t_predicted": t_predicted, "bucket": current_bucket, "recorded": False}
    
    snapshot = {
        "time": now_iso,
        "t_predicted": t_predicted,
        "bucket": current_bucket,
        "metar_count": n_pts,
        "current_hour": now_local.hour,
        "live_weights": live_w,
        "live_rmse": live_rmse,
    }
    
    with open(snap_path, "a") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    
    snapshot["recorded"] = True
    return snapshot


def _prev_date_str(date_str: str) -> str:
    """返回前一天日期 YYYY-MM-DD"""
    from datetime import timedelta
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
