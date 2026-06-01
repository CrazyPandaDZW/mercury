#!/usr/bin/env python3
"""Mercury 每日结算管线 — 拉 METAR → 更新 MAE → 引擎评估 → 输出报告

用法:
    python3 scripts/daily_settle.py --date 2026-05-31
    python3 scripts/daily_settle.py --date 2026-05-31 --dry-run
"""

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    """运行命令，返回 (exit_code, output)"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=PROJECT_ROOT)
        return r.returncode, r.stdout.strip()[-500:] if r.stdout else ""
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, str(e)


def main():
    parser = argparse.ArgumentParser(description="Mercury 每日结算")
    parser.add_argument("--date", type=str, help="目标日期 YYYY-MM-DD (默认今天)")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不更新 MAE")
    args = parser.parse_args()

    target_date = args.date or date.today().isoformat()
    print(f"Mercury 每日结算 — {target_date}")
    print("=" * 60)

    # Step 1: 拉取 METAR
    print("\n[1/4] 拉取 METAR 实测...")
    code, out = run(["python3", str(SCRIPTS / "fetch_metar.py"), "--all", "--date", target_date], timeout=120)
    n_ok = out.count("✅")
    n_fail = out.count("❌")
    print(f"  成功: {n_ok}, 失败: {n_fail}")

    # Step 2: 更新 MAE
    if not args.dry_run:
        print("\n[2/4] 更新 MAE...")
        code, out = run(["python3", str(SCRIPTS / "update_mae.py"), "--all", "--date", target_date])
        print(out[:300] if out else "  完成")
    else:
        print("\n[2/4] 更新 MAE (跳过, dry-run)")

    # Step 3: 运行引擎
    print("\n[3/4] DEB 引擎...")
    code, out = run(["python3", str(SCRIPTS / "engine.py"), "--all", "--date", target_date, "--deb", "--save"], timeout=60)
    # 统计
    lines = [l for l in out.split("\n") if "[DEB]" in l and "err=" in l]
    n_with_metar = len(lines)
    if lines:
        errors = []
        for l in lines:
            # 提取 err= 值
            if "err=" in l:
                err_part = l.split("err=")[1].split("°C")[0]
                try:
                    errors.append(float(err_part))
                except:
                    pass
        if errors:
            mae = sum(abs(e) for e in errors) / len(errors)
            bias = sum(errors) / len(errors)
            print(f"  有实测: {n_with_metar}城, MAE={mae:.2f}°C, bias={bias:+.2f}°C")
        else:
            print(f"  有实测: {n_with_metar}城")

    # Step 4: 评估报告
    print("\n[4/4] 评估报告...")
    code, out = run(["python3", str(SCRIPTS / "evaluate.py"), "--all"], timeout=30)
    print(out[:800] if out else "  无数据")

    print(f"\n{'='*60}")
    print(f"结算完成: {target_date}")


if __name__ == "__main__":
    main()
