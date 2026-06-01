#!/usr/bin/env python3
"""静默拉取当日全城 METAR — 增量追加模式，用于高频 cron。
成功无输出（cron 不推送），失败才报错 stderr。
"""
import sys, os, time
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from fetch_metar import load_config, fetch_metar, save_metar, load_existing_metar

config = load_config()
target_date = date.today().isoformat()
errors = []

for city_key, city_cfg in config["cities"].items():
    station = city_cfg["icao"]
    try:
        records = fetch_metar(station, target_date)
        save_metar(city_key, station, target_date, records, merge=True)
    except Exception as e:
        errors.append(f"  {city_key}: {e}")
    time.sleep(0.2)  # 避免限速

if errors:
    print(f"METAR 失败 {len(errors)}/{len(config['cities'])}:")
    for e in errors:
        print(e)
    sys.exit(1)
# 成功 → 无 stdout，cron 不推送
