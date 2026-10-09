"""Engine 1 — webhook ingestion (runs in a background worker).

Order of work for each inbound message:
  1. resolve account by phone_number_id
  2. find/create WA Contact (number or BSUID) + ERP Contact/Customer link or Lead
  3. find/create the conversation (deterministic name => race-safe)
  4. take the per-conversation Redis lock
  5. idempotency: the (account, wamid) dedupe_key is UNIQUE -> a Meta retry is a no-op
  6. store message, atomically bump unread + 24h window, reopen if resolved
  7. opt-out / opt-in keywords
  8. hand over to the dispatcher (flows -> automations -> away msg -> AI -> webhooks)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frappe
from frappe.utils import add_to_date, now_datetime

from waerp_crm.core.logic import META_STATUS_MAP, MESSAGE_LADDER, is_valid_transition, split_lines
from waerp_crm.utils import (conversation_lock, fresh_write, get_or_create_conversation,
                             get_or_create_wa_contact, publish, settings)

MAX_ATTEMPTS = 5


# ------------------------------------------------------------------ job entry points


def process_log(log_name: str):
    log = frappe.get_doc("WA Webhook Log", log_name)
    if log.status == "Processed":
        return
    frappe.set_user("Administrator")
    try:
        process_payload(json.loads(log.payload))
        log.db_set({"status": "Processed", "attempts": (log.attempts or 0) + 1, "error": None})
    except Exception:
        frappe.db.rollback()
        log.reload()
        log.db_set({"status": "Failed" if (log.attempts or 0) + 1 >= MAX_ATTEMPTS else "Queued",
                    "attempts": (log.attempts or 0) + 1, "error": frappe.get_traceback()[-2000:]})
        frappe.log_error(title="WAERP CRM: webhook processing failed", reference_doctype="WA Webhook Log",
                         reference_name=log_name)
    frappe.db.commit()


def requeue_stuck_logs():
    """Scheduler (every 5 min): re-enqueue logs a crashed worker never finished."""
    cutoff = add_to_date(now_datetime(), minutes=-5)
    for name in frappe.get_all("WA Webhook Log", filters={"status": "Queued", "modified": ("<", cutoff)},
                               pluck="name", limit=200):
        frappe.enqueue("waerp_crm.whatsapp.inbound.process_log", queue="short", log_name=name)


def process_payload(payload: dict):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            field = change.get("field")
            value = change.get("value") or {}
            if field in ("message_template_status_update", "message_template_quality_update",
                         "template_category_update"):
                from waerp_crm.whatsapp.templates import handle_template_event
                handle_template_event(field, value, waba_id=entry.get("id"))
                continue
            if field == "phone_number_quality_update":
                _phone_quality(value)
                continue
            if field != "messages":
                continue
            account = frappe.db.get_value(
                "WA Account", {"phone_number_id": (value.get("metadata") or {}).get("phone_number_id")}, "name"
            )
            if not account:
                frappe.log_error(title="WAERP CRM: webhook for unknown phone_number_id", message=json.dumps(value)[:5000])
                continue
            for status in value.get("statuses") or []:
                handle_status(account, status)
            contacts = value.get("contacts") or []
            for i, msg in enumerate(sorted(value.get("messages") or [], key=lambda m: int(m.get("timestamp") or 0))):
                contact = next((c for c in contacts if c.get("wa_id") and c.get("wa_id") == msg.get("from")),
                               contacts[i] if i < len(contacts) else (contacts[0] if contacts else {}))
                handle_message(account, msg, contact)


def _phone_quality(value: dict):
    pid = value.get("display_phone_number")
    if pid:
        frappe.db.set_value("WA Account", {"display_phone_number": pid}, {
            "quality_rating": value.get("current_limit") or value.get("event"),
        }, update_modified=False)


# ------------------------------------------------------------------ statuses


def handle_status(account: str, status: dict):
    wamid = status.get("id")
    incoming = META_STATUS_MAP.get(status.get("status"))
    if not wamid or not incoming:
        return
    err = (status.get("errors") or [{}])[0]
    msg = frappe.db.get_value("WA Message", {"dedupe_key": f"out:{account}:{wamid}"},
                              ["name", "status", "conversation"], as_dict=True)
    if msg and is_valid_transition(msg.status, incoming, MESSAGE_LADDER):
        updates = {"status": incoming}
        if incoming == "Failed":
            updates.update(error_code=str(err.get("code") or ""),
                           error_message=((err.get("error_data") or {}).get("details") or err.get("title") or "")[:1000])
        frappe.db.set_value("WA Message", msg.name, updates, update_modified=False)
        publish("status", msg.conversation, {"message": msg.name, "status": incoming})
        from waerp_crm.engines.outbound_webhooks import emit
        emit("message.status", {"message": msg.name, "wamid": wamid, "status": incoming,
                                "conversation": msg.conversation})

    # Broadcast recipient mirror (forward-only ladder + cumulative counters)
    rcpt = frappe.db.get_value("WA Broadcast Recipient", {"message_id": wamid}, ["name", "status", "broadcast"],
                               as_dict=True)
    if rcpt:
        from waerp_crm.engines.broadcasts import transition_recipient
        transition_recipient(rcpt, incoming, error=err.get("title") if incoming == "Failed" else None)


# ------------------------------------------------------------------ messages


def parse_content(msg: dict) -> dict:
    """Normalise Meta's message envelope into our columns."""
    t = msg.get("type")
    out = {"message_type": t if t in ("text", "image", "video", "audio", "document", "sticker", "location",
                                      "contacts", "interactive", "reaction") else "unsupported",
           "content": None, "media_id": None, "media_mime": None, "filename": None,
           "interactive_reply_id": None}
    if t == "text":
        out["content"] = (msg.get("text") or {}).get("body")
    elif t in ("image", "video", "audio", "document", "sticker"):
        m = msg.get(t) or {}
        out.update(content=m.get("caption"), media_id=m.get("id"), media_mime=m.get("mime_type"),
                   filename=m.get("filename"))
    elif t == "location":
        loc = msg.get("location") or {}
        label = " - ".join(x for x in (loc.get("name"), loc.get("address")) if x)
        out["content"] = f"{label}\nhttps://maps.google.com/?q={loc.get('latitude')},{loc.get('longitude')}".strip()
    elif t == "contacts":
        names = [((c.get("name") or {}).get("formatted_name") or "") + " " +
                 ", ".join(p.get("phone", "") for p in c.get("phones", [])) for c in msg.get("contacts", [])]
        out["content"] = "\n".join(n.strip() for n in names)
    elif t == "interactive":
        it = msg.get("interactive") or {}
        reply = it.get("button_reply") or it.get("list_reply") or {}
        out.update(content=reply.get("title"), interactive_reply_id=reply.get("id"))
    elif t == "button":  # quick-reply tap on a TEMPLATE (broadcasts)
        b = msg.get("button") or {}
        out.update(message_type="interactive", content=b.get("text"),
                   interactive_reply_id=b.get("payload") or b.get("text"))
    elif t == "reaction":
        r = msg.get("reaction") or {}
        out.update(content=r.get("emoji"))
    else:
        out["content"] = f"[{t} message]"
    return out


