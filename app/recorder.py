"""Traffic recorder for the recon pass.

Captures every request (including POST bodies) and response the browser makes,
masks credentials/tokens, and writes:

  - recon/har/<ts>.har          full HAR 1.2-ish capture for offline analysis
  - recon/booking-flow.json     distilled, redacted endpoint summary

Deliberately built on Playwright's own request/response events rather than a CDP
session, so the same recorder works whether we launched the browser ourselves
(persistent profile) or attached to a running one over CDP.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from typing import Any

from playwright.async_api import BrowserContext, Page, Request, Response

logger = logging.getLogger(__name__)

# Keys whose *values* are masked. Matches form fields, JSON keys and query params.
SECRET_KEY_RE = re.compile(
    r"(pass|pwd|secret|token|csrf|xsrf|session|auth|cookie|otp|captcha|"
    r"form_build_id|form_token|signature|nonce|apikey|api_key)",
    re.IGNORECASE,
)

# Headers kept in the HAR but with masked values.
SECRET_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-csrf-token",
    "x-xsrf-token",
    "x-api-key",
}

# Header names worth keeping verbatim (structure matters for replay).
KEEP_HEADERS = {
    "content-type",
    "accept",
    "x-requested-with",
    "referer",
    "origin",
    "user-agent",
}

DEFAULT_TYPES = ("document", "xhr", "fetch")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mask(value: Any) -> str:
    """Mask a secret value but keep its length/shape recognisable."""
    if value is None:
        return ""
    text = str(value)
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:2]}{'*' * min(len(text) - 4, 24)}{text[-2:]}"


def _is_secret(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key or ""))


def redact_dict(pairs: dict[str, Any]) -> dict[str, Any]:
    return {k: (mask(v) if _is_secret(k) else v) for k, v in pairs.items()}


def redact_node(node: Any) -> Any:
    """Recursively mask secret-looking keys inside decoded JSON.

    Recurse rather than only masking top-level keys: request bodies nest
    applicant data (e.g. {"applicant": {"passport": ...}}), and a shallow pass
    would write that PII into the HAR verbatim.
    """
    if isinstance(node, dict):
        return {k: (mask(v) if _is_secret(k) else redact_node(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [redact_node(v) for v in node]
    return node


def redact_url(url: str) -> str:
    """Mask secret query-parameter values, keep the rest of the URL intact."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    safe = "&".join(f"{k}={mask(v) if _is_secret(k) else v}" for k, v in query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, safe, parts.fragment))


