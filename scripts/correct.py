#!/usr/bin/env python3
"""Mercury Phase 3 — 实时 METAR 修正：拉当前实测 → 修正日最高温预报

原理:
  已知当前实测 T_now、当前小时 h_now、预报峰值 T_max_fc、峰值时间 h_peak
  如果 h_now < h_peak: 调整 T_max = T_now + (T_max_fc - T_fc[h_now]) * 衰减系数
  如果 h_now >= h_peak: T_max = max(T_now, 已过去时段最高温)

用法:
    python3 scripts/correct.py --city beijing
    python3 scripts/correct.py --all
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent


def load_config():
    with open(PROJECT_ROOT / "config" / "cities.yaml") as f:
        return yaml.safe_load(f)


def load_forecast_hourly(city_key: str, target_date: str) -> dict | None:
    """加载今日逐时预报（engine输出优先，fallback到forecast raw）"""
    # 优先用 engine 输出
    engine_path = PROJECT_ROOT / "data" / "engine" / city_key / f"{target_date}.json"
    if engine_path.exists():
        with open(engine_path) as f:
            eng = json.load(f)
        # 需要逐时曲线，从原始 forecast 加载
        pass
    
    # 直接用 forecast raw
    fc_path = PROJECT_ROOT / "data" / "forecasts" / city_key / target_date / "latest.json"
    if not fc_path.exists():
        return None
    with open(fc_path) as f:
        return json.load(f)


def fetch_latest_metar(city_key: str, target_date: str) -> dict | None:
    """从本地 METAR 文件读取最新观测，返回 {"temp_c": float, "time_utc": str}"""
    metar_path = PROJECT_ROOT / "data" / "metar" / city_key / f"{target_date}.json"
    if not metar_path.exists():
        return None
    with open(metar_path) as f:
        data = json.load(f)
    
    records = data.get("records", [])
    if not records:
        return None
    
    latest = records[-1]
    return {"temp_c": latest.get("temp_c"), "time_utc": latest.get("time_utc")}


def get_peak_hour(hourly_data: list[dict]) -> int:
    """从逐时数据中找峰值小时（ICON 模型的峰值）"""
    peak_h = 14  # 默认下午2点
    peak_t = -99
    for entry in hourly_data:
        temps = entry.get("temps", {})
        t = temps.get("icon_seamless", temps.get("ecmwf_ifs", 0))
        if t and t > peak_t:
            peak_t = t
            peak_h = entry["hour"]
    return peak_h, peak_t


def correct_city(city_key: str, city_cfg: dict) -> dict | None:
    """实时修正单个城市的日最高温预报"""
    target_date = date.today().isoformat()
    station = city_cfg["icao"]
    
    # 1. 加载逐时预报
    fc = load_forecast_hourly(city_key, target_date)
    if fc is None:
        return None
    
    hourly = fc.get("hourly", [])
    if not hourly:
        return None
    
    # 2. 取 ICON 逐时曲线（v3 核心模型）
    icon_curve = {}
    for entry in hourly:
        t = entry.get("temps", {}).get("icon_seamless")
        if t is not None:
            icon_curve[entry["hour"]] = t
    
    if not icon_curve:
        return None
    
    # 3. DEB 日最高温（从 engine 读取）
    eng_path = PROJECT_ROOT / "data" / "engine" / city_key / f"{target_date}.json"
    t_max_fc = None
    if eng_path.exists():
        with open(eng_path) as f:
            eng = json.load(f)
        t_max_fc = eng.get("t_calibrated")
    
    if t_max_fc is None:
        t_max_fc = max(icon_curve.values())
    
    # 4. 拉最新 METAR（从本地文件）
    metar = fetch_latest_metar(city_key, target_date)
    if metar is None or metar["temp_c"] is None:
        return {"city": city_key, "t_max_fc": t_max_fc, "t_max_corrected": t_max_fc,
                "correction": 0.0, "metar_ok": False}
    
    t_now = metar["temp_c"]
    
    # 解析当前小时
    time_str = metar["time_utc"]
    try:
        dt_utc = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        h_now_utc = dt_utc.hour
    except:
        h_now_utc = datetime.now(timezone.utc).hour
    
    # 转为当地时间
    tz_name = city_cfg.get("tz", "UTC")
    from datetime import timedelta
    # 简化：用 UTC+8 近似北京时间城市
    if "Shanghai" in tz_name or "Hong_Kong" in tz_name:
        h_now_local = (h_now_utc + 8) % 24
    elif "Tokyo" in tz_name or "Seoul" in tz_name:
        h_now_local = (h_now_utc + 9) % 24
    elif "London" in tz_name:
        h_now_local = (h_now_utc + 1) % 24
    elif "Berlin" in tz_name or "Paris" in tz_name or "Madrid" in tz_name or "Amsterdam" in tz_name:
        h_now_local = (h_now_utc + 2) % 24
    elif "New_York" in tz_name or "Atlanta" in tz_name or "Miami" in tz_name:
        h_now_local = (h_now_utc - 4) % 24
    elif "Chicago" in tz_name:
        h_now_local = (h_now_utc - 5) % 24
    elif "Los_Angeles" in tz_name:
        h_now_local = (h_now_utc - 7) % 24
    else:
        h_now_local = h_now_utc
    
    # 5. 找峰值小时
    peak_h, peak_t = get_peak_hour(hourly)
    
    # 6. 修正逻辑
    fc_at_now = icon_curve.get(h_now_local, t_now)
    
    if h_now_local < peak_h:
        # 还未到峰值 — 基于当前偏差外推
        current_bias = t_now - fc_at_now  # 正=实际比预报热
        # 衰减系数：离峰值越近，修正力度越小
        hours_to_peak = peak_h - h_now_local
        decay = min(1.0, hours_to_peak / 6.0)  # 6小时外全量修正
        correction = current_bias * decay
        t_max_corrected = round(t_max_fc + correction, 1)
        method = f"pre-peak (h={h_now_local}, bias={current_bias:+.1f}, decay={decay:.2f})"
    else:
        # 已过峰值 — 取 max(当前, 预报峰值)
        t_max_corrected = round(max(t_now, t_max_fc), 1)
        method = f"post-peak (h={h_now_local}, max({t_now:.0f}, {t_max_fc:.0f}))"
    
    return {
        "city": city_key,
        "station": station,
        "local_hour": h_now_local,
        "peak_hour": peak_h,
        "t_now": t_now,
        "t_fc_at_now": round(fc_at_now, 1),
        "current_bias": round(t_now - fc_at_now, 1),
        "t_max_fc": t_max_fc,
        "t_max_corrected": t_max_corrected,
        "correction": round(t_max_corrected - t_max_fc, 1),
        "method": method,
        "metar_ok": True,
    }


def main():
    parser = argparse.ArgumentParser(description="Mercury 实时 METAR 修正")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key")
    group.add_argument("--all", action="store_true", help="所有城市")
    args = parser.parse_args()

    config = load_config()
    
    if args.city:
        if args.city not in config["cities"]:
            print(f"❌ 未知城市: {args.city}")
            sys.exit(1)
        targets = [(args.city, config["cities"][args.city])]
    else:
        targets = [(k, v) for k, v in config["cities"].items()]

    print(f"{'城市':12s} {'时':>2s} {'峰时':>2s} {'当前':>5s} {'预报':>5s} {'偏差':>5s} {'原预报':>6s} {'修正后':>6s} {'调整':>5s}")
    print("-" * 75)

    corrections = []
    for city_key, city_cfg in targets:
        result = correct_city(city_key, city_cfg)
        if result is None:
            continue
        
        display = city_cfg.get("display_name", city_key)
        
        if not result["metar_ok"]:
            print(f"  {display:10s} ⚠️ 无 METAR 数据")
            continue

        correction = result["correction"]
        arrow = "⬆" if correction > 0 else "⬇" if correction < 0 else "→"
        
        print(f"  {display:10s} {result['local_hour']:2d}h {result['peak_hour']:2d}h "
              f"{result['t_now']:4.0f}°C {result['t_fc_at_now']:4.0f}°C {result['current_bias']:+4.0f}°C "
              f"{result['t_max_fc']:5.1f}°C {result['t_max_corrected']:5.1f}°C {correction:+4.1f}°C {arrow}")
        
        corrections.append(result)

    if corrections:
        n_up = sum(1 for c in corrections if c["correction"] > 0)
        n_down = sum(1 for c in corrections if c["correction"] < 0)
        n_same = sum(1 for c in corrections if c["correction"] == 0)
        avg_corr = sum(c["correction"] for c in corrections) / len(corrections)
        print(f"\n共 {len(corrections)} 城, 上调 {n_up}, 下调 {n_down}, 不变 {n_same}, 平均修正 {avg_corr:+.1f}°C")


if __name__ == "__main__":
    main()
