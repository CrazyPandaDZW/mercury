#!/usr/bin/env python3
"""Mercury Phase 1 — 拉取 Open-Meteo 多模型集合预报

用法:
    python3 scripts/fetch_openmeteo.py --city beijing
    python3 scripts/fetch_openmeteo.py --all              # 所有40城
    python3 scripts/fetch_openmeteo.py --city beijing --days 5
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent


def load_config():
    with open(PROJECT_ROOT / "config" / "cities.yaml") as f:
        return yaml.safe_load(f)


def fetch_city(city_key: str, city_cfg: dict, models: list, days: int) -> dict:
    """拉取单个城市的多模型集合预报"""
    lat = city_cfg["lat"]
    lon = city_cfg["lon"]
    tz = city_cfg["tz"]
    model_str = ",".join(models)

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&models={model_str}"
        f"&forecast_days={days}"
        f"&timezone={tz}"
    )

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read())

    return parse_response(city_key, raw, models)


def parse_response(city_key: str, raw: dict, models: list) -> dict:
    """解析 Open-Meteo 响应，按日期组织"""
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])

    # 按日期分组
    days = {}
    all_models = []
    model_keys = {}

    for m in models:
        key = f"temperature_2m_{m}"
        if key in hourly:
            vals = hourly[key]
            has_data = any(v is not None for v in vals)
            if has_data:
                all_models.append(m)
                model_keys[m] = key

    for i, t_str in enumerate(times):
        date_str = t_str[:10]  # "2026-06-01T14:00" → "2026-06-01"
        hour = int(t_str[11:13])

        if date_str not in days:
            days[date_str] = {"hours": [], "models": {}}

        days[date_str]["hours"].append(hour)
        for m in all_models:
            if m not in days[date_str]["models"]:
                days[date_str]["models"][m] = []
            days[date_str]["models"][m].append(hourly[model_keys[m]][i])

    # 计算每模型每日最高温
    forecasts = {}
    for date_str, day_data in days.items():
        day_forecast = {
            "date": date_str,
            "t_max": {},  # 每模型最高温
            "t_ensemble": None,  # 集合均值
            "n_models": len(all_models),
            "models_available": all_models,
        }

        model_maxes = {}
        for m in all_models:
            vals = [v for v in day_data["models"][m] if v is not None]
            if vals:
                model_maxes[m] = round(max(vals), 1)

        day_forecast["t_max"] = model_maxes
        if model_maxes:
            day_forecast["t_ensemble"] = round(
                sum(model_maxes.values()) / len(model_maxes), 1
            )

        forecasts[date_str] = day_forecast

    return {
        "city": city_key,
        "lat": raw.get("latitude"),
        "lon": raw.get("longitude"),
        "elevation": raw.get("elevation"),
        "tz": raw.get("timezone"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "forecasts": forecasts,
    }


def save_forecast(city_key: str, data: dict):
    """保存预报到 data/forecasts/{city}/{date}.json"""
    base_dir = PROJECT_ROOT / "data" / "forecasts" / city_key
    base_dir.mkdir(parents=True, exist_ok=True)

    for date_str, forecast in data["forecasts"].items():
        filepath = base_dir / f"{date_str}.json"
        output = {
            "city": data["city"],
            "date": date_str,
            "fetched_at": data["fetched_at"],
            **forecast,
        }
        with open(filepath, "w") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    # 保存 latest 元数据
    latest_path = base_dir / "latest.json"
    with open(latest_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="拉取 Open-Meteo 多模型集合预报")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key（如 beijing）")
    group.add_argument("--all", action="store_true", help="所有 40 城")
    parser.add_argument("--days", type=int, default=3, help="预报天数（默认 3）")
    args = parser.parse_args()

    config = load_config()
    models = config["models"]
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
            data = fetch_city(city_key, city_cfg, models, days=args.days)
            save_forecast(city_key, data)

            dates = list(data["forecasts"].keys())
            n_models = data["forecasts"][dates[0]]["n_models"]
            t_ens = data["forecasts"][dates[0]].get("t_ensemble", "N/A")
            print(f"✅ {n_models}模型  {dates[0]} t_ens={t_ens}°C")
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
