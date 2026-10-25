"""Local alarm for a claimed slot.

Runs the beep in a background thread so it never blocks the asyncio loop, and
keeps repeating for a configurable window: a single beep is easy to miss, and a
missed slot is an appointment lost.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:  # Windows only
    import winsound  # type: ignore
except ImportError:  # pragma: no cover - platform dependent
    winsound = None


@dataclass
class AlarmConfig:
    seconds: int = 120
    beep_hz: int = 1000
    beep_ms: int = 500
    gap_seconds: float = 1.0


class Alarm:
    """Repeating, non-blocking, dismissible alarm."""

    def __init__(self, cfg: AlarmConfig | None = None):
        self.cfg = cfg or AlarmConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._message: str | None = None
        self._started_at: float | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def message(self) -> str | None:
        return self._message

    @property
    def elapsed(self) -> float:
        return 0.0 if self._started_at is None else time.time() - self._started_at

    def start(self, message: str) -> None:
        self.stop()
        self._message = message
        self._started_at = time.time()
        self._stop.clear()
        logger.warning("ALARM: %s", message)
        self._thread = threading.Thread(target=self._run, name="gvcw-alarm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        deadline = time.time() + self.cfg.seconds
        while not self._stop.is_set() and time.time() < deadline:
            self._beep()
            if self._stop.wait(self.cfg.gap_seconds):
                return

    def _beep(self) -> None:
        try:
            if winsound is not None:
                winsound.Beep(self.cfg.beep_hz, self.cfg.beep_ms)
            else:  # POSIX fallback: terminal bell
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            logger.debug("beep failed", exc_info=True)
