"""Explicit entry also supports embedded Windows Python with an isolated ._pth."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jzbot.app import main

if __name__ == "__main__":
    main()
