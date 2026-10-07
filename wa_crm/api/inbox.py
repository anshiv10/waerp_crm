"""Whitelisted endpoints used by the Desk inbox page (/app/wa-inbox)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from wa_crm.utils import SESSION_WINDOW_HOURS, add_note, assign_conversation


def _can(conversation: str, ptype: str = "write"):
    frappe.has_permission("WA Conversation", ptype, conversation, throw=True)


@frappe.whitelist()
def get_conversations(view: str = "all", status: str = "Open", search: str | None = None,
                      account: str | None = None, start: int = 0, page_length: int = 50):
    filters = {}
    if status and status != "All":
        filters["status"] = status
    if account:
        filters["wa_account"] = account
    if view == "mine":
        filters["assigned_to"] = frappe.session.user
    elif view == "unassigned":
        filters["assigned_to"] = ("is", "not set")
    elif view == "unread":
        filters["unread_count"] = (">", 0)
    elif view == "waiting":
        filters["awaiting_reply_since"] = ("is", "set")
    or_filters = None
    if search:
        like = f"%{search}%"
        or_filters = [["contact_name", "like", like], ["wa_id", "like", like], ["last_message", "like", like]]
    rows = frappe.get_list(
        "WA Conversation", filters=filters, or_filters=or_filters,
        fields=["name", "contact_name", "wa_id", "customer", "status", "assigned_to", "unread_count", "last_message",
                "last_message_at", "last_inbound_at", "awaiting_reply_since", "priority", "wa_account",
                "ai_autoreply_disabled"],
        order_by="last_message_at desc", start=int(start), page_length=int(page_length),
    )
    cutoff = add_to_date(now_datetime(), hours=-SESSION_WINDOW_HOURS)
    for r in rows:
        r["session_open"] = bool(r.last_inbound_at and get_datetime(r.last_inbound_at) > cutoff)
    return rows


@frappe.whitelist()
def get_conversation(conversation: str, before: str | None = None, limit: int = 50):
    _can(conversation, "read")
    conv = frappe.get_doc("WA Conversation", conversation)
    filters = {"conversation": conversation}
    if before:
        filters["timestamp"] = ("<", before)
    messages = frappe.get_all(
        "WA Message", filters=filters,
        fields=["name", "direction", "message_type", "content", "media_file", "media_mime", "filename", "status",
                "timestamp", "source", "sent_by", "is_note", "is_ai_generated", "reaction", "reply_to", "error_message",
                "template", "reference_doctype", "reference_name"],
        order_by="timestamp desc, creation desc", limit=int(limit),
    )
    contact = frappe.get_doc("WA Contact", conv.wa_contact)
    cutoff = add_to_date(now_datetime(), hours=-SESSION_WINDOW_HOURS)
    return {
        "conversation": conv.as_dict(),
        "session_open": bool(conv.last_inbound_at and get_datetime(conv.last_inbound_at) > cutoff),
        "messages": list(reversed(messages)),
        "contact": {"name": contact.name, "full_name": contact.full_name, "wa_id": contact.wa_id,
                    "customer": contact.customer, "lead": contact.lead, "contact": contact.contact,
                    "opt_status": contact.opt_status, "tags": [t.tag for t in contact.tags],
                    "attributes": frappe.parse_json(contact.attributes) or {}},
        "active_flow": frappe.db.get_value("WA Flow Run", {"wa_contact": conv.wa_contact, "status": "Active"},
                                           ["name", "flow", "current_node_key"], as_dict=True),
    }


@frappe.whitelist()
def mark_read(conversation: str):
    _can(conversation, "read")
    frappe.db.set_value("WA Conversation", conversation, "unread_count", 0, update_modified=False)
    last_in = frappe.db.get_value("WA Message", {"conversation": conversation, "direction": "Inbound"},
                                  ["message_id", "wa_account"], as_dict=True, order_by="timestamp desc")
    if last_in and last_in.message_id:
        try:
            from wa_crm.whatsapp.graph import GraphClient
            GraphClient(last_in.wa_account).mark_read(last_in.message_id)
        except Exception:
            pass  # blue ticks are best-effort


@frappe.whitelist()
def send_text(conversation: str, text: str, reply_to: str | None = None):
    _can(conversation)
    from wa_crm.whatsapp.outbound import send_text as _send
    return _send(conversation, text, source="Agent", reply_to=reply_to).name


@frappe.whitelist()
def send_media(conversation: str, file_url: str, caption: str | None = None):
    _can(conversation)
    from wa_crm.whatsapp.outbound import send_media as _send
    return _send(conversation, file_url=file_url, caption=caption, source="Agent").name


@frappe.whitelist()
def send_template(conversation: str, template: str, params: str | None = None, header_file: str | None = None):
    _can(conversation)
    from wa_crm.whatsapp.outbound import send_template as _send
    return _send(conversation, template, frappe.parse_json(params) or [], header_file=header_file, source="Agent").name


@frappe.whitelist()
def react(message: str, emoji: str):
    conv = frappe.db.get_value("WA Message", message, "conversation")
    _can(conv)
    from wa_crm.whatsapp.outbound import send_reaction
    send_reaction(message, emoji)


@frappe.whitelist()
def note(conversation: str, text: str):
    _can(conversation)
    return add_note(conversation, text, user=frappe.session.user)


@frappe.whitelist()
def assign(conversation: str, user: str | None = None):
    _can(conversation)
    if user == "__auto__":
        return assign_conversation(conversation, None, auto=True)
    doc = frappe.get_doc("WA Conversation", conversation)
    doc.assigned_to = user or None
    doc.save()
    return doc.assigned_to


@frappe.whitelist()
def set_status(conversation: str, status: str):
    _can(conversation)
    if status not in ("Open", "Pending", "Resolved"):
        frappe.throw(_("Invalid status"))
    doc = frappe.get_doc("WA Conversation", conversation)
    doc.status = status
    if status == "Resolved":
        doc.awaiting_reply_since = None
    doc.save()


@frappe.whitelist()
def toggle_ai(conversation: str, paused: int):
    _can(conversation)
    frappe.db.set_value("WA Conversation", conversation, {"ai_autoreply_disabled": int(paused),
                                                          **({"ai_reply_count": 0} if not int(paused) else {})})


@frappe.whitelist()
def start_flow(conversation: str, flow: str):
    _can(conversation)
    from wa_crm.engines.flows import start_run
    wa_contact = frappe.db.get_value("WA Conversation", conversation, "wa_contact")
    return start_run(flow, wa_contact, conversation)


@frappe.whitelist()
def start_chat(phone: str, account: str | None = None, name: str | None = None):
    """New outbound conversation from a number (first message must be a template)."""
    frappe.has_permission("WA Conversation", "create", throw=True)
    from wa_crm.whatsapp.outbound import conversation_for_number
    return conversation_for_number(phone, account, name=name)


@frappe.whitelist()
def get_templates(account: str):
    return frappe.get_all("WA Template", filters={"wa_account": account, "status": "APPROVED"},
                          fields=["name", "template_name", "language", "category", "body", "body_param_count",
                                  "header_type"], order_by="template_name asc")


@frappe.whitelist()
def get_quick_replies():
    return frappe.get_all("WA Quick Reply", fields=["shortcut", "message"], order_by="shortcut asc")


@frappe.whitelist()
def get_agents(account: str):
    users = [a.user for a in frappe.get_doc("WA Account", account).agents]
    if not users:
        users = frappe.get_all("Has Role", filters={"role": ("in", ["WA Agent", "WA Manager"]), "parenttype": "User"},
                               pluck="parent", distinct=True)
    return frappe.get_all("User", filters={"name": ("in", users or [""]), "enabled": 1},
                          fields=["name", "full_name"])
