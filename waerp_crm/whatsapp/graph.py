"""Thin, explicit client for Meta's WhatsApp Cloud API (Graph API)."""

from __future__ import annotations

import frappe
import requests
from frappe import _

TIMEOUT = 30

# Friendly explanations for the error codes that support teams hit most.
ERROR_HINTS = {
    131047: "More than 24 hours have passed since the customer last replied. Send an approved template instead.",
    131026: "Message undeliverable: the number may not be on WhatsApp, or hasn't accepted the latest terms.",
    131051: "Unsupported message type.",
    131049: "Meta limited this marketing message to protect user experience (per-user marketing limit). Try later.",
    131050: "The user has stopped marketing messages from your business.",
    131030: "Recipient not in the allowed list (test numbers only while the app is in development).",
    131056: "Too many messages to this user in a short time (pair rate limit). Slow down.",
    130429: "Throughput limit reached. Messages will need to be retried.",
    132000: "Template parameter count does not match the template.",
    132001: "Template does not exist in this language or is not approved yet.",
    132015: "Template is paused due to low quality.",
    132016: "Template is disabled due to low quality.",
    190: "Access token expired or invalid. Generate a permanent System User token.",
    100: "Invalid parameter in the request.",
    368: "This WhatsApp account is temporarily restricted for policy violations.",
    131031: "This WhatsApp account has been locked. Check Business Manager.",
}


class WhatsAppAPIError(Exception):
    def __init__(self, message: str, code: int | None = None, details: str | None = None,
                 http_status: int | None = None):
        self.code = code
        self.details = details
        self.http_status = http_status
        hint = ERROR_HINTS.get(code or 0)
        super().__init__(f"{message}{' — ' + hint if hint else ''}")

    @property
    def is_recipient_not_allowed(self) -> bool:
        return self.code == 131030

    @property
    def is_retryable(self) -> bool:
        return self.code in (130429, 131056, 131000, 2) or (self.http_status or 0) >= 500


