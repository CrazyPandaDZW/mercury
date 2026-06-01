#!/usr/bin/env python3
"""Mercury Phase 1 — 拉取 Open-Meteo 多模型集合预报

每次运行保存当日生成的预报快照，支持追踪同一目标日期的预报演变。

用法:
    python3 scripts/fetch_openmeteo.py --city beijing
    python3 scripts/fetch_openmeteo.py --all              # 所有40城
    python3 scripts/fetch_openmeteo.py --city beijing --days 10
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent


def load_config():
    with open(PROJECT_ROOT / "config" / "cities.yaml") as f:
        return yaml.safe_load(f)


def get_models_for_city(city_key: str, config: dict) -> list:
    """根据城市所属区域返回可用模型列表"""
    global_models = config["models"]["global"]
    europe_models = config["models"].get("europe", [])
    europe_cities = config.get("region_europe", [])

    if city_key in europe_cities:
        return global_models + europe_models
    return global_models


def fetch_city(city_key: str, city_cfg: dict, models: list, days: int) -> dict:
    """拉取单个城市的多模型集合预报"""
    lat = city_cfg["lat"]
    lon = city_cfg["lon"]
    tz = city_cfg["tz"]
    model_str = ",".join(models)

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,cloud_cover"
        f"&models={model_str}"
        f"&forecast_days={days}"
        f"&timezone={tz}"
    )

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read())

    return parse_response(city_key, raw, models)


def parse_response(city_key: str, raw: dict, models: list) -> dict:
    """解析 Open-Meteo 响应，按日期组织，保留逐时温度曲线"""
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])

    days = {}
    all_models = []
    model_keys = {}

    for m in models:
        key = f"temperature_2m_{m}"
        if key in hourly:
            vals = hourly[key]
            if any(v is not None for v in vals):
                all_models.append(m)
                model_keys[m] = key

    for i, t_str in enumerate(times):
        date_str = t_str[:10]
        hour = int(t_str[11:13])

        if date_str not in days:
            days[date_str] = {"hours": [], "models": {m: [] for m in all_models}, "cloud_cover": []}

        days[date_str]["hours"].append(hour)
        for m in all_models:
            val = hourly[model_keys[m]][i]
            days[date_str]["models"][m].append(val)
        
        # 云量（取 ICON 模型，fallback 到 ECMWF）
        cc = None
        for cc_model in ["icon_seamless", "ecmwf_ifs"]:
            cc_key = f"cloud_cover_{cc_model}"
            if cc_key in hourly:
                cc = hourly[cc_key][i]
                break
        days[date_str]["cloud_cover"].append(cc)

    # 按日期组织预报
    generated_at = datetime.now(timezone.utc).isoformat()
    generated_date = date.today().isoformat()

    forecasts = {}
    for date_str, day_data in days.items():
        t_max = {}
        hourly_detail = []

        for m in all_models:
            vals = [v for v in day_data["models"][m] if v is not None]
            if vals:
                t_max[m] = round(max(vals), 1)

        # 逐时温度曲线（每个模型一条）
        for h_idx, hour in enumerate(day_data["hours"]):
            hour_entry = {"hour": hour, "temps": {}}
            for m in all_models:
                hour_entry["temps"][m] = day_data["models"][m][h_idx]
            # 云量
            if h_idx < len(day_data.get("cloud_cover", [])):
                cc = day_data["cloud_cover"][h_idx]
                if cc is not None:
                    hour_entry["cloud_cover"] = round(cc)
            hourly_detail.append(hour_entry)

        t_ensemble = None
        if t_max:
            t_ensemble = round(sum(t_max.values()) / len(t_max), 1)

        forecasts[date_str] = {
            "target_date": date_str,
            "generated_date": generated_date,
            "generated_at": generated_at,
            "t_max": t_max,
            "t_ensemble": t_ensemble,
            "n_models": len(all_models),
            "models_available": all_models,
            "hourly": hourly_detail,
        }

    return {
        "city": city_key,
        "lat": raw.get("latitude"),
        "lon": raw.get("longitude"),
        "elevation": raw.get("elevation"),
        "tz": raw.get("timezone"),
        "generated_date": generated_date,
        "generated_at": generated_at,
        "forecasts": forecasts,
    }


def save_forecast(city_key: str, data: dict):
    """保存预报快照，支持演变追踪
    
    目录结构:
      data/forecasts/{city}/{target_date}/
        {generated_date}.json     ← 某天生成的快照
        latest.json               ← 最新快照（软链接效果）
    """
    base_dir = PROJECT_ROOT / "data" / "forecasts" / city_key
    base_dir.mkdir(parents=True, exist_ok=True)

    gen_date = data["generated_date"]

    for target_date, forecast in data["forecasts"].items():
        target_dir = base_dir / target_date
        target_dir.mkdir(parents=True, exist_ok=True)

        # 保存快照: {target_date}/{generated_date}.json
        snapshot_path = target_dir / f"{gen_date}.json"
        with open(snapshot_path, "w") as f:
            json.dump(forecast, f, ensure_ascii=False, indent=2)

        # latest.json 指向最新
        latest_path = target_dir / "latest.json"
        with open(latest_path, "w") as f:
            json.dump(forecast, f, ensure_ascii=False, indent=2)

    # 根级 latest.json
    root_latest = base_dir / "latest.json"
    with open(root_latest, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="拉取 Open-Meteo 多模型集合预报")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key（如 beijing）")
    group.add_argument("--all", action="store_true", help="所有 40 城")
    parser.add_argument("--days", type=int, default=16, help="预报天数（默认 16）")
    args = parser.parse_args()

    config = load_config()
    rate_limit = config["openmeteo"]["rate_limit_delay"]

    if args.city:
        city_key = args.city
        if city_key not in config["cities"]:
            print(f"❌ 未知城市: {city_key}")
            print(f"   可用: {', '.join(config['cities'].keys())}")
            sys.exit(1)
        targets = [(city_key, config["cities"][city_key])]
    else:
        targets = list(config["cities"].items())

    success, fail = 0, 0
    for i, (city_key, city_cfg) in enumerate(targets):
        display = city_cfg.get("display_name", city_key)
        print(f"[{i+1}/{len(targets)}] {display} ({city_key}) ... ", end="", flush=True)

        try:
            models = get_models_for_city(city_key, config)
            data = fetch_city(city_key, city_cfg, models, days=args.days)
            save_forecast(city_key, data)

            dates = list(data["forecasts"].keys())
            n_models = data["forecasts"][dates[0]]["n_models"]
            t_ens = data["forecasts"][dates[0]].get("t_ensemble", "N/A")
            print(f"✅ {n_models}模型  {len(dates)}天({dates[0]}~{dates[-1]})  t_ens={t_ens}°C")
            success += 1

        except Exception as e:
            print(f"❌ {e}")
            fail += 1

        if i < len(targets) - 1:
            time.sleep(rate_limit)

    print(f"\n{'='*50}")
    print(f"完成: {success} 成功, {fail} 失败")
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
