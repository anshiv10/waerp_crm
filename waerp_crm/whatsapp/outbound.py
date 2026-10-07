"""Single outbound path. Every engine (agent, flow, automation, broadcast, AI,
ERP notification, API) sends through here so that logging, the 24h rule,
opt-out, realtime and webhooks behave identically everywhere."""

from __future__ import annotations

import mimetypes

import frappe
from frappe import _
from frappe.utils import now_datetime

from waerp_crm.core.logic import build_template_components, render_template_preview
from waerp_crm.utils import get_or_create_conversation, publish, session_open
from waerp_crm.whatsapp.graph import GraphClient, WhatsAppAPIError

MARKETING_SOURCES = {"Broadcast"}


class OutboundBlocked(frappe.ValidationError):
    pass


def _conv(conversation: str):
    return frappe.db.get_value(
        "WA Conversation", conversation,
        ["name", "wa_account", "wa_contact", "wa_id", "first_response_at"], as_dict=True,
    )


def _recipient(conv) -> str:
    wa_id = frappe.db.get_value("WA Contact", conv.wa_contact, "wa_id")  # source of truth, not the fetched copy
    if not wa_id:
        raise OutboundBlocked(_("This contact has no phone number (username-only); reply within the chat window."))
    return wa_id


def _create(conv, *, message_type, content=None, source="Agent", sent_by=None, template=None,
            template_params=None, interactive_payload=None, media_file=None, filename=None,
            reply_to=None, reference_doctype=None, reference_name=None, is_ai=False):
    return frappe.get_doc({
        "doctype": "WA Message",
        "conversation": conv.name,
        "wa_account": conv.wa_account,
        "wa_contact": conv.wa_contact,
        "direction": "Outbound",
        "message_type": message_type,
        "content": content,
        "status": "Queued",
        "source": source,
        "sent_by": sent_by or (frappe.session.user if source == "Agent" else None),
        "template": template,
        "template_params": template_params,
        "interactive_payload": interactive_payload,
        "media_file": media_file,
        "filename": filename,
        "reply_to": reply_to,
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "is_ai_generated": 1 if is_ai else 0,
        "timestamp": now_datetime(),
    }).insert(ignore_permissions=True)


def _finish(msg, conv, call, preview: str, raise_on_error: bool):
    """Run the Graph call, record result, update conversation, notify."""
    try:
        wamid = call()
        msg.db_set({"status": "Sent", "message_id": wamid,
                    "dedupe_key": f"out:{conv.wa_account}:{wamid}"}, update_modified=False)
    except WhatsAppAPIError as e:
        msg.db_set({"status": "Failed", "error_code": str(e.code or ""), "error_message": str(e)[:1000]},
                   update_modified=False)
        publish("message", conv.name, {"message": msg.name})
        if raise_on_error:
            raise
        return msg

    now = now_datetime()
    updates = {"last_message": (preview or "")[:300], "last_message_at": now, "last_outbound_at": now}
    if msg.source == "Agent":
        updates["awaiting_reply_since"] = None
        if not conv.first_response_at:
            updates["first_response_at"] = now
    frappe.db.set_value("WA Conversation", conv.name, updates, update_modified=False)
    publish("message", conv.name, {"message": msg.name})

    from waerp_crm.engines.outbound_webhooks import emit
    emit("message.sent", {"conversation": conv.name, "message": msg.name, "wamid": msg.message_id,
                          "type": msg.message_type, "source": msg.source})
    return msg


def _guard_freeform(conv, source):
    if not session_open(conv.name):
        raise OutboundBlocked(
            _("The 24-hour customer service window is closed. Send an approved template instead.")
        )


def _guard_opt_out(conv, source):
    if source in MARKETING_SOURCES and frappe.db.get_value("WA Contact", conv.wa_contact, "opt_status") == "Opted Out":
        raise OutboundBlocked(_("Contact has opted out of marketing messages."))


# ------------------------------------------------------------------ public API


def send_text(conversation: str, text: str, source="Agent", reply_to: str | None = None,
              raise_on_error=True, is_ai=False, **ref):
    conv = _conv(conversation)
    _guard_freeform(conv, source)
    to = _recipient(conv)
    parent_wamid = frappe.db.get_value("WA Message", reply_to, "message_id") if reply_to else None
    msg = _create(conv, message_type="text", content=text, source=source, reply_to=reply_to, is_ai=is_ai, **ref)
    client = GraphClient(conv.wa_account)
    return _finish(msg, conv, lambda: client.send_text(to, text, reply_to=parent_wamid), text, raise_on_error)


def send_buttons(conversation: str, cfg: dict, source="Flow", raise_on_error=True, text: str | None = None):
    conv = _conv(conversation)
    _guard_freeform(conv, source)
    to = _recipient(conv)
    body = text or cfg.get("text")
    msg = _create(conv, message_type="interactive", content=body, source=source, interactive_payload=cfg)
    client = GraphClient(conv.wa_account)
    return _finish(msg, conv, lambda: client.send_buttons(to, body, cfg["buttons"], cfg.get("header_text"),
                                                          cfg.get("footer_text")), body, raise_on_error)