def redact_headers(headers: dict[str, str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for name, value in (headers or {}).items():
        low = name.lower()
        if low in SECRET_HEADERS:
            out.append({"name": name, "value": mask(value)})
        elif low in KEEP_HEADERS:
            out.append({"name": name, "value": value})
        else:
            out.append({"name": name, "value": ""})
    return out


def parse_post_data(post_data: str, content_type: str) -> tuple[str | None, list[dict[str, str]], dict[str, Any]]:
    """Return (mimeType, HAR params, redacted field map) for a request body."""
    if not post_data:
        return None, [], {}
    mime = (content_type or "").split(";")[0].strip() or "application/octet-stream"

    if mime == "application/x-www-form-urlencoded":
        pairs = parse_qsl(post_data, keep_blank_values=True)
        fields = {k: (mask(v) if _is_secret(k) else v) for k, v in pairs}
        body = "&".join(f"{k}={fields[k]}" for k, _ in pairs)
        return mime, [{"name": k, "value": fields[k]} for k, _ in pairs], {"fields": fields}

    if "json" in mime:
        try:
            parsed = json.loads(post_data)
        except (ValueError, TypeError):
            return mime, [], {"raw": mask(post_data)}
        if isinstance(parsed, dict):
            return mime, [], {"fields": redact_node(parsed)}
        return mime, [], {"value": "[non-object json]"}

    return mime, [], {"raw": mask(post_data)}


@dataclass
class RecorderConfig:
    out_dir: Path
    capture_types: tuple[str, ...] = DEFAULT_TYPES
    capture_all: bool = False
    max_body_bytes: int = 512_000
    redact: bool = True
    rotate_bytes: int = 32 * 1024 * 1024


class TrafficRecorder:
    """Attaches to a BrowserContext and records everything that flows through it."""

    def __init__(self, cfg: RecorderConfig):
        self.cfg = cfg
        self.entries: list[dict[str, Any]] = []
        self.pages_seen: set[str] = set()
        self.errors = 0
        self.started_at = _now_iso()
        self._har_index = 0
        self._page_listeners: set[int] = set()
        (self.cfg.out_dir / "har").mkdir(parents=True, exist_ok=True)

    # ── attach ────────────────────────────────────────────────────────────

    def attach(self, context: BrowserContext) -> None:
        """Hook existing pages and any page opened later (new tabs included)."""
        context.on("page", self._on_page)
        for page in context.pages:
            self._on_page(page)

    def _on_page(self, page: Page) -> None:
        key = id(page)
        if key in self._page_listeners:
            return
        self._page_listeners.add(key)
        page.on("response", lambda resp: self._spawn(self._on_response(resp)))
        logger.info("recorder attached to page %s", getattr(page, "url", "?"))

    def _spawn(self, coro) -> None:
        """Fire-and-forget a coroutine on the running loop without killing it."""
        import asyncio

        task = asyncio.ensure_future(coro)

        def _done(t: "asyncio.Task") -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self.errors += 1
                logger.debug("recorder handler failed: %s", exc)

        task.add_done_callback(_done)

    # ── capture ───────────────────────────────────────────────────────────

    async def _on_response(self, response: Response) -> None:
        try:
            request = response.request
            rtype = request.resource_type
            if not self.cfg.capture_all and rtype not in self.cfg.capture_types:
                return

            req_headers = await self._safe_headers(request)
            resp_headers = await self._safe_headers(response)
            content_type = resp_headers.get("content-type", "")

            post_mime, params, req_body = parse_post_data(
                request.post_data or "", req_headers.get("content-type", "")
            )
            body_text, body_size = await self._safe_body(response, content_type)

            entry = {
                "startedDateTime": self.started_at,
                "time": 0,
                "resourceType": rtype,
                "request": {
                    "method": request.method,
                    "url": redact_url(request.url) if self.cfg.redact else request.url,
                    "headers": redact_headers(req_headers) if self.cfg.redact else req_headers,
                    "postData": (
                        {"mimeType": post_mime, "params": params} if post_mime else None
                    ),
                    "fields": req_body,
                },
                "response": {
                    "status": response.status,
                    "statusText": response.status_text,
                    "headers": redact_headers(resp_headers) if self.cfg.redact else resp_headers,
                    "content": {
                        "mimeType": content_type,
                        "size": body_size,
                        "text": body_text,
                    },
                },
            }
            self.entries.append(entry)
            self._log_interesting(entry)
            if sum(len(json.dumps(e)) for e in self.entries[-50:]) > self.cfg.rotate_bytes:
                self.write_har()
        except Exception as exc:  # never let capture break the session
            self.errors += 1
            logger.debug("recorder failed on %s: %s", getattr(response, "url", "?"), exc)

    async def _safe_headers(self, obj) -> dict[str, str]:
        try:
            return await obj.all_headers()
        except Exception:
            return {}

    async def _safe_body(self, response: Response, content_type: str) -> tuple[str | None, int]:
        """Read the response body when it's small and textual."""
        textual = any(
            token in (content_type or "")
            for token in ("json", "text", "html", "xml", "javascript")
        )
        if not textual:
            return None, -1
        try:
            raw = await response.body()
        except Exception:
            return None, -1
        size = len(raw)
        if size > self.cfg.max_body_bytes:
            return f"[body omitted: {size} bytes]", size
        text = raw.decode("utf-8", errors="replace")
        if self.cfg.redact:
            text = self._redact_body(text, content_type)
        return text, size

    def _redact_body(self, text: str, content_type: str) -> str:
        if "json" in (content_type or ""):
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                return text
            return json.dumps(self._redact_json(parsed))[: self.cfg.max_body_bytes]
        # token-ish values in HTML are worth masking even when not JSON
        return re.sub(
            r"(name=\"[^\"]*(?:token|csrf|pass)[^\"]*\"[^>]*value=\")([^\"]*)(\")",
            lambda m: f"{m.group(1)}{mask(m.group(2))}{m.group(3)}",
            text,
        )

    def _redact_json(self, node: Any) -> Any:
        return redact_node(node)

    def _log_interesting(self, entry: dict[str, Any]) -> None:
        url = entry["request"]["url"]
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        rtype = entry["resourceType"]
        has_body = "yes" if entry["request"]["fields"] else "no"
        if rtype in ("xhr", "fetch") or method != "GET":
            logger.info(
                "REC | %s %s -> %s | body=%s | type=%s",
                method,
                url[:110],
                status,
                has_body,
                rtype,
            )

    # ── output ────────────────────────────────────────────────────────────

    def write_har(self) -> Path:
        self._har_index += 1
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.cfg.out_dir / "har" / f"{stamp}__{self._har_index:03d}.har"
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "gvcw-recorder", "version": "0.1"},
                "pages": [{"startedDateTime": self.started_at, "id": "page_1", "title": "GVCW recon"}],
                "entries": self.entries,
            }
        }
        path.write_text(json.dumps(har, indent=1))
        logger.info("wrote %s (%d entries)", path, len(self.entries))
        return path

    def write_flow(self) -> Path:
        """Distil the capture into the endpoint map used to fill BookingSelectors."""
        endpoints: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self.entries:
            req = entry["request"]
            url = req["url"]
            try:
                split = urlsplit(url)
                key = (req["method"], f"{split.scheme}://{split.netloc}{split.path}")
            except ValueError:
                key = (req["method"], url)
            slot = endpoints.setdefault(
                key,
                {
                    "method": req["method"],
                    "url": url,
                    "count": 0,
                    "statuses": [],
                    "requestContentTypes": set(),
                    "requestFields": set(),
                    "responseShape": set(),
                    "resourceTypes": set(),
                },
            )
            slot["count"] += 1
            if entry["response"]["status"] not in slot["statuses"]:
                slot["statuses"].append(entry["response"]["status"])
            slot["resourceTypes"].add(entry["resourceType"])
            if req["postData"]:
                slot["requestContentTypes"].add(req["postData"].get("mimeType") or "")
            fields = (req.get("fields") or {}).get("fields") or {}
            slot["requestFields"].update(fields.keys())
            text = entry["response"]["content"].get("text")
            if text and "json" in (entry["response"]["content"].get("mimeType") or ""):
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    slot["responseShape"].update(parsed.keys())
                elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    slot["responseShape"].update(f"[] {k}" for k in parsed[0].keys())

        flows = []
        for slot in endpoints.values():
            for key in ("requestContentTypes", "requestFields", "responseShape", "resourceTypes"):
                slot[key] = sorted(x for x in slot[key] if x)
            flows.append(slot)
        flows.sort(key=lambda s: (not s["requestFields"], -s["count"]))

        out = {
            "generated_at": _now_iso(),
            "entry_count": len(self.entries),
            "recorder_errors": self.errors,
            "endpoints": flows,
        }
        path = self.cfg.out_dir / "booking-flow.json"
        path.write_text(json.dumps(out, indent=1))
        logger.info("wrote %s (%d endpoints)", path, len(flows))
        return path

    async def aclose(self) -> None:
        try:
            self.write_har()
            self.write_flow()
        except Exception as exc:
            logger.warning("could not write recon output: %s", exc)
