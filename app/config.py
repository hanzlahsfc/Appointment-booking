"""Config + env loading and validation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


VALID_DRIVERS = ("bridge", "playwright")


@dataclass
class AppConfig:
    poll_interval_seconds: int = 300
    jitter_seconds: int = 30
    concurrency: int = 4
    timezone: str = "Asia/Karachi"
    dry_run: bool = False
    headless: bool = True
    # "bridge": drive the operator's signed-in browser via the extension.
    # "playwright": launch our own Chromium (storage_state login fallback).
    driver: str = "bridge"


VALID_BOOKING_MODES = ("off", "assist", "auto")


@dataclass
class BookingConfig:
    """Auto-booking settings. Every default is off/dry on purpose: a mistaken
    automatic booking costs a real appointment slot."""

    mode: str = "off"  # off | assist | auto
    dry_run: bool = True  # log the whole flow, never click reserve
    alarm_seconds: int = 120
    headless: bool = False  # assist needs a visible window to take over
    max_attempts_per_profile_per_hour: int = 4
    reserve_timeout_seconds: int = 120
    evidence_dir: Path = field(default_factory=lambda: ROOT / "data" / "evidence")
    profile_dir: Path = field(default_factory=lambda: ROOT / "data" / "chrome-profile")
    selectors_file: Path = field(default_factory=lambda: ROOT / "recon" / "booking-selectors.json")

    @property
    def enabled(self) -> bool:
        return self.mode in ("assist", "auto")


@dataclass
class ProfileConfig:
    name: str
    email: str
    category: str
    location: str
    booking_url: str
    username_env: str = "USERNAME"
    password_env: str = "PASSWORD"
    auto_book: bool = False
    applicant_file: str | None = None
    prefer: dict[str, Any] = field(default_factory=dict)
    # In bridge mode: pin this profile to a specific Chrome tab id. When unset
    # the worker drives whichever tab the operator picked in the popup.
    tab_id: int | None = None


@dataclass
class HostedConfig:
    """Hosted-client layer: remote browser sessions for provisioned accounts.

    Everything here defaults to the safe option: the mock portal is only ever
    reachable when allow_mock_portal is set explicitly, and browsers are
    headless so a hosted session can never surface a desktop window.
    """

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8081
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "hosted.db")
    browsers_dir: Path = field(default_factory=lambda: ROOT / "data" / "hosted-browsers")
    session_ttl_hours: int = 12
    # Idle manual-control sessions hand the browser back to automation.
    manual_idle_seconds: int = 900
    poll_interval_seconds: int = 30
    headless: bool = True
    # Test-only allowance: without this, provisioning or navigating to the mock
    # portal is refused even if a stale client row still lists its origin.
    allow_mock_portal: bool = False
    mock_portal_url: str = "http://127.0.0.1:8099"
    screen_quality: int = 60
    screen_max_width: int = 1366
    screen_max_height: int = 900
    max_input_events_per_second: int = 60
    viewport_width: int = 1366
    viewport_height: int = 900
    # Origins allowed to call the API cross-origin (the Vercel dashboard).
    # Empty = same-origin only. Auth is Bearer-token, so CORS never carries
    # credentials — the header is explicit on every request.
    dashboard_origins: list[str] = field(default_factory=list)


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
    booking: BookingConfig = field(default_factory=BookingConfig)
    hosted: HostedConfig = field(default_factory=HostedConfig)
    proxy: str | None = None  # e.g. socks5://user:pass@host:1080 or http://host:3128
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    state_dir: Path = field(default_factory=lambda: ROOT / "state")

    @property
    def creds_dir(self) -> Path:
        return self.data_dir / "creds"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "slot_state.db"

    @property
    def headless(self) -> bool:
        """Assist/auto need a visible window so a human can finish the form."""
        if self.booking.enabled:
            return self.booking.headless
        return self.app.headless

    def profile_by_name(self, name: str) -> ProfileConfig | None:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def booking_enabled_for(self, profile_name: str) -> bool:
        """Master mode on AND this profile explicitly opted in."""
        if not self.booking.enabled:
            return False
        profile = self.profile_by_name(profile_name)
        return bool(profile and profile.auto_book)


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_booking(raw: dict[str, Any], root: Path) -> BookingConfig:
    book_raw = raw.get("booking", {}) or {}
    mode = str(book_raw.get("mode", "off")).strip().lower()
    if mode not in VALID_BOOKING_MODES:
        raise ValueError(f"booking.mode must be one of {VALID_BOOKING_MODES}, got {mode!r}")
    return BookingConfig(
        mode=mode,
        dry_run=_parse_bool(book_raw.get("dry_run", True), True),
        alarm_seconds=int(book_raw.get("alarm_seconds", 120)),
        headless=_parse_bool(book_raw.get("headless", False), False),
        max_attempts_per_profile_per_hour=int(
            book_raw.get("max_attempts_per_profile_per_hour", 4)
        ),
        reserve_timeout_seconds=int(book_raw.get("reserve_timeout_seconds", 120)),
        evidence_dir=Path(book_raw.get("evidence_dir", root / "data" / "evidence")),
        profile_dir=Path(book_raw.get("profile_dir", root / "data" / "chrome-profile")),
        selectors_file=Path(
            book_raw.get("selectors_file", root / "recon" / "booking-selectors.json")
        ),
    )


def _parse_hosted(raw: dict[str, Any], root: Path) -> HostedConfig:
    h = raw.get("hosted", {}) or {}
    return HostedConfig(
        enabled=_parse_bool(h.get("enabled", False), False),
        bind=str(h.get("bind", "127.0.0.1")),
        port=int(h.get("port", 8081)),
        db_path=Path(h.get("db_path", root / "data" / "hosted.db")),
        browsers_dir=Path(h.get("browsers_dir", root / "data" / "hosted-browsers")),
        session_ttl_hours=int(h.get("session_ttl_hours", 12)),
        manual_idle_seconds=int(h.get("manual_idle_seconds", 900)),
        poll_interval_seconds=int(h.get("poll_interval_seconds", 30)),
        headless=_parse_bool(h.get("headless", True), True),
        allow_mock_portal=_parse_bool(h.get("allow_mock_portal", False), False),
        mock_portal_url=str(h.get("mock_portal_url", "http://127.0.0.1:8099")).rstrip("/"),
        screen_quality=int(h.get("screen_quality", 60)),
        screen_max_width=int(h.get("screen_max_width", 1366)),
        screen_max_height=int(h.get("screen_max_height", 900)),
        max_input_events_per_second=int(h.get("max_input_events_per_second", 60)),
        viewport_width=int(h.get("viewport_width", 1366)),
        viewport_height=int(h.get("viewport_height", 900)),
        dashboard_origins=[
            str(o).rstrip("/")
            for o in (h.get("dashboard_origins", []) or [])
        ],
    )


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text()) or {}

    app_raw = raw.get("app", {}) or {}
    notif_raw = raw.get("notifications", {}) or {}

    driver = str(app_raw.get("driver", "bridge")).strip().lower()
    if driver not in VALID_DRIVERS:
        raise ValueError(f"app.driver must be one of {VALID_DRIVERS}, got {driver!r}")

    app = AppConfig(
        poll_interval_seconds=int(app_raw.get("poll_interval_seconds", 300)),
        jitter_seconds=int(app_raw.get("jitter_seconds", 30)),
        concurrency=int(app_raw.get("concurrency", 4)),
        timezone=app_raw.get("timezone", "Asia/Karachi"),
        dry_run=_parse_bool(app_raw.get("dry_run", False), False),
        headless=_parse_bool(app_raw.get("headless", True), True),
        driver=driver,
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
            auto_book=_parse_bool(p.get("auto_book", False), False),
            applicant_file=(str(p["applicant_file"]) if p.get("applicant_file") else None),
            prefer=dict(p.get("prefer", {}) or {}),
            tab_id=(int(p["tab_id"]) if p.get("tab_id") is not None else None),
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
        booking=_parse_booking(app_raw, ROOT),
        hosted=_parse_hosted(raw, ROOT),
        proxy=(raw.get("network", {}) or {}).get("proxy"),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.creds_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg