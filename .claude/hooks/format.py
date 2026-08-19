#!/usr/bin/env python3
"""PostToolUse: 편집된 파이썬 파일을 ruff 로 포맷(있을 때만). 실패해도 무해."""
import sys
import json
import shutil
import subprocess


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    path = (data.get("tool_input") or {}).get("file_path", "")
    if not path.endswith(".py"):
        sys.exit(0)
    if shutil.which("ruff") is None:
        sys.exit(0)
    try:
        subprocess.run(["ruff", "format", path], capture_output=True, timeout=30)
        subprocess.run(["ruff", "check", "--fix", path], capture_output=True, timeout=30)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
