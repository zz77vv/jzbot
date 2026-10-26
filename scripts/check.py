"""Compile, prove V2 import provenance/side-effect freedom, run the complete suite."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(root=ROOT):
    root = Path(root).resolve(strict=True)
    commands = [
        [sys.executable, "-m", "compileall", "."],
        [
            sys.executable,
            "-c",
            "import sys;sys.path.insert(0," + repr(str(root)) + ");"
            "import jzbot,jzbot.app;from pathlib import Path;"
            "assert Path(jzbot.__file__).resolve()==Path("
            + repr(str(root / "jzbot" / "__init__.py"))
            + ");"
            'assert not hasattr(jzbot.app,"db");print("IMPORT_OK")',
        ],
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-m", "ruff", "check", "jzbot", "scripts"],
    ]
    for name, command in zip(("compile", "import", "pytest", "static"), commands):
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode:
            print(result.stdout)
            print(result.stderr)
            raise RuntimeError(name + " failed")
        print(
            name
            + ": "
            + (result.stdout.strip().splitlines()[-1] if name == "pytest" else "PASS"),
            flush=True,
        )


if __name__ == "__main__":
    validate()