class GraphClient:
    def __init__(self, account):
        if isinstance(account, str):
            account = frappe.get_cached_doc("WA Account", account)
        self.account = account
        self.version = account.api_version or "v23.0"
        self.base = f"https://graph.facebook.com/{self.version}"
        self.token = account.get_password("access_token")
        self.phone_number_id = account.phone_number_id
        self.waba_id = account.waba_id

    # ------------------------------------------------------------- transport
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        try:
            resp = requests.request(method, url, headers=self._headers(), timeout=TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise WhatsAppAPIError(_("Could not reach Meta: {0}").format(e)) from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
            err = (data or {}).get("error") or {}
            raise WhatsAppAPIError(
                err.get("message") or f"HTTP {resp.status_code}",
                code=err.get("code"),
                details=(err.get("error_data") or {}).get("details"),
                http_status=resp.status_code,
            )
        return data

    def _send(self, payload: dict) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        data = self._request("POST", f"{self.phone_number_id}/messages", json=body)
        return (data.get("messages") or [{}])[0].get("id")

    # ------------------------------------------------------------- messages
    def send_text(self, to: str, body: str, preview_url: bool = True, reply_to: str | None = None) -> str:
        payload = {"to": to, "type": "text", "text": {"body": body[:4096], "preview_url": preview_url}}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return self._send(payload)

    def send_template(self, to: str, name: str, language: str, components: list | None = None) -> str:
        tpl = {"name": name, "language": {"code": language}}
        if components:
            tpl["components"] = components
        return self._send({"to": to, "type": "template", "template": tpl})

    def send_media(self, to: str, media_type: str, link: str | None = None, media_id: str | None = None,
                   caption: str | None = None, filename: str | None = None) -> str:
        media = {"id": media_id} if media_id else {"link": link}
        if caption and media_type in ("image", "video", "document"):
            media["caption"] = caption[:1024]
        if filename and media_type == "document":
            media["filename"] = filename
        return self._send({"to": to, "type": media_type, media_type: media})

    def send_buttons(self, to: str, text: str, buttons: list[dict], header: str | None = None,
                     footer: str | None = None) -> str:
        interactive = {
            "type": "button",
            "body": {"text": text[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["reply_id"], "title": b["title"][:20]}} for b in buttons[:3]
            ]},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}
        return self._send({"to": to, "type": "interactive", "interactive": interactive})

    def send_list(self, to: str, text: str, button_label: str, sections: list[dict],
                  header: str | None = None, footer: str | None = None) -> str:
        interactive = {
            "type": "list",
            "body": {"text": text[:4096]},
            "action": {
                "button": button_label[:20],
                "sections": [
                    {
                        **({"title": s["title"][:24]} if s.get("title") else {}),
                        "rows": [
                            {"id": r["reply_id"], "title": r["title"][:24],
                             **({"description": r["description"][:72]} if r.get("description") else {})}
                            for r in s.get("rows", [])
                        ],
                    }
                    for s in sections
                ],
            },
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}
        return self._send({"to": to, "type": "interactive", "interactive": interactive})

    def send_reaction(self, to: str, message_id: str, emoji: str) -> str:
        return self._send({"to": to, "type": "reaction", "reaction": {"message_id": message_id, "emoji": emoji}})

    def mark_read(self, message_id: str, typing: bool = False) -> None:
        body = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        if typing:
            body["typing_indicator"] = {"type": "text"}
        self._request("POST", f"{self.phone_number_id}/messages", json=body)

    # ------------------------------------------------------------- media
    def upload_media(self, content: bytes, mime_type: str, filename: str = "file") -> str:
        data = self._request(
            "POST", f"{self.phone_number_id}/media",
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, content, mime_type)},
        )
        return data.get("id")

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = self._request("GET", media_id)
        url = meta.get("url")
        if not url:
            raise WhatsAppAPIError(_("Media URL missing for {0}").format(media_id))
        resp = requests.get(url, headers=self._headers(), timeout=60)
        if resp.status_code >= 400:
            raise WhatsAppAPIError(_("Media download failed"), http_status=resp.status_code)
        return resp.content, meta.get("mime_type") or resp.headers.get("Content-Type", "")

    # ------------------------------------------------------------- management
    def list_templates(self) -> list[dict]:
        out, url = [], f"{self.waba_id}/message_templates"
        params = {"limit": 100,
                  "fields": "id,name,language,status,category,components,quality_score,rejected_reason"}
        while url:
            data = self._request("GET", url, params=params)
            out.extend(data.get("data", []))
            url = (data.get("paging") or {}).get("next")
            params = None  # the `next` URL already carries the query
        return out

    def get_app_id(self) -> str:
        """Meta App ID for the Resumable Upload API: the account's field, else the token's own app."""
        app_id = (self.account.get("app_id") or "").strip()
        if app_id:
            return app_id
        try:
            app_id = str(self._request("GET", "app", params={"fields": "id"}).get("id") or "")
        except WhatsAppAPIError:
            app_id = ""
        if not app_id:
            raise WhatsAppAPIError(_("Set the Meta App ID on WA Account {0} (App Dashboard > App settings > Basic).")
                                   .format(self.account.name))
        frappe.db.set_value("WA Account", self.account.name, "app_id", app_id, update_modified=False)
        return app_id

    def upload_sample(self, content: bytes, mime_type: str, filename: str) -> str:
        """Resumable Upload API -> file handle for a template's media-header example."""
        session = self._request("POST", f"{self.get_app_id()}/uploads", params={
            "file_name": filename, "file_length": len(content), "file_type": mime_type})
        sid = session.get("id")
        if not sid:
            raise WhatsAppAPIError(_("Meta did not start the sample upload."))
        url = f"{self.base}/{sid}"
        try:
            resp = requests.post(url, data=content, timeout=120,
                                 headers={"Authorization": f"OAuth {self.token}", "file_offset": "0"})
        except requests.RequestException as e:
            raise WhatsAppAPIError(_("Could not reach Meta: {0}").format(e)) from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or not data.get("h"):
            err = (data or {}).get("error") or {}
            raise WhatsAppAPIError(err.get("message") or f"Sample upload failed (HTTP {resp.status_code})",
                                   code=err.get("code"), http_status=resp.status_code)
        return data["h"]

    def create_template(self, payload: dict) -> dict:
        return self._request("POST", f"{self.waba_id}/message_templates", json=payload)

    def delete_template(self, name: str) -> dict:
        return self._request("DELETE", f"{self.waba_id}/message_templates", params={"name": name})

    def phone_info(self) -> dict:
        return self._request(
            "GET", self.phone_number_id,
            params={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier"},
        )

    def subscribe_app(self) -> dict:
        """Subscribe our Meta app to this WABA's webhooks (needed once per WABA)."""
        return self._request("POST", f"{self.waba_id}/subscribed_apps")