def handle_message(account: str, msg: dict, contact_payload: dict):
    wa_id = contact_payload.get("wa_id") or msg.get("from")
    bsuid = msg.get("from_user_id") or contact_payload.get("user_id")
    if not wa_id and not bsuid:
        frappe.log_error(title="WAERP CRM: inbound message without sender identity", message=json.dumps(msg)[:3000])
        return
    profile = contact_payload.get("profile") or {}
    referral = msg.get("referral") or {}
    source = referral.get("headline") or referral.get("source_url")
    wa_contact, contact_created = get_or_create_wa_contact(
        wa_id, bsuid=bsuid, name=profile.get("name"), username=profile.get("username"), source=source
    )
    conversation, conv_created = get_or_create_conversation(account, wa_contact)
    frappe.db.commit()  # make contact/conversation visible to other workers before locking

    with conversation_lock(conversation):
        _handle_locked(account, msg, wa_contact, contact_created, conversation, conv_created)


def _handle_locked(account, msg, wa_contact, contact_created, conversation, conv_created):
    from waerp_crm.engines.outbound_webhooks import emit

    wamid = msg.get("id")
    dedupe_key = f"in:{account}:{wamid}"
    if frappe.db.exists("WA Message", {"dedupe_key": dedupe_key}):
        return  # Meta retry — already processed

    if conv_created:
        emit("conversation.created", {"conversation": conversation, "wa_contact": wa_contact})

    parsed = parse_content(msg)

    if parsed["message_type"] == "reaction":
        target = frappe.db.get_value("WA Message",
                                     {"dedupe_key": f"out:{account}:{(msg.get('reaction') or {}).get('message_id')}"},
                                     "name")
        if target:
            frappe.db.set_value("WA Message", target, "reaction", parsed["content"] or None, update_modified=False)
            publish("reaction", conversation, {"message": target, "emoji": parsed["content"]})
        frappe.db.commit()
        return

    reply_to = None
    if (msg.get("context") or {}).get("id"):
        cid = msg["context"]["id"]
        reply_to = frappe.db.get_value("WA Message", {"dedupe_key": f"out:{account}:{cid}"}, "name") or \
            frappe.db.get_value("WA Message", {"dedupe_key": f"in:{account}:{cid}"}, "name")

    is_first_inbound = not frappe.db.exists("WA Message", {"conversation": conversation, "direction": "Inbound"})
    ts = meta_timestamp(msg.get("timestamp"))

    doc = frappe.get_doc({
        "doctype": "WA Message", "conversation": conversation, "wa_account": account, "wa_contact": wa_contact,
        "direction": "Inbound", "status": "Received", "source": "Customer", "message_id": wamid,
        "dedupe_key": dedupe_key, "timestamp": ts, "reply_to": reply_to,
        **{k: v for k, v in parsed.items() if k in ("message_type", "content", "media_id", "media_mime",
                                                     "filename", "interactive_reply_id")},
    })
    try:
        doc.insert(ignore_permissions=True)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.clear_last_message()
        return

    preview = parsed["content"] or f"[{parsed['message_type']}]"
    # Atomic update (never read-modify-write a counter), in its own fresh transaction.
    fresh_write(lambda: frappe.db.sql(
        """update `tabWA Conversation`
           set unread_count = unread_count + 1, last_message = %(preview)s, last_message_at = %(now)s,
               last_inbound_at = %(now)s, awaiting_reply_since = coalesce(awaiting_reply_since, %(now)s),
               status = case when status = 'Resolved' then 'Open' else status end
           where name = %(name)s""",
        {"preview": preview[:300], "now": now_datetime(), "name": conversation},
    ))
    _flag_broadcast_reply(wa_contact)
    frappe.db.commit()

    if parsed["media_id"] and settings().mirror_inbound_media:
        _mirror_media(doc, account)

    publish("message", conversation, {"message": doc.name, "inbound": 1})

    text = parsed["content"] or ""
    if _handle_opt_keywords(conversation, wa_contact, text):
        frappe.db.commit()
        return

    from waerp_crm.engines.dispatch import dispatch_inbound
    dispatch_inbound(frappe._dict(
        account=account, conversation=conversation, wa_contact=wa_contact, message=doc.name, wamid=wamid,
        text=text, interactive_reply_id=parsed["interactive_reply_id"], message_type=parsed["message_type"],
        is_first_inbound=is_first_inbound, contact_created=contact_created,
    ))
    frappe.db.commit()


