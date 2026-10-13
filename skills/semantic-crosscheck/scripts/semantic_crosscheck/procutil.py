"""Locate external programs (node, npm, java) without trusting the current directory.

shutil.which() on Windows searches the current working directory before PATH, so a file named node.exe or
npm.cmd planted in the directory the user runs from would be executed. This lookup uses only absolute PATH
entries, in order, and on Windows tries the PATHEXT extensions.
"""

import os
import sys
from pathlib import Path


def which(name: str) -> str | None:
    """Absolute path of the first executable called `name` on PATH, ignoring empty and relative entries."""
    if os.path.dirname(name):
        p = Path(name)
        return str(p) if p.is_absolute() and _executable(p) else None
    exts = [""]
    if sys.platform == "win32":
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        if Path(name).suffix.lower() in exts:
            exts = [""]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        for ext in exts:
            p = Path(entry) / (name + ext)
            if _executable(p):
                return str(p)
    return None


def _executable(p: Path) -> bool:
    return p.is_file() and (sys.platform == "win32" or os.access(p, os.X_OK))
