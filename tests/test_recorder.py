"""Tests for the recon recorder: redaction and endpoint distillation.

No browser needed — these cover the security-critical paths (nothing secret may
reach recon/har/*.har or recon/booking-flow.json) and the flow distillation.
"""
from __future__ import annotations

import json

from app.recorder import (
    RecorderConfig,
    TrafficRecorder,
    mask,
    parse_post_data,
    redact_headers,
    redact_url,
)


# ── masking ──────────────────────────────────────────────────────────────

def test_mask_hides_the_middle():
    out = mask("supersecretvalue")
    assert out.startswith("su") and out.endswith("ue")
    assert "supersecret" not in out
    assert "*" in out


def test_mask_short_value_is_all_stars():
    assert mask("abc") == "***"
    assert mask(None) == ""


# ── url redaction ────────────────────────────────────────────────────────

def test_redact_url_masks_token_but_keeps_other_params():
    url = "https://pk-gr-services.gvcworld.eu/slots?form_token=abcdef123456&page=2"
    out = redact_url(url)
    assert "abcdef123456" not in out
    assert "page=2" in out
    assert "form_token=" in out


def test_redact_url_without_query_is_unchanged():
    url = "https://pk-gr-services.gvcworld.eu/login/appointments/en?lang=en_US"
    assert redact_url(url) == url


# ── header redaction ─────────────────────────────────────────────────────

def test_redact_headers_masks_secrets_and_blanks_others():
    headers = {
        "Cookie": "incap_ses_7744=zzz",
        "Authorization": "Bearer sk-live-123",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-Unknown-Header": "whatever",
    }
    out = {h["name"]: h["value"] for h in redact_headers(headers)}
    assert "zzz" not in out["Cookie"]
    assert "sk-live-123" not in out["Authorization"]
    assert out["Content-Type"] == "application/json"
    assert out["X-Requested-With"] == "XMLHttpRequest"
    assert out["X-Unknown-Header"] == ""


# ── request body redaction ───────────────────────────────────────────────

def test_form_post_keeps_field_names_but_masks_secrets():
    body = "username=alice&password=hunter2&form_build_id=form-xyz&vac=lahore"
    mime, params, fields = parse_post_data(body, "application/x-www-form-urlencoded")

    assert mime == "application/x-www-form-urlencoded"
    names = {p["name"] for p in params}
    assert {"username", "password", "form_build_id", "vac"} <= names

    blob = json.dumps(fields)
    assert "hunter2" not in blob
    assert "form-xyz" not in blob
    assert fields["fields"]["username"] == "alice"
    assert fields["fields"]["vac"] == "lahore"


def test_json_post_masks_token_and_pii():
    body = json.dumps(
        {"csrf_token": "t0ken-value", "applicant": {"passport": "AB1234567", "city": "Lahore"}}
    )
    _, _, fields = parse_post_data(body, "application/json; charset=utf-8")

    blob = json.dumps(fields)
    assert "t0ken-value" not in blob
    assert "AB1234567" not in blob  # 'passport' matches the pass rule → PII protected
    assert fields["fields"]["applicant"]["city"] == "Lahore"


def test_empty_post_data_is_none():
    mime, params, fields = parse_post_data("", "application/json")
    assert mime is None and params == [] and fields == {}


# ── flow distillation + har ──────────────────────────────────────────────

def _entry(method, url, status, rtype, fields=None, resp_text=None, resp_mime="application/json"):
    return {
        "startedDateTime": "2026-10-26T00:00:00.000Z",
        "time": 0,
        "resourceType": rtype,
        "request": {
            "method": method,
            "url": url,
            "headers": [],
            "postData": {"mimeType": "application/json", "params": []} if fields else None,
            "fields": {"fields": fields} if fields else {},
        },
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": [],
            "content": {"mimeType": resp_mime, "size": len(resp_text or ""), "text": resp_text},
        },
    }


def test_write_flow_distils_endpoints(tmp_path):
    rec = TrafficRecorder(RecorderConfig(out_dir=tmp_path))
    rec.entries = [
        _entry("POST", "https://x.example/api/slots", 200, "xhr",
               fields={"vac": "lahore", "form_token": "ab****cd"},
               resp_text='{"slots": [], "total": 0}'),
        _entry("POST", "https://x.example/api/slots", 200, "xhr",
               fields={"vac": "islamabad", "form_token": "ef****gh"},
               resp_text='{"slots": [], "total": 0}'),
    ]

    path = rec.write_flow()
    flow = json.loads(path.read_text())

    assert flow["entry_count"] == 2
    endpoint = flow["endpoints"][0]
    assert endpoint["method"] == "POST"
    assert endpoint["url"].startswith("https://x.example/api/slots")
    assert endpoint["count"] == 2
    assert set(endpoint["requestFields"]) == {"vac", "form_token"}
    assert set(endpoint["responseShape"]) == {"slots", "total"}
    assert endpoint["statuses"] == [200]


def test_write_har_has_valid_envelope(tmp_path):
    rec = TrafficRecorder(RecorderConfig(out_dir=tmp_path))
    rec.entries = [_entry("GET", "https://x.example/a", 200, "document", resp_text="<html></html>", resp_mime="text/html")]

    path = rec.write_har()
    har = json.loads(path.read_text())

    assert har["log"]["version"] == "1.2"
    assert len(har["log"]["entries"]) == 1
    assert har["log"]["entries"][0]["response"]["status"] == 200
