#!/usr/bin/env python3
"""引擎自动运行 — 对今天日期跑 DEB 引擎，用于 launchd 定时任务。
等价于: python3 scripts/engine.py --all --date $(date +%Y-%m-%d) --deb --save
"""
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
today = date.today().isoformat()

cmd = [
    sys.executable,
    str(PROJECT_ROOT / "scripts" / "engine.py"),
    "--all",
    "--date", today,
    "--deb",
    "--save",
]

result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=PROJECT_ROOT)
if result.stdout:
    print(result.stdout.strip())
if result.stderr:
    print(result.stderr.strip(), file=sys.stderr)
sys.exit(result.returncode)
