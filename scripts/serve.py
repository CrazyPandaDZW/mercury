#!/usr/bin/env python3
"""Mercury 前端 — API 服务 + 静态页面

用法:
    python3 scripts/serve.py                 # 默认端口 8080
    python3 scripts/serve.py --port 3000     # 自定义端口
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = PROJECT_ROOT / "data"

# 导入引擎权重计算（需在 PROJECT_ROOT 定义后）
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
try:
    from engine import compute_live_weights, load_config as eng_load_config
except ImportError:
    compute_live_weights = None
    eng_load_config = None


def _prev_date(date_str: str) -> str:
    """返回前一天日期字符串"""
    from datetime import timedelta
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


class MercuryHandler(SimpleHTTPRequestHandler):
    """自定义请求处理：API 端点 + 静态文件"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # API 路由
        if path == "/api/status":
            self._json_response(self._get_status())
        elif path == "/api/cities":
            self._json_response(self._get_cities())
        elif path == "/api/summary":
            self._json_response(self._get_summary(params.get("date", [None])[0]))
        elif path == "/api/forecast":
            self._json_response(self._get_forecast(
                params.get("city", [None])[0],
                params.get("date", [None])[0],
            ))
        elif path == "/api/metar":
            self._json_response(self._get_metar(
                params.get("city", [None])[0],
                params.get("date", [None])[0],
            ))
        elif path == "/" or path == "":
            # 首页重定向到 frontend/index.html
            self.send_response(302)
            self.send_header("Location", "/frontend/index.html")
            self.end_headers()
        else:
            # 静态文件
            super().do_GET()

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def _get_status(self):
        fc_dirs = list((DATA_DIR / "forecasts").iterdir()) if (DATA_DIR / "forecasts").exists() else []
        metar_dirs = list((DATA_DIR / "metar").iterdir()) if (DATA_DIR / "metar").exists() else []
        today = date.today().isoformat()
        return {
            "ok": True,
            "cities_forecast": len([d for d in fc_dirs if d.is_dir()]),
            "cities_metar": len([d for d in metar_dirs if d.is_dir()]),
            "today": today,
        }

    def _get_cities(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "cities.yaml") as f:
            config = yaml.safe_load(f)
        return {
            "cities": [
                {
                    "key": k,
                    "name": v.get("display_name", k),
                    "icao": v.get("icao"),
                    "region": self._guess_region(k),
                }
                for k, v in config["cities"].items()
            ]
        }

    def _guess_region(self, city_key: str) -> str:
        china = {"beijing","shanghai","chongqing","qingdao","wuhan","hong-kong","taipei"}
        us = {"atlanta","austin","chicago","dallas","denver","houston","los-angeles","miami","nyc","san-francisco","seattle"}
        asia = {"tokyo","seoul","singapore","kuala-lumpur","jakarta","manila","karachi"}
        europe = {"london","paris","amsterdam","munich","berlin","madrid","rome","warsaw","helsinki","istanbul","ankara","moscow","tel-aviv"}
        south = {"cape-town","mexico-city","panama-city"}
        if city_key in china: return "🇨🇳"
        if city_key in us: return "🇺🇸"
        if city_key in asia: return "🌏"
        if city_key in europe: return "🇪🇺"
        if city_key in south: return "🌍"
        return "🌐"

    def _get_summary(self, target_date=None):
        target_date = target_date or date.today().isoformat()
        cities = []
        engine_dir = DATA_DIR / "engine"
        if not engine_dir.exists():
            return {"date": target_date, "cities": [], "n": 0}

        for d in sorted(engine_dir.iterdir()):
            if not d.is_dir(): continue
            city = d.name
            f = d / f"{target_date}.json"
            if not f.exists(): continue
            with open(f) as fp:
                eng = json.load(fp)
            
            metar_f = DATA_DIR / "metar" / city / f"{target_date}.json"
            metar_tmax = None
            if metar_f.exists():
                with open(metar_f) as fp:
                    m = json.load(fp)
                metar_tmax = m.get("t_max")

            cities.append({
                "city": city,
                "t_deb": eng.get("t_calibrated"),
                "t_std": eng.get("t_std"),
                "t_metar": metar_tmax,
                "error": eng.get("forecast_error"),
                "n_models": eng.get("n_models"),
                "blend_method": eng.get("blend_method"),
                "top_bucket": max(eng.get("buckets_normal", {}).items(), key=lambda x: x[1]) if eng.get("buckets_normal") else None,
            })

        errors = [c["error"] for c in cities if c["error"] is not None]
        return {
            "date": target_date,
            "n": len(cities),
            "n_with_metar": len(errors),
            "mae": round(sum(abs(e) for e in errors) / len(errors), 2) if errors else None,
            "bias": round(sum(errors) / len(errors), 2) if errors else None,
            "within_1c": sum(1 for e in errors if abs(e) <= 1) if errors else 0,
            "within_2c": sum(1 for e in errors if abs(e) <= 2) if errors else 0,
            "cities": sorted(cities, key=lambda c: abs(c["error"]) if c["error"] is not None else 999),
        }

    def _get_forecast(self, city_key, target_date=None):
        target_date = target_date or date.today().isoformat()
        if not city_key:
            return {"error": "missing city parameter"}

        # Engine 输出
        eng_f = DATA_DIR / "engine" / city_key / f"{target_date}.json"
        eng = None
        if eng_f.exists():
            with open(eng_f) as f:
                eng = json.load(f)

        # 逐时曲线
        fc_f = DATA_DIR / "forecasts" / city_key / target_date / "latest.json"
        hourly = None
        if fc_f.exists():
            with open(fc_f) as f:
                fc = json.load(f)
            hourly = fc.get("hourly", [])

        # L1 融合曲线 — 冻结历史 + 动态未来
        frozen_l1 = eng.get("l1_curve", {}) if eng else {}
        is_fahrenheit = eng.get("unit") == "fahrenheit" if eng else False
        
        l1_curve_c = self._build_l1_curve(
            city_key, target_date, eng, hourly, frozen_l1, is_fahrenheit
        )
        
        # 如果是 °F 城市，输出转 °F
        if is_fahrenheit and l1_curve_c:
            l1_curve = {h: round(v * 9.0 / 5.0 + 32.0, 1) for h, v in l1_curve_c.items()}
        else:
            l1_curve = l1_curve_c

        return {
            "city": city_key,
            "date": target_date,
            "engine": eng,
            "hourly": hourly,
            "l1_curve": l1_curve,
        }

    def _build_l1_curve(self, city_key, target_date, eng, hourly, frozen_l1, is_fahrenheit=False):
        """合并冻结 L1 + 动态 L1（未来时段用最新 METAR 重算权重）。
        
        内部统一用 °C 计算，避免 °F/°C 混用导致曲线跳变。
        副作用：如果预测桶发生变化，追加快照到 snapshots.jsonl。
        """
        if not hourly or not eng or compute_live_weights is None:
            return frozen_l1

        # ── °F 城市：frozen_l1 先转回 °C，内部统一单位 ──
        if is_fahrenheit:
            frozen_l1_c = {}
            for h, v in frozen_l1.items():
                try:
                    frozen_l1_c[h] = round((float(v) - 32.0) * 5.0 / 9.0, 1)
                except (ValueError, TypeError):
                    frozen_l1_c[h] = v
        else:
            frozen_l1_c = frozen_l1

        # 获取当前当地时间
        from datetime import timezone as tz_utc
        try:
            cfg = eng_load_config()
            city_tz_name = cfg.get("cities", {}).get(city_key, {}).get("tz", "UTC")
            now_local = datetime.now(tz_utc.utc).astimezone(ZoneInfo(city_tz_name))
            current_hour = now_local.hour
        except Exception:
            return frozen_l1

        # 加载最新 METAR（当天 + 前一天，用于时间衰减窗口）
        metar_records = []
        for d in [target_date, _prev_date(target_date)]:
            mf = DATA_DIR / "metar" / city_key / f"{d}.json"
            if mf.exists():
                with open(mf) as f:
                    md = json.load(f)
                metar_records.extend(md.get("records", []))

        if not metar_records:
            return frozen_l1

        # 用最新 METAR 重算实时权重
        model_keys = eng.get("models_available", [])
        try:
            live_w, live_rmse, n_pts = compute_live_weights(
                hourly, metar_records, model_keys, city_tz_name
            )
        except Exception:
            return frozen_l1

        if not live_w or len(live_w) < 2:
            return frozen_l1

        # 合并：过去小时冻结，未来小时用最新权重
        merged = {}
        for h_data in hourly:
            h = h_data.get("hour")
            if h is None:
                continue
            if h < current_hour:
                if str(h) in frozen_l1_c:
                    merged[str(h)] = frozen_l1_c[str(h)]
            else:
                temps = h_data.get("temps", {})
                w_sum, w_total = 0.0, 0.0
                for m, w in live_w.items():
                    t = temps.get(m)
                    if t is not None and w > 0:
                        w_sum += t * w
                        w_total += w
                if w_total > 0:
                    merged[str(h)] = round(w_sum / w_total, 1)

        # 如果未来时段缺失，用冻结值补
        for h_str, v in frozen_l1_c.items():
            if h_str not in merged:
                merged[h_str] = v

        # ── 记录预测快照（用于回测预测-时间曲线） ──
        self._record_prediction_snapshot(
            city_key, target_date, merged, live_w, live_rmse,
            n_pts, current_hour, now_local.isoformat()
        )

        return merged

    def _record_prediction_snapshot(self, city_key, target_date, l1_curve,
                                     live_weights, live_rmse, metar_count,
                                     current_hour, now_iso):
        """追加预测快照到 snapshots.jsonl（桶变化或距上次≥30分钟时记录）。"""
        import os as _os
        
        # 从 L1 曲线提取预测最高温
        vals = [v for v in l1_curve.values() if isinstance(v, (int, float))]
        if not vals:
            return
        t_predicted = round(max(vals), 1)
        current_bucket = round(t_predicted)
        
        snap_dir = DATA_DIR / "engine" / city_key
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_path = snap_dir / f"{target_date}_snapshots.jsonl"
        
        # 检查是否需要记录（防重复）
        should_record = True
        if snap_path.exists():
            try:
                with open(snap_path) as f:
                    lines = f.readlines()
                if lines:
                    last = json.loads(lines[-1].strip())
                    last_bucket = last.get("bucket")
                    last_ts = last.get("time", "")
                    
                    # 桶没变 且 距上次不到30分钟 → 跳过
                    if last_bucket == current_bucket:
                        try:
                            last_dt = datetime.fromisoformat(last_ts)
                            now_dt = datetime.fromisoformat(now_iso)
                            delta_m = (now_dt - last_dt).total_seconds() / 60
                            if delta_m < 30:
                                should_record = False
                        except Exception:
                            pass
            except Exception:
                pass
        
        if not should_record:
            return
        
        snapshot = {
            "time": now_iso,
            "t_predicted": t_predicted,
            "bucket": current_bucket,
            "metar_count": metar_count,
            "current_hour": current_hour,
            "live_weights": live_weights,
            "live_rmse": live_rmse,
        }
        
        with open(snap_path, "a") as f:
            f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    def _get_metar(self, city_key, target_date=None):
        target_date = target_date or date.today().isoformat()
        if not city_key:
            return {"error": "missing city parameter"}

        mf = DATA_DIR / "metar" / city_key / f"{target_date}.json"
        if not mf.exists():
            return {"city": city_key, "date": target_date, "records": [], "n": 0}
        
        with open(mf) as f:
            data = json.load(f)
        
        # 获取城市时区
        import yaml
        with open(PROJECT_ROOT / "config" / "cities.yaml") as fcfg:
            config = yaml.safe_load(fcfg)
        city_tz = config.get("cities", {}).get(city_key, {}).get("tz", "UTC")
        try:
            tz = ZoneInfo(city_tz)
        except Exception:
            tz = ZoneInfo("UTC")
        
        def _convert_records(raw_records, data_meta=None):
            """UTC → 本地时间转换"""
            out = []
            for r in raw_records:
                rec = dict(r)
                utc_str = r.get("time_utc", "")
                if utc_str:
                    try:
                        dt_utc = datetime.strptime(utc_str, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("UTC"))
                        dt_local = dt_utc.astimezone(tz)
                        rec["time_local"] = dt_local.strftime("%Y-%m-%d %H:%M")
                        rec["hour_local"] = dt_local.hour
                        rec["hour_frac"] = dt_local.hour + dt_local.minute / 60
                    except Exception:
                        rec["time_local"] = utc_str
                        h = int(utc_str.split(" ")[1].split(":")[0]) if " " in utc_str else 0
                        rec["hour_local"] = h
                        rec["hour_frac"] = h
                out.append(rec)
            return out

        # 当天 METAR 记录
        all_records = _convert_records(data.get("records", []))
        
        # 前一天 METAR：跨时区补全（如北京 UTC 16:00-23:30 属于本地次日 00:00-07:30）
        from datetime import timedelta
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_mf = DATA_DIR / "metar" / city_key / f"{prev_date}.json"
        if prev_mf.exists():
            with open(prev_mf) as f:
                prev_data = json.load(f)
            prev_records = _convert_records(prev_data.get("records", []))
            # 只保留本地时间落在 target_date 的记录
            prev_records = [r for r in prev_records 
                           if r.get("time_local", "").startswith(target_date)]
            all_records.extend(prev_records)
        
        # 只保留本地时间在 target_date 的记录（排除当天文件中溢出的次日数据）
        records = [r for r in all_records 
                   if r.get("time_local", "").startswith(target_date)]
        records.sort(key=lambda r: r.get("time_local", ""))
        
        return {
            "city": city_key,
            "date": target_date,
            "n_records": data.get("n_records", 0),
            "t_max": data.get("t_max"),
            "t_min": data.get("t_min"),
            "t_avg": data.get("t_avg"),
            "fetch_count": data.get("fetch_count", 0),
            "records": records,
        }

    def log_message(self, format, *args):
        # 精简日志
        if "/api/" in (args[0] if args else ""):
            print(f"  {args[0]}")


def main():
    parser = argparse.ArgumentParser(description="Mercury 前端服务")
    parser.add_argument("--port", type=int, default=8080, help="端口号 (默认 8080)")
    args = parser.parse_args()

    # 确保 frontend 目录存在
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)

    server = HTTPServer(("0.0.0.0", args.port), MercuryHandler)
    print(f"Mercury 前端服务已启动: http://localhost:{args.port}")
    print(f"API: http://localhost:{args.port}/api/summary")
    print(f"按 Ctrl+C 停止")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
