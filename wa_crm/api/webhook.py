"""Meta webhook endpoint.

Callback URL:  https://<your-site>/api/method/wa_crm.api.webhook.receive

GET  -> subscription handshake (hub.challenge) against any account's verify token.
POST -> verify X-Hub-Signature-256 over the RAW body, persist the payload in
        WA Webhook Log (durable inbox), enqueue processing, answer 200 at once.
        Meta retries slow acks, so no business logic runs in the request.
"""

import frappe
from werkzeug.wrappers import Response

from wa_crm.core.logic import verify_meta_signature


def _accounts():
    return frappe.get_all("WA Account", filters={"enabled": 1}, pluck="name")


def _secret(account: str, field: str) -> str | None:
    try:
        return frappe.get_cached_doc("WA Account", account).get_password(field, raise_exception=False)
    except Exception:
        return None


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def receive():
    if frappe.request.method == "GET":
        return _verify_subscription()
    return _accept_event()


def _verify_subscription():
    args = frappe.request.args
    mode, token, challenge = args.get("hub.mode"), args.get("hub.verify_token"), args.get("hub.challenge")
    if mode == "subscribe" and token and challenge:
        for account in _accounts():
            if _secret(account, "verify_token") == token:
                return Response(challenge, status=200, mimetype="text/plain")
    return Response("Verification failed", status=403, mimetype="text/plain")


def _accept_event():
    raw = frappe.request.get_data(cache=True) or b""
    signature = frappe.request.headers.get("X-Hub-Signature-256")
    secrets = [_secret(a, "app_secret") for a in _accounts()]
    if not verify_meta_signature(raw, signature, secrets):
        # 401 (not 200) so a misconfigured secret shows loudly in Meta's dashboard.
        return Response("Invalid signature", status=401, mimetype="text/plain")

    log = frappe.get_doc({"doctype": "WA Webhook Log", "status": "Queued",
                          "payload": raw.decode("utf-8", "replace")})
    log.insert(ignore_permissions=True)
    frappe.enqueue(
        "wa_crm.whatsapp.inbound.process_log",
        queue="short",
        log_name=log.name,
        enqueue_after_commit=True,
    )
    frappe.db.commit()
    return Response('{"status":"received"}', status=200, mimetype="application/json")
