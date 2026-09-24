#!/usr/bin/env python3
"""Bounded command bridge to the operator's existing Chrome Vast session.

This is deliberately not a general browser-launcher: every operation first
selects an already-open ``https://cloud.vast.ai/`` tab.  It never creates a
browser profile or authenticates, so a paid run can only proceed through the
operator's existing desktop session.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


VAST_ORIGIN = "https://cloud.vast.ai/"


class AdapterError(RuntimeError):
    pass


def apple(script: str, *arguments: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/osascript", "-l", "AppleScript", "-e", script, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode:
        raise AdapterError("Chrome desktop automation failed")
    return completed.stdout.rstrip("\n")


SELECT_VAST_TAB = '''
on run argv
  tell application "Google Chrome"
    repeat with w in windows
      set i to 0
      repeat with t in tabs of w
        set i to i + 1
        if (URL of t starts with "https://cloud.vast.ai/") then
          set index of w to 1
          set active tab index of front window to i
          return "selected"
        end if
      end repeat
    end repeat
  end tell
  error "No existing Vast console tab is open"
end run
'''


def select_vast_tab() -> None:
    apple(SELECT_VAST_TAB)


def current_url() -> str:
    select_vast_tab()
    return apple('''
tell application "Google Chrome"
  return URL of active tab of front window
end tell
''')


def page_text() -> str:
    select_vast_tab()
    return apple('''
on run argv
  tell application "Google Chrome"
    return execute active tab of front window javascript "document.body ? document.body.innerText : ''"
  end tell
end run
''')


def goto(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "cloud.vast.ai" or parsed.query or parsed.fragment:
        raise AdapterError("refusing non-Vast navigation")
    select_vast_tab()
    apple('''
on run argv
  tell application "Google Chrome"
    set URL of active tab of front window to item 1 of argv
  end tell
end run
''', url)


def run_javascript(source: str) -> str:
    select_vast_tab()
    return apple('''
on run argv
  tell application "Google Chrome"
    return execute active tab of front window javascript (item 1 of argv)
  end tell
end run
''', source)


def screenshot(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    select_vast_tab()
    bounds = apple('''
tell application "Google Chrome"
  return bounds of front window
end tell
''')
    try:
        left, top, right, bottom = (int(part.strip()) for part in bounds.split(","))
    except ValueError as error:
        raise AdapterError("Chrome window bounds are unavailable") from error
    if right <= left or bottom <= top:
        raise AdapterError("Chrome window bounds are invalid")
    completed = subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-o", "-R", f"{left},{top},{right - left},{bottom - top}", str(destination)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode or not destination.is_file() or destination.stat().st_size == 0:
        raise AdapterError("Chrome screenshot capture failed")


def main(arguments: list[str]) -> int:
    if not arguments:
        raise AdapterError("missing browser command")
    command, *rest = arguments
    if command == "goto" and len(rest) == 1:
        goto(rest[0])
    elif command == "url" and not rest:
        print(current_url())
    elif command == "text" and not rest:
        print(page_text())
    elif command == "js" and len(rest) == 1:
        print(run_javascript(rest[0]))
    elif command == "screenshot" and len(rest) == 1:
        screenshot(Path(rest[0]).expanduser().resolve())
    elif command == "wait" and len(rest) == 1 and rest[0].startswith("text="):
        expected = rest[0][5:]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if expected in page_text():
                return 0
            time.sleep(0.5)
        raise AdapterError("Chrome page did not reach the expected text")
    else:
        raise AdapterError("unsupported browser command")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (AdapterError, subprocess.TimeoutExpired):
        raise SystemExit("desktop browser adapter refused or failed safely")
