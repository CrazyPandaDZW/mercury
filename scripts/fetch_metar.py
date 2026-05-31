#!/usr/bin/env python3
"""Mercury Phase 1 — 拉取 IEM ASOS (METAR) 实测温度

用法:
    python3 scripts/fetch_metar.py --city beijing
    python3 scripts/fetch_metar.py --all
    python3 scripts/fetch_metar.py --city beijing --date 2026-06-01
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


def fetch_metar(station: str, target_date: str) -> list[dict]:
    """从 IEM 拉取指定日期 ASOS 逐时温度数据"""
    # 解析日期，构造 CST 时间范围（IEM 使用 CST = UTC-6）
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    # IEM API: 需要 CST 格式的起止时间
    start_str = dt.strftime("%Y-%m-%d 00:00")
    end_str = dt.strftime("%Y-%m-%d 23:59")

    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={station}"
        f"&data=tmpc"
        f"&year1={dt.year}&month1={dt.month}&day1={dt.day}"
        f"&year2={dt.year}&month2={dt.month}&day2={dt.day}"
        "&tz=Etc/UTC&format=onlycomma&latlon=no&missing=null"
    )

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")

    return parse_iem_csv(text)


def parse_iem_csv(text: str) -> list[dict]:
    """解析 IEM ASOS CSV 格式"""
    records = []
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return records

    header = lines[0].split(",")
    try:
        idx_station = header.index("station")
        idx_valid = header.index("valid")
        idx_tmpc = header.index("tmpc") if "tmpc" in header else -1
    except ValueError:
        return records

    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < len(header):
            continue

        tmpc_val = None

        if idx_tmpc >= 0 and parts[idx_tmpc] not in ("", "M", "null"):
            try:
                tmpc_val = float(parts[idx_tmpc])
            except ValueError:
                pass

        if tmpc_val is not None:
            records.append({
                "station": parts[idx_station],
                "time_utc": parts[idx_valid],
                "temp_c": tmpc_val,
            })

    return records


def save_metar(city_key: str, station: str, target_date: str, records: list[dict]):
    """保存 METAR 数据"""
    base_dir = PROJECT_ROOT / "data" / "metar" / city_key
    base_dir.mkdir(parents=True, exist_ok=True)

    filepath = base_dir / f"{target_date}.json"
    output = {
        "city": city_key,
        "station": station,
        "date": target_date,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "n_records": len(records),
        "records": records,
    }

    if records:
        temps = [r["temp_c"] for r in records if r["temp_c"] is not None]
        if temps:
            output["t_max"] = round(max(temps), 1)
            output["t_min"] = round(min(temps), 1)

    with open(filepath, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="拉取 IEM ASOS METAR 实测温度")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key（如 beijing）")
    group.add_argument("--all", action="store_true", help="所有 40 城")
    parser.add_argument("--date", type=str, default=None,
                        help="日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args()

    config = load_config()
    target_date = args.date or date.today().isoformat()

    if args.city:
        city_key = args.city
        if city_key not in config["cities"]:
            print(f"❌ 未知城市: {city_key}")
            sys.exit(1)
        targets = [(city_key, config["cities"][city_key])]
    else:
        targets = list(config["cities"].items())

    success, fail = 0, 0
    for i, (city_key, city_cfg) in enumerate(targets):
        station = city_cfg["icao"]
        display = city_cfg.get("display_name", city_key)
        print(f"[{i+1}/{len(targets)}] {display} ({station}) ... ", end="", flush=True)

        try:
            records = fetch_metar(station, target_date)
            save_metar(city_key, station, target_date, records)

            t_max = None
            temps = [r["temp_c"] for r in records if r["temp_c"] is not None]
            if temps:
                t_max = max(temps)

            print(f"✅ {len(records)}条  t_max={t_max}°C")
            success += 1
        except Exception as e:
            print(f"❌ {e}")
            fail += 1

        time.sleep(0.5)

    print(f"\n{'='*50}")
    print(f"完成: {success} 成功, {fail} 失败")


if __name__ == "__main__":
    main()
