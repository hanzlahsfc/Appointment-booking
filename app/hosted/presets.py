"""Provisioning presets: the field bundles `add-client --preset` fills in.

`mock` is the only preset today: it points a client at the local fake portal
(tests/fake_portal) with selectors that match its DOM. It exists so the hosted
flow can be exercised end to end without touching a real visa portal — and it
is refused unless hosted.allow_mock_portal is set, so a mock client can never
be created by accident on a production build.
"""
from __future__ import annotations

from typing import Any

MOCK_SELECTORS: dict[str, Any] = {
    "vac_select": "select[name='vac']",
    "category_select": "select[name='category']",
    "slot_row": ".slot-row",
    "reserve_button": "button.reserve",
    "applicant_fields": {
        "passport": "#edit-passport",
        "fullname": "#edit-fullname",
    },
    "submit_button": "#edit-submit",
    "confirmation_marker": "#confirmation",
    "token_inputs": ["form_build_id", "form_token", "form_id"],
}


def mock_preset(portal_url: str, username: str) -> dict[str, Any]:
    """Client fields for a mock-portal account (test-only)."""
    base = portal_url.rstrip("/")
    return {
        "profile_name": f"client:{username}",
        "booking_url": f"{base}/en/appointments",
        "category": "national_d_employment",
        "location": "lahore",
        "allowed_origins": [base],
        "selectors": dict(MOCK_SELECTORS),
        "auto_book": True,
    }