def send_list(conversation: str, cfg: dict, source="Flow", raise_on_error=True, text: str | None = None):
    conv = _conv(conversation)
    _guard_freeform(conv, source)
    to = _recipient(conv)
    body = text or cfg.get("text")
    msg = _create(conv, message_type="interactive", content=body, source=source, interactive_payload=cfg)
    client = GraphClient(conv.wa_account)
    return _finish(msg, conv, lambda: client.send_list(to, body, cfg["button_label"], cfg["sections"],
                                                       cfg.get("header_text"), cfg.get("footer_text")),
                   body, raise_on_error)


def _file_bytes(file_url: str) -> tuple[bytes, str, str]:
    file_doc = frappe.get_doc("File", {"file_url": file_url})
    content = file_doc.get_content()
    if isinstance(content, str):
        content = content.encode()
    mime = mimetypes.guess_type(file_doc.file_name or file_url)[0] or "application/octet-stream"
    return content, mime, file_doc.file_name


def media_kind(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def send_media(conversation: str, file_url: str | None = None, link: str | None = None, caption: str | None = None,
               media_type: str | None = None, filename: str | None = None, source="Agent", raise_on_error=True,
               **ref):
    """Send a Frappe File (uploaded to Meta first, so private files work) or a public link."""
    conv = _conv(conversation)
    _guard_freeform(conv, source)
    to = _recipient(conv)
    client = GraphClient(conv.wa_account)
    media_id = None
    if file_url and not file_url.startswith("http"):
        content, mime, fname = _file_bytes(file_url)
        media_type = media_type or media_kind(mime)
        filename = filename or fname
        media_id = client.upload_media(content, mime, fname)
    else:
        link = link or file_url
        media_type = media_type or media_kind(mimetypes.guess_type(link or "")[0] or "")
    msg = _create(conv, message_type=media_type, content=caption, source=source, media_file=file_url or link,
                  filename=filename, **ref)
    return _finish(msg, conv, lambda: client.send_media(to, media_type, link=link, media_id=media_id,
                                                        caption=caption, filename=filename),
                   caption or f"[{media_type}]", raise_on_error)


def send_template(conversation: str, template: str, body_params: list | None = None,
                  header_param: str | None = None, header_file: str | None = None,
                  header_pdf: tuple[bytes, str] | None = None, button_params: list | None = None,
                  source="Agent", raise_on_error=True, **ref):
    """Templates work at any time (outside the 24h window too).

    header_file: Frappe file URL / public link for media headers.
    header_pdf:  (bytes, filename) for a generated PDF (ERP notifications).
    """
    conv = _conv(conversation)
    _guard_opt_out(conv, source)
    to = _recipient(conv)
    tpl = frappe.get_cached_doc("WA Template", template)
    if tpl.status != "APPROVED":
        raise OutboundBlocked(_("Template {0} is not approved by Meta (status: {1}).").format(tpl.template_name, tpl.status))
    client = GraphClient(conv.wa_account)
    header_media = None
    if tpl.header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
        if header_pdf:
            content, fname = header_pdf
            header_media = {"id": client.upload_media(content, "application/pdf", fname), "filename": fname}
        elif header_file:
            if header_file.startswith("http"):
                header_media = {"link": header_file}
            else:
                content, mime, fname = _file_bytes(header_file)
                header_media = {"id": client.upload_media(content, mime, fname), "filename": fname}
        else:
            raise OutboundBlocked(_("Template {0} needs a {1} header.").format(tpl.template_name, tpl.header_type.lower()))
    body_params = [str(p) for p in (body_params or [])]
    if len(body_params) < (tpl.body_param_count or 0):
        body_params += ["-"] * ((tpl.body_param_count or 0) - len(body_params))
    components = build_template_components(tpl.header_type, body_params, header_param, header_media, button_params)
    preview = render_template_preview(tpl.body, body_params)
    msg = _create(conv, message_type="template", content=preview, source=source, template=template,
                  template_params={"body": body_params, "header": header_param}, **ref)
    return _finish(msg, conv, lambda: client.send_template(to, tpl.template_name, tpl.language, components),
                   preview, raise_on_error)


def send_reaction(message: str, emoji: str):
    m = frappe.get_doc("WA Message", message)
    conv = _conv(m.conversation)
    GraphClient(conv.wa_account).send_reaction(_recipient(conv), m.message_id, emoji)


def conversation_for_number(phone: str, account: str | None = None, name: str | None = None) -> str:
    """Resolve (or create) the conversation for a raw phone number (API / notifications / broadcasts)."""
    from waerp_crm.utils import get_account, get_or_create_wa_contact

    acc = get_account(account)
    wa_contact, _created = get_or_create_wa_contact(phone, name=name)
    conv, _ = get_or_create_conversation(acc.name, wa_contact)
    return conv
