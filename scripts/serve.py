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
from datetime import date
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = Path(__file__).parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = PROJECT_ROOT / "data"


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

        return {
            "city": city_key,
            "date": target_date,
            "engine": eng,
            "hourly": hourly,
        }

    def _get_metar(self, city_key, target_date=None):
        target_date = target_date or date.today().isoformat()
        if not city_key:
            return {"error": "missing city parameter"}

        mf = DATA_DIR / "metar" / city_key / f"{target_date}.json"
        if not mf.exists():
            return {"city": city_key, "date": target_date, "records": [], "n": 0}
        
        with open(mf) as f:
            data = json.load(f)
        
        return {
            "city": city_key,
            "date": target_date,
            "n_records": data.get("n_records", 0),
            "t_max": data.get("t_max"),
            "t_min": data.get("t_min"),
            "t_avg": data.get("t_avg"),
            "fetch_count": data.get("fetch_count", 0),
            "records": data.get("records", []),
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
