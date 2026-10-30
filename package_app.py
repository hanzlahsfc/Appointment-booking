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
    "wheels",
    "requirements.txt",
    "config.yaml",
    "apk",
    "SETUP.bat",
    "START.bat",
    "DIAGNOSE.bat",
    "CLIENT-README.md",
    "HOW-TO-PAIR.md",
    "TELEGRAM-SETUP.md",
    "README-APK.md",
    "HANDOFF.md",
]

EXCLUDE_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "data",
    "dist", "node_modules", "recon", "captures",
}
EXCLUDE_SUFFIXES = {".pyc", ".db", ".sqlite", ".sqlite3", ".log", ".png",
                    ".zip", ".tar", ".gz", ".pem", ".key", ".env",
                    ".exe", ".dll", ".lnk"}


def _skip(path: Path) -> bool:
    parts = set(path.relative_to(ROOT).parts)
    if parts & EXCLUDE_PARTS:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    name = path.name.lower()
    if name.startswith(".env") or "secret" in name or "applicant" in name:
        return True
    if name in {"diagnose-log.txt", "app-token", "queue.db", "nul"}:
        return True
    return False


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    # Text files must carry CRLF line endings inside the ZIP — cmd.exe
    # misparses LF-only batch files (the window flashes and closes).
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            p = ROOT / item
            files = [p] if p.is_file() else [
                f for f in sorted(p.rglob("*"))
                if f.is_file() and not _skip(f)]
            for f in files:
                arc = str(f.relative_to(ROOT)).replace("\\", "/")
                if f.suffix.lower() in {".bat", ".cmd", ".txt", ".md"}:
                    text = f.read_bytes().replace(b"\r\n", b"\n")
                    zf.writestr(arc, text.replace(b"\n", b"\r\n"))
                else:
                    zf.write(f, arc)
                count += 1
    print(f"built {OUT} ({count} files, {OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
