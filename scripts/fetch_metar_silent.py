#!/usr/bin/env python3
"""静默拉取当日全城 METAR — 并行增量追加模式，用于系统 cron。
成功无输出，失败才写 stderr。

并发 8 线程拉取 39 城，将原 ~10 分钟压至 ~2 分钟。
用法: python3 scripts/fetch_metar_silent.py [--date YYYY-MM-DD]
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from fetch_metar import load_config, fetch_metar, save_metar, load_existing_metar

MAX_WORKERS = 2   # NOAA IEM 速率限制严格，2线并发
RETRY_COUNT = 2   # 失败重试次数

# 全局速率控制
import threading
_rate_lock = threading.Lock()
_rate_last = 0.0
_RATE_INTERVAL = 2.0  # 两次请求最小间隔(秒)


def _rate_wait():
    """确保两次请求间隔 >= _RATE_INTERVAL 秒"""
    global _rate_last
    with _rate_lock:
        now = time.time()
        wait = _RATE_INTERVAL - (now - _rate_last)
        if wait > 0:
            time.sleep(wait)
        _rate_last = time.time()


def fetch_one(city_key: str, city_cfg: dict, target_date: str) -> dict:
    """拉取单个城市的 METAR，返回结果字典"""
    station = city_cfg["icao"]
    display = city_cfg.get("display_name", city_key)
    for attempt in range(RETRY_COUNT + 1):
        try:
            _rate_wait()  # 全局限速
            records = fetch_metar(station, target_date)
            n_before = 0
            existing = load_existing_metar(city_key, target_date)
            if existing:
                n_before = existing.get("n_records", 0)
            save_metar(city_key, station, target_date, records, merge=True)

            # 读取最终文件获取记录数
            final_path = PROJECT_ROOT / "data" / "metar" / city_key / f"{target_date}.json"
            if final_path.exists():
                import json
                with open(final_path) as f:
                    final = json.load(f)
                n_now = final.get("n_records", 0)
                n_new = n_now - n_before
                return {"city": display, "ok": True, "total": n_now, "new": n_new}
            return {"city": display, "ok": True, "total": len(records), "new": len(records)}

        except Exception as e:
            if attempt < RETRY_COUNT:
                time.sleep(2 * (attempt + 1))
                continue
            return {"city": display, "ok": False, "error": str(e)}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args()

    config = load_config()
    target_date = args.date or date.today().isoformat()
    cities = list(config["cities"].items())

    t0 = time.time()
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_one, k, v, target_date): k for k, v in cities}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            if not r["ok"]:
                errors.append(f"  {r['city']}: {r['error']}")

    elapsed = time.time() - t0

    if errors:
        print(f"METAR {target_date}: {len(errors)}/{len(cities)} 失败 ({elapsed:.0f}s)", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)

    # 成功 → 无 stdout（cron 不推送邮件）
    # 如需确认，可看 parsed log
    total_new = sum(r.get("new", 0) for r in results)
    if total_new > 0:
        # 静默记录到日志文件（不推送到stdout）
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        with open(log_dir / "metar_fetch.log", "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} [{target_date}] "
                    f"{len(cities)} cities, {total_new} new records, {elapsed:.0f}s\n")


if __name__ == "__main__":
    main()
