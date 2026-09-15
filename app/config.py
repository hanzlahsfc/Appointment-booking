"""Config + env loading and validation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class AppConfig:
    poll_interval_seconds: int = 300
    jitter_seconds: int = 30
    concurrency: int = 4
    timezone: str = "Asia/Karachi"
    dry_run: bool = False
    headless: bool = True


@dataclass
class ProfileConfig:
    name: str
    email: str
    category: str
    location: str
    booking_url: str
    username_env: str = "USERNAME"
    password_env: str = "PASSWORD"


@dataclass
class NotificationsConfig:
    resend_api_key_env: str = "RESEND_API_KEY"
    from_addr: str = "GVCW Slot Alert <oncall@yourdomain.com>"
    to: list[str] = field(default_factory=list)


@dataclass
class Config:
    app: AppConfig
    profiles: list[ProfileConfig]
    notifications: NotificationsConfig
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    state_dir: Path = field(default_factory=lambda: ROOT / "state")

    @property
    def creds_dir(self) -> Path:
        return self.data_dir / "creds"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "slot_state.db"


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text()) or {}

    app_raw = raw.get("app", {}) or {}
    notif_raw = raw.get("notifications", {}) or {}

    app = AppConfig(
        poll_interval_seconds=int(app_raw.get("poll_interval_seconds", 300)),
        jitter_seconds=int(app_raw.get("jitter_seconds", 30)),
        concurrency=int(app_raw.get("concurrency", 4)),
        timezone=app_raw.get("timezone", "Asia/Karachi"),
        dry_run=_parse_bool(app_raw.get("dry_run", False), False),
        headless=_parse_bool(app_raw.get("headless", True), True),
    )

    default_username_env = app_raw.get("username_env", "USERNAME")
    default_password_env = app_raw.get("password_env", "PASSWORD")

    profiles = [
        ProfileConfig(
            name=str(p.get("name", f"profile-{i}")),
            email=str(p["email"]),
            category=str(p.get("category", "unknown")),
            location=str(p.get("location", "unknown")),
            booking_url=str(
                p.get(
                    "booking_url",
                    "https://pk-gr-services.gvcworld.eu/login/appointments",
                )
            ),
            username_env=str(p.get("username_env", default_username_env)),
            password_env=str(p.get("password_env", default_password_env)),
        )
        for i, p in enumerate(raw.get("profiles", []) or [])
    ]

    notifications = NotificationsConfig(
        resend_api_key_env=str(notif_raw.get("resend_api_key_env", "RESEND_API_KEY")),
        from_addr=str(
            os.getenv("RESEND_FROM", notif_raw.get("from", "GVCW Slot Alert <oncall@yourdomain.com>"))
        ),
        to=list(notif_raw.get("to", []) or []),
    )

    cfg = Config(
        app=app,
        profiles=profiles,
        notifications=notifications,
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.creds_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg