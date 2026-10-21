"""Runs `igothired run` on a schedule with launchd (macOS)."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .paths import LOG_DIR, REPO_ROOT, ensure_home, search_path

LABEL = "com.igothired.run"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def install(times: list[tuple[int, int]], weekdays: list[int]) -> Path:
    ensure_home()
    intervals = [
        {"Weekday": day + 1, "Hour": hour, "Minute": minute}  # launchd counts Monday as 1
        for day in weekdays
        for hour, minute in times
    ]
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "igothired", "run", "--quiet"],
        "WorkingDirectory": str(REPO_ROOT),
        "StartCalendarInterval": intervals,
        "EnvironmentVariables": {"PATH": search_path()},
        "StandardOutPath": str(LOG_DIR / "scheduled.log"),
        "StandardErrorPath": str(LOG_DIR / "scheduled.log"),
        "ProcessType": "Background",
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    remove()
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True, capture_output=True)
    return PLIST


def remove() -> bool:
    if not PLIST.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    PLIST.unlink()
    return True


def installed() -> bool:
    return PLIST.exists()
