#!/usr/bin/env python3
"""Mercury — MAE 追踪更新：新 METAR 到来后更新逐模型逐城 MAE 和偏差

用法:
    python3 scripts/update_mae.py --city beijing --date 2026-05-31
    python3 scripts/update_mae.py --all --date 2026-05-31
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
MAE_SEED_PATH = PROJECT_ROOT / "data" / "models" / "mae_seed.json"


def load_forecast_tmax(city_key: str, target_date: str) -> dict[str, float] | None:
    """读取各模型日最高温预测"""
    path = PROJECT_ROOT / "data" / "forecasts" / city_key / target_date / "latest.json"
    if not path.exists():
        return None
    with open(path) as f:
        fc = json.load(f)
    return fc.get("t_max", {})


def load_metar_tmax(city_key: str, target_date: str) -> float | None:
    """读取实测日最高温"""
    path = PROJECT_ROOT / "data" / "metar" / city_key / f"{target_date}.json"
    if not path.exists():
        return None
    with open(path) as f:
        metar = json.load(f)
    return metar.get("t_max")


def load_mae_db() -> dict:
    """加载 MAE 数据库"""
    if not MAE_SEED_PATH.exists():
        return {"cities": {}, "bias": {}, "generated_at": "", "source": "empty"}
    with open(MAE_SEED_PATH) as f:
        return json.load(f)


def save_mae_db(db: dict):
    """保存 MAE 数据库"""
    MAE_SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MAE_SEED_PATH, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def update_city(db: dict, city_key: str, target_date: str) -> dict | None:
    """更新单个城市的 MAE 和偏差
    
    MAE 更新公式（指数移动平均）：
        MAE_new = α · |error| + (1-α) · MAE_old
        其中 α = 1 / (n + 1)，n 是已有样本数
    
    Bias 更新公式（简单累积）：
        Bias_new = (n · Bias_old + error) / (n + 1)
    """
    t_max_models = load_forecast_tmax(city_key, target_date)
    actual = load_metar_tmax(city_key, target_date)

    if not t_max_models or actual is None:
        return None

    cities = db.setdefault("cities", {})
    bias_db = db.setdefault("bias", {})

    city_mae = cities.get(city_key, {})
    old_bias = bias_db.get(city_key, 0.0)
    n_samples = city_mae.get("_n_samples", 0)

    errors = {}
    for model, t_forecast in t_max_models.items():
        error = t_forecast - actual
        errors[model] = round(error, 1)

    # MAE 更新
    alpha = 1.0 / (n_samples + 1)
    new_mae = {}
    for model, error in errors.items():
        abs_err = abs(error)
        old_mae = city_mae.get(model, None)
        if old_mae is not None:
            new_mae[model] = round((1 - alpha) * old_mae + alpha * abs_err, 2)
        else:
            new_mae[model] = round(abs_err, 2)

    new_mae["_n_samples"] = n_samples + 1
    cities[city_key] = new_mae

    # Bias 更新
    ensemble_error = sum(errors.values()) / len(errors)
    new_bias = (n_samples * old_bias + ensemble_error) / (n_samples + 1)
    bias_db[city_key] = round(new_bias, 2)

    db["generated_at"] = datetime.now(timezone.utc).isoformat()
    db["source"] = f"polywx-bootstrap + {n_samples + 1} Mercury obs"

    return {
        "city": city_key,
        "date": target_date,
        "n_samples": n_samples + 1,
        "errors": errors,
        "mae": new_mae,
        "bias": new_bias,
    }


def main():
    parser = argparse.ArgumentParser(description="更新 MAE 追踪数据")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", type=str, help="城市 key")
    group.add_argument("--all", action="store_true", help="所有有数据城市")
    parser.add_argument("--date", type=str, required=True, help="目标日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不保存")
    args = parser.parse_args()

    if args.city:
        targets = [args.city]
    else:
        # 找所有有 forecast 的城市
        fc_dir = PROJECT_ROOT / "data" / "forecasts"
        if fc_dir.exists():
            targets = [d.name for d in fc_dir.iterdir() if d.is_dir()]
        else:
            targets = []

    db = load_mae_db()
    results = []

    for city_key in sorted(targets):
        result = update_city(db, city_key, args.date)
        if result is None:
            continue
        results.append(result)

        old_mae = sum(v for k, v in result["mae"].items() if k != "_n_samples") / max(1, len(result["mae"]) - 1)
        print(f"  {city_key:16s} n={result['n_samples']:>3d}  "
              f"avg_mae={old_mae:.2f}°C  bias={result['bias']:+.2f}°C")

    if not results:
        print("⚠️ 没有可更新的配对数据。请确保 forecasts 和 metar 目录都有数据。")
        return

    if not args.dry_run:
        save_mae_db(db)
        print(f"\n✅ 已更新 {MAE_SEED_PATH} ({len(results)} 城)")
    else:
        print(f"\n🔍 预览模式 — 未保存 ({len(results)} 城)")


if __name__ == "__main__":
    main()
