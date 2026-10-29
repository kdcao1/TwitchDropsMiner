from __future__ import annotations

import re
import sys
import shutil
import subprocess


TITLE_PATTERN = re.compile(r"(?:^|\s[-–—|]\s)Twitch(?:\s|$)")
EXCLUDED = ("Drops Miner",)


def _matches(title: str) -> bool:
    return bool(TITLE_PATTERN.search(title)) and not any(e in title for e in EXCLUDED)


def _windows_titles() -> list[str]:
    import win32gui

    titles: list[str] = []

    def callback(hwnd: int, _: None) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                titles.append(title)
        return True

    win32gui.EnumWindows(callback, None)
    return titles


def _linux_titles() -> list[str]:
    if shutil.which("wmctrl"):
        out = subprocess.run(
            ["wmctrl", "-l"], capture_output=True, text=True, timeout=5
        ).stdout
        return [line.split(None, 3)[3] for line in out.splitlines() if len(line.split(None, 3)) == 4]
    if shutil.which("xdotool"):
        out = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "Twitch", "getwindowname", "%@"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return out.splitlines()
    return []


def _macos_titles() -> list[str]:
    script = (
        'tell application "System Events" to get name of every window of '
        "(every process whose background only is false)"
    )
    out = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=5
    ).stdout
    return [t.strip() for t in out.split(",")]


def user_watching_twitch() -> bool:
    try:
        if sys.platform == "win32":
            titles = _windows_titles()
        elif sys.platform == "darwin":
            titles = _macos_titles()
        else:
            titles = _linux_titles()
    except Exception:
        return False
    return any(_matches(t) for t in titles)
