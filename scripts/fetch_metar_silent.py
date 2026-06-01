#!/usr/bin/env python3
"""静默拉取当日全城 METAR — 用于高频 cron，成功则无输出，失败才报错"""
import sys, os
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from fetch_metar import load_config, fetch_metar, save_metar

config = load_config()
target_date = date.today().isoformat()
errors = []

for city_key, city_cfg in config["cities"].items():
    station = city_cfg["icao"]
    try:
        records = fetch_metar(station, target_date)
        save_metar(city_key, station, target_date, records)
    except Exception as e:
        errors.append(f"{city_key}: {e}")

if errors:
    print(f"METAR 拉取失败 {len(errors)}/{len(config['cities'])}:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
# 成功则无输出 = cron 不推送消息
