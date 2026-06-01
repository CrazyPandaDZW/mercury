#!/usr/bin/env python3
"""Mercury — 拉取 IEM ASOS (METAR) 实测气象数据

每半小时高频采集时增量追加，保留完整字段：温度/露点/风速/气压。
支持 --merge 增量模式（仅追加新记录，去重）。

用法:
    python3 scripts/fetch_metar.py --city beijing
    python3 scripts/fetch_metar.py --all
    python3 scripts/fetch_metar.py --city beijing --date 2026-06-01
    python3 scripts/fetch_metar.py --all --merge    # 增量追加（高频采集用）
"""

import argparse
import json
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
    """从 IEM 拉取指定日期 ASOS 逐时气象数据（温度/露点/风速/气压）"""
    dt = datetime.strptime(target_date, "%Y-%m-%d")

    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={station}"
        f"&data=tmpc&data=dwpc&data=sknt&data=alti"
        f"&year1={dt.year}&month1={dt.month}&day1={dt.day}"
        f"&year2={dt.year}&month2={dt.month}&day2={dt.day}"
        "&tz=Etc/UTC&format=onlycomma&latlon=no&missing=null"
    )

    req = urllib.request.Request(url)
    # 绕过系统代理直连 NOAA IEM（代理会 MITM SSL 证书导致验证失败）
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    with opener.open(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")

    return parse_iem_csv(text, station)


def parse_iem_csv(text: str, station: str) -> list[dict]:
    """解析 IEM ASOS CSV，提取温度/露点/风速/气压"""
    records = []
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return records

    header = lines[0].split(",")
    try:
        idx_station = header.index("station")
        idx_valid = header.index("valid")
        idx_tmpc = header.index("tmpc") if "tmpc" in header else -1
        idx_dwpc = header.index("dwpc") if "dwpc" in header else -1
        idx_sknt = header.index("sknt") if "sknt" in header else -1
        idx_alti = header.index("alti") if "alti" in header else -1
    except ValueError:
        return records

    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < len(header):
            continue

        def _float(idx):
            if idx < 0 or idx >= len(parts):
                return None
            v = parts[idx]
            if v in ("", "M", "null"):
                return None
            try:
                return round(float(v), 1)
            except ValueError:
                return None

        record = {
            "station": parts[idx_station],
            "time_utc": parts[idx_valid],
            "temp_c": _float(idx_tmpc),
            "dew_point_c": _float(idx_dwpc),
            "wind_knots": _float(idx_sknt),
            "pressure_alti": _float(idx_alti),
        }

        # 至少要有温度才算有效记录
        if record["temp_c"] is not None:
            records.append(record)

    return records


def load_existing_metar(city_key: str, target_date: str) -> dict | None:
    """加载已有的 METAR 文件"""
    path = PROJECT_ROOT / "data" / "metar" / city_key / f"{target_date}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def save_metar(city_key: str, station: str, target_date: str, 
               records: list[dict], merge: bool = False):
    """保存 METAR 数据。merge=True 时增量追加新记录（按 time_utc 去重）"""
    base_dir = PROJECT_ROOT / "data" / "metar" / city_key
    base_dir.mkdir(parents=True, exist_ok=True)
    filepath = base_dir / f"{target_date}.json"

    fetched_at = datetime.now(timezone.utc).isoformat()
    fetch_count = 1

    if merge:
        existing = load_existing_metar(city_key, target_date)
        if existing:
            # 已有记录的时间戳集合
            existing_times = {r["time_utc"] for r in existing.get("records", [])}
            new_records = [r for r in records if r["time_utc"] not in existing_times]
            
            if not new_records:
                return  # 无新数据，不写文件
            
            existing["records"].extend(new_records)
            existing["records"].sort(key=lambda r: r["time_utc"])
            existing["n_records"] = len(existing["records"])
            existing["fetched_at"] = fetched_at
            existing["fetch_count"] = existing.get("fetch_count", 1) + 1
            records = existing["records"]
            fetch_count = existing["fetch_count"]
        else:
            records.sort(key=lambda r: r["time_utc"])
    else:
        records.sort(key=lambda r: r["time_utc"])

    # 计算统计
    temps = [r["temp_c"] for r in records if r["temp_c"] is not None]
    dewps = [r["dew_point_c"] for r in records if r["dew_point_c"] is not None]
    winds = [r["wind_knots"] for r in records if r["wind_knots"] is not None]

    output = {
        "city": city_key,
        "station": station,
        "date": target_date,
        "source": "IEM ASOS (NOAA)",
        "icao": station,
        "fetched_at": fetched_at,
        "fetch_count": fetch_count,
        "n_records": len(records),
        "records": records,
    }

    if temps:
        output["t_max"] = round(max(temps), 1)
        output["t_min"] = round(min(temps), 1)
        output["t_avg"] = round(sum(temps) / len(temps), 1)
    if dewps:
        output["dew_point_avg"] = round(sum(dewps) / len(dewps), 1)
    if winds:
        output["wind_max_knots"] = round(max(winds), 1)

    with open(filepath, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # ── METAR 写入后自动更新预测快照 ──
    try:
        from engine import snapshot_prediction
        snap = snapshot_prediction(city_key, target_date)
        if snap and snap.get("recorded"):
            new_bucket = snap.get("bucket")
            t_pred = snap.get("t_predicted")
            if not merge or not locals().get("_quiet_log"):
                print(f"      📊 预测更新: t={t_pred}°C bucket={new_bucket}")
    except Exception:
        pass  # 快照失败不阻塞 METAR 拉取


def main():
    parser = argparse.ArgumentParser(description="拉取 IEM ASOS METAR 实测气象数据")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key（如 beijing）")
    group.add_argument("--all", action="store_true", help="所有城市")
    parser.add_argument("--date", type=str, default=None,
                        help="日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--merge", action="store_true",
                        help="增量追加模式（高频采集用，去重不覆盖）")
    parser.add_argument("--quiet", action="store_true",
                        help="静默模式（成功无输出）")
    args = parser.parse_args()

    config = load_config()
    target_date = args.date or date.today().isoformat()

    if args.city:
        if args.city not in config["cities"]:
            print(f"❌ 未知城市: {args.city}")
            sys.exit(1)
        targets = [(args.city, config["cities"][args.city])]
    else:
        targets = list(config["cities"].items())

    success, fail, skipped = 0, 0, 0
    for i, (city_key, city_cfg) in enumerate(targets):
        station = city_cfg["icao"]
        display = city_cfg.get("display_name", city_key)

        try:
            records = fetch_metar(station, target_date)
            n_before = 0
            if args.merge:
                existing = load_existing_metar(city_key, target_date)
                if existing:
                    n_before = existing.get("n_records", 0)

            save_metar(city_key, station, target_date, records, merge=args.merge)

            if args.merge and n_before > 0:
                # 读取保存后的文件获取最终记录数
                with open(PROJECT_ROOT / "data" / "metar" / city_key / f"{target_date}.json") as f:
                    final = json.load(f)
                n_new = final["n_records"] - n_before
                if n_new == 0:
                    skipped += 1
                    continue
                if not args.quiet:
                    print(f"[{i+1}/{len(targets)}] {display:6s} +{n_new}条 (共{final['n_records']})")
            elif not args.quiet:
                print(f"[{i+1}/{len(targets)}] {display:6s} ✅ {len(records)}条  t_max={max(r['temp_c'] for r in records if r['temp_c'])}°C")
            success += 1

        except Exception as e:
            if not args.quiet:
                print(f"[{i+1}/{len(targets)}] {display:6s} ❌ {e}")
            fail += 1

        time.sleep(0.3)  # 礼貌限速

    if not args.quiet:
        status = f"成功 {success}, 失败 {fail}"
        if args.merge:
            status += f", 跳过(无新数据) {skipped}"
        print(f"\n{'='*50}")
        print(f"完成: {status}")
    
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
