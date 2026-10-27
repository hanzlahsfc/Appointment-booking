"""Build the client ZIP: Appointment-booker-client.zip

Includes the app code, extension, requirements, .bat files, docs —
excludes secrets, data, .git, browser profiles, caches, dev artifacts.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "Appointment-booker-client.zip"

INCLUDE = [
    "app",
    "extension",
    "requirements.txt",
    "SETUP.bat",
    "START.bat",
    "CLIENT-README.md",
    "HANDOFF.md",
]

EXCLUDE_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "data",
    "dist", "node_modules", "recon", "captures",
}
EXCLUDE_SUFFIXES = {".pyc", ".db", ".sqlite", ".sqlite3", ".log", ".png",
                    ".zip", ".tar", ".gz", ".pem", ".key", ".env"}


def _skip(path: Path) -> bool:
    parts = set(path.relative_to(ROOT).parts)
    if parts & EXCLUDE_PARTS:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    name = path.name.lower()
    if name.startswith(".env") or "secret" in name or "applicant" in name:
        return True
    return False


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            p = ROOT / item
            if p.is_file():
                zf.write(p, item); count += 1
                continue
            for f in sorted(p.rglob("*")):
                if f.is_file() and not _skip(f):
                    zf.write(f, f.relative_to(ROOT)); count += 1
    print(f"built {OUT} ({count} files, {OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
