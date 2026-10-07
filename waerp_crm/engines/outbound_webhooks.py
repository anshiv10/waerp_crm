"""Signed outbound webhooks so clients can plug WAERP CRM into anything (n8n, Zapier, own apps)."""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from urllib.parse import urlparse

import frappe
import requests
from frappe.utils import now_datetime

from waerp_crm.core.logic import sign_body, split_lines


def _endpoints_for(event: str) -> list[str]:
    key = "waerp_crm:webhook_endpoints"
    rows = frappe.cache.get_value(key)
    if rows is None:
        rows = frappe.get_all("WA Webhook Endpoint", filters={"enabled": 1}, fields=["name", "events"])
        frappe.cache.set_value(key, rows, expires_in_sec=300)
    out = []
    for r in rows:
        events = split_lines(r.get("events")) or ["*"]
        if "*" in events or event in events:
            out.append(r["name"])
    return out


def clear_cache(doc=None, method=None):
    frappe.cache.delete_value("waerp_crm:webhook_endpoints")


def emit(event: str, data: dict):
    """Fire-and-forget: enqueue one delivery job per subscribed endpoint."""
    try:
        endpoints = _endpoints_for(event)
    except Exception:
        return
    payload = {"event": event, "site": frappe.local.site, "timestamp": str(now_datetime()), "data": data}
    for ep in endpoints:
        frappe.enqueue("waerp_crm.engines.outbound_webhooks.deliver", queue="short", endpoint=ep,
                       payload=payload, enqueue_after_commit=True)


def is_public_url(url: str) -> bool:
    """SSRF guard: only http(s) to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def safe_post(url: str, body: dict | bytes, headers: dict | None = None, timeout: int = 10) -> requests.Response:
    if not frappe.get_cached_doc("WA CRM Settings").allow_private_webhook_targets and not is_public_url(url):
        raise ValueError(f"Blocked non-public webhook target: {url}")
    raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
    return requests.post(url, data=raw, headers={"Content-Type": "application/json", **(headers or {})},
                         timeout=timeout, allow_redirects=False)


def deliver(endpoint: str, payload: dict):
    ep = frappe.get_doc("WA Webhook Endpoint", endpoint)
    raw = json.dumps(payload, default=str).encode()
    secret = ep.get_password("secret", raise_exception=False)
    headers = {"X-WAERP-CRM-Event": payload.get("event", "")}
    if secret:
        headers["X-WAERP-CRM-Signature"] = sign_body(secret, raw)
    last_error = None
    for attempt in range(3):
        try:
            resp = safe_post(ep.url, raw, headers)
            if resp.status_code < 300:
                if ep.failure_count:
                    ep.db_set({"failure_count": 0, "last_error": None}, update_modified=False)
                return
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except ValueError as e:  # SSRF block: don't retry
            last_error = str(e)
            break
        except requests.RequestException as e:
            last_error = str(e)
        time.sleep(2 ** attempt)
    count = (ep.failure_count or 0) + 1
    ep.db_set({"failure_count": count, "last_error": last_error[:1000] if last_error else None,
               "enabled": 0 if count >= 50 else ep.enabled}, update_modified=False)
