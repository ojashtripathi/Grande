"""The zip that gets handed to people must carry nothing personal."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _package():
    spec = importlib.util.spec_from_file_location("package", ROOT / "package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nothing_that_would_be_shipped_is_personal():
    """Regression: BUILD-NOTES.md shipped in a zip with its author's home paths."""
    package = _package()
    files = sorted(p for p in ROOT.rglob("*") if package.wanted(p))
    assert package.personal_lines(files) == []


def test_personal_information_is_caught(tmp_path, monkeypatch):
    package = _package()
    monkeypatch.setenv("USERNAME", "zzplanted")
    # Built at run time, so this file itself holds no such path or address.
    samples = {
        "windows.md": "kept at C:" + "/Users/" + "someone/Documents/notes",
        "backslash.txt": "D:" + "\\Users\\" + "someone\\Desktop",
        "mac.sh": "cd /Users" + "/someone/code",
        "linux.py": "PATH = '/home" + "/someone/venv'",
        "mail.md": "contact: someone" + "@" + "example.com",
        "user.md": "built by zzplanted on Tuesday",
    }
    for name, text in samples.items():
        (tmp_path / name).write_text(f"fine line\n{text}\n", encoding="utf-8")
    (tmp_path / "clean.md").write_text("C:\\exports\\sales.csv and ~/AppData are fine\n", encoding="utf-8")

    found = package.personal_lines(sorted(tmp_path.iterdir()), root=tmp_path)
    assert sorted(line.split(":")[0] for line in found) == sorted(samples)
