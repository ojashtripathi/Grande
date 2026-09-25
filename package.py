"""Build a zip someone can extract and run.

    python package.py

Produces ``dist/Grande-<version>.zip`` containing the source, the launchers and
the docs — everything needed to double-click ``run.bat``. Caches, the virtual
environment, the git history and previously built archives are left out.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).parent
DIST = ROOT / "dist"

#: Directories never worth shipping.
SKIP_DIRS = {
    ".git", ".venv", "venv", "dist", "build", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".idea", ".vscode",
}
#: Files never worth shipping.
SKIP_SUFFIXES = {".pyc", ".pyo", ".duckdb", ".wal", ".zip"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}


def version() -> str:
    text = (ROOT / "src" / "grande" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    return "0.0.0"


def wanted(path: pathlib.Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.name in SKIP_NAMES or path.suffix.lower() in SKIP_SUFFIXES:
        return False
    return path.is_file()


#: Nothing in the archive may identify a person or a machine. A working note once
#: shipped with its author's home-folder paths in it; the build now refuses.
PERSONAL = [
    re.compile(r"\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'`<>]+", re.IGNORECASE),  # a Windows profile folder
    re.compile(r"/(?:home|Users)/[^/\s\"'`<>]+/"),                            # a macOS or Linux home folder
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]*[A-Za-z]{2,}\b"),                   # an email address
]


def personal_patterns() -> list[re.Pattern[str]]:
    """The generic patterns, plus this machine's own user and computer names."""
    patterns = list(PERSONAL)
    for value in {os.environ.get(k, "") for k in ("USERNAME", "USER", "LOGNAME", "COMPUTERNAME")}:
        if len(value) >= 4:
            patterns.append(re.compile(rf"(?<![\w-]){re.escape(value)}(?![\w-])", re.IGNORECASE))
    return patterns


def personal_lines(files: list[pathlib.Path], root: pathlib.Path = ROOT) -> list[str]:
    """Every line, in any text file, that looks like personal information."""
    patterns = personal_patterns()
    found = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not text
        for number, line in enumerate(text.splitlines(), start=1):
            if any(p.search(line) for p in patterns):
                found.append(f"{path.relative_to(root)}:{number}: {line.strip()[:100]}")
    return found


def main() -> int:
    DIST.mkdir(exist_ok=True)
    name = f"Grande-{version()}"
    target = DIST / f"{name}.zip"

    files = sorted(p for p in ROOT.rglob("*") if wanted(p))
    if not files:
        print("Nothing to package.", file=sys.stderr)
        return 1

    leaks = personal_lines(files)
    if leaks:
        print("Not building: these lines would put personal information in the zip.",
              file=sys.stderr)
        for leak in leaks:
            print(f"  {leak}", file=sys.stderr)
        return 1

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            # Everything sits under one folder, so extracting never scatters
            # files across the user's Downloads.
            archive.write(path, pathlib.Path(name) / path.relative_to(ROOT))

    size = target.stat().st_size
    print(f"{target}")
    print(f"  {len(files)} files, {size / 1024:.0f} KB")
    print()
    print("  To use it: extract the zip, open the folder, double-click run.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
