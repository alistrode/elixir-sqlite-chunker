#!/usr/bin/env python3
"""Send Windows' Print Screen key, then save the current full desktop as a PNG."""
from __future__ import annotations

import argparse
import ctypes
import time
from pathlib import Path


VK_SNAPSHOT = 0x2C
KEYEVENTF_KEYUP = 0x0002


def main() -> int:
    parser = argparse.ArgumentParser(description="调用 Windows 全屏截图键并保存桌面截图。")
    parser.add_argument("--output", type=Path, default=Path("fullscreen.png"))
    args = parser.parse_args()

    user32 = ctypes.windll.user32
    user32.keybd_event(VK_SNAPSHOT, 0, 0, 0)
    user32.keybd_event(VK_SNAPSHOT, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.5)

    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise SystemExit("需要 Pillow 才能将全屏截图保存为 PNG") from exc
    image = ImageGrab.grab(all_screens=True)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