def meta_timestamp(value) -> datetime:
    """Meta sends unix seconds (UTC); store as naive system-timezone datetime like Frappe does."""
    if not value:
        return now_datetime()
    utc = datetime.fromtimestamp(int(value), tz=timezone.utc)
    return frappe.utils.convert_utc_to_system_timezone(utc).replace(tzinfo=None)


def _flag_broadcast_reply(wa_contact: str):
    rcpt = frappe.db.sql(
        """select name, status, broadcast from `tabWA Broadcast Recipient`
           where wa_contact=%s and status in ('Sent','Delivered','Read') and sent_at >= %s
           order by sent_at desc limit 1""",
        (wa_contact, add_to_date(now_datetime(), days=-3)), as_dict=True,
    )
    if rcpt:
        from waerp_crm.engines.broadcasts import transition_recipient
        transition_recipient(rcpt[0], "Replied")


def _mirror_media(doc, account: str):
    """Download inbound media from Meta (URLs expire) into a private File."""
    from waerp_crm.whatsapp.graph import GraphClient
    try:
        content, mime = GraphClient(account).download_media(doc.media_id)
        import mimetypes
        ext = mimetypes.guess_extension((mime or "").split(";")[0]) or ""
        fname = doc.filename or f"{doc.message_type}-{doc.name}{ext}"
        f = frappe.get_doc({"doctype": "File", "file_name": fname, "content": content, "is_private": 1,
                            "attached_to_doctype": "WA Message", "attached_to_name": doc.name})
        f.save(ignore_permissions=True)
        doc.db_set("media_file", f.file_url, update_modified=False)
        frappe.db.commit()
    except Exception:
        frappe.log_error(title="WAERP CRM: media mirror failed", reference_doctype="WA Message", reference_name=doc.name)


def _handle_opt_keywords(conversation: str, wa_contact: str, text: str) -> bool:
    """STOP / START compliance. Returns True when the message was a keyword."""
    s = settings()
    t = (text or "").strip().upper()
    if not t:
        return False
    from waerp_crm.whatsapp.outbound import send_text
    if t in {k.upper() for k in split_lines(s.opt_out_keywords)}:
        frappe.db.set_value("WA Contact", wa_contact, {"opt_status": "Opted Out", "opted_out_on": now_datetime()})
        frappe.db.set_value("WA Flow Run", {"wa_contact": wa_contact, "status": "Active"},
                            {"status": "Completed", "ended_reason": "opted_out"})
        from waerp_crm.engines.outbound_webhooks import emit
        emit("contact.opted_out", {"wa_contact": wa_contact})
        if s.opt_out_reply:
            send_text(conversation, s.opt_out_reply, source="System", raise_on_error=False)
        return True
    if t in {k.upper() for k in split_lines(s.opt_in_keywords)}:
        frappe.db.set_value("WA Contact", wa_contact, {"opt_status": "Opted In", "opted_out_on": None})
        if s.opt_in_reply:
            send_text(conversation, s.opt_in_reply, source="System", raise_on_error=False)
        return True
    return False
