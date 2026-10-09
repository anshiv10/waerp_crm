"""Frappe-aware shared helpers (contacts, conversations, locks, realtime, assignment)."""

from __future__ import annotations

import time
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.realtime import get_doctype_room
from frappe.utils import add_to_date, get_datetime, now_datetime

from waerp_crm.core.logic import last_digits, normalize_phone

REALTIME_EVENT = "waerp_crm_event"
SESSION_WINDOW_HOURS = 24


def settings():
    return frappe.get_cached_doc("WA CRM Settings")


def norm(phone: str | None) -> str:
    return normalize_phone(phone, settings().default_country_code or "")


def get_account(name: str | None = None):
    if name:
        return frappe.get_cached_doc("WA Account", name)
    default = frappe.db.get_value("WA Account", {"enabled": 1, "is_default": 1}, "name") or frappe.db.get_value(
        "WA Account", {"enabled": 1}, "name"
    )
    if not default:
        frappe.throw(_("No enabled WA Account is configured."))
    return frappe.get_cached_doc("WA Account", default)


@contextmanager
def conversation_lock(conversation: str, timeout: int = 120):
    """Serialise processing per conversation across all RQ workers (Redis lock).

    Keeps two inbound messages of the same chat from advancing a flow or
    double-claiming an AI reply slot concurrently.
    """
    lock = frappe.cache.lock(f"{frappe.local.site}:waerp_crm:conv:{conversation}", timeout=timeout,
                             blocking_timeout=timeout)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                pass


def publish(event_type: str, conversation: str, data: dict | None = None):
    """Push an inbox event to every desk user subscribed to WA Conversation."""
    frappe.publish_realtime(
        REALTIME_EVENT,
        {"type": event_type, "conversation": conversation, **(data or {})},
        room=get_doctype_room("WA Conversation"),
        after_commit=True,
    )


# ------------------------------------------------------------------ contacts


def _find_erp_contact(wa_id: str) -> tuple[str | None, str | None]:
    """Match an ERPNext Contact by its last 10 digits; return (contact, customer)."""
    tail = last_digits(wa_id)
    if len(tail) < 8:
        return None, None
    like = f"%{tail}"
    row = frappe.db.sql(
        """
        select c.name from `tabContact` c
        left join `tabContact Phone` p on p.parent = c.name and p.parenttype = 'Contact'
        where c.mobile_no like %(like)s or c.phone like %(like)s or p.phone like %(like)s
        order by c.modified desc limit 1
        """,
        {"like": like},
    )
    if not row:
        return None, None
    contact = row[0][0]
    customer = frappe.db.get_value(
        "Dynamic Link",
        {"parenttype": "Contact", "parent": contact, "link_doctype": "Customer"},
        "link_name",
    )
    return contact, customer


def get_or_create_wa_contact(wa_id: str | None, bsuid: str | None = None, name: str | None = None,
                             username: str | None = None, source: str | None = None) -> tuple[str, bool]:
    """Find the WA Contact for a sender (number or BSUID), creating it once. -> (name, created)."""
    wa_id = norm(wa_id) if wa_id else None
    existing = None
    if wa_id:
        existing = frappe.db.get_value("WA Contact", {"wa_id": wa_id}, "name")
    if not existing and bsuid:
        existing = frappe.db.get_value("WA Contact", {"bsuid": bsuid}, "name")
    if existing:
        updates = {}
        cur = frappe.db.get_value("WA Contact", existing, ["full_name", "wa_id", "bsuid"], as_dict=True)
        if name and not cur.full_name:
            updates["full_name"] = name
        if wa_id and not cur.wa_id:
            updates["wa_id"] = wa_id
        if bsuid and not cur.bsuid:
            updates["bsuid"] = bsuid
        if updates:
            frappe.db.set_value("WA Contact", existing, updates, update_modified=False)
        return existing, False

    contact, customer = _find_erp_contact(wa_id) if wa_id else (None, None)
    doc = frappe.get_doc({
        "doctype": "WA Contact",
        "full_name": name or wa_id or username,
        "wa_id": wa_id,
        "bsuid": bsuid,
        "username": username,
        "contact": contact,
        "customer": customer,
        "source": source,
        "first_seen": now_datetime(),
    })
    try:
        doc.insert(ignore_permissions=True)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        # Lost a race with a concurrent worker: the other insert won.
        frappe.clear_last_message()
        filters = {"wa_id": wa_id} if wa_id else {"bsuid": bsuid}
        return frappe.db.get_value("WA Contact", filters, "name"), False

    if not customer and settings().auto_create_lead and wa_id:
        try:
            ensure_lead(doc.name)
        except Exception:
            frappe.log_error(title="WAERP CRM: auto-create lead failed")
    return doc.name, True


def ensure_lead(wa_contact: str) -> str | None:
    c = frappe.get_doc("WA Contact", wa_contact)
    if c.lead or c.customer:
        return c.lead
    existing = frappe.db.get_value("Lead", {"mobile_no": ("like", f"%{last_digits(c.wa_id)}")}, "name")
    if existing:
        lead = existing
    else:
        s = settings()
        lead_doc = frappe.get_doc({
            "doctype": "Lead",
            "first_name": c.full_name or c.wa_id,
            "mobile_no": f"+{c.wa_id}" if c.wa_id else None,
            "whatsapp_no": f"+{c.wa_id}" if c.wa_id else None,
            "source": s.lead_source or None,
        })
        lead_doc.flags.ignore_mandatory = True
        lead_doc.insert(ignore_permissions=True)
        lead = lead_doc.name
    frappe.db.set_value("WA Contact", wa_contact, "lead", lead, update_modified=False)
    return lead


def get_or_create_conversation(account: str, wa_contact: str) -> tuple[str, bool]:
    """One conversation per (account, contact). Deterministic name makes this race-safe."""
    name = f"{account}-{wa_contact}"
    if frappe.db.exists("WA Conversation", name):
        return name, False
    doc = frappe.get_doc({"doctype": "WA Conversation", "wa_account": account, "wa_contact": wa_contact,
                          "status": "Open"})
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        frappe.clear_last_message()
        return name, False
    return doc.name, True


def session_open(conversation: str) -> bool:
    last = frappe.db.get_value("WA Conversation", conversation, "last_inbound_at")
    return bool(last and get_datetime(last) > add_to_date(now_datetime(), hours=-SESSION_WINDOW_HOURS))


def contact_namespace(wa_contact: str) -> dict:
    c = frappe.db.get_value("WA Contact", wa_contact,
                            ["name", "full_name", "wa_id", "customer", "lead", "attributes", "opt_status"],
                            as_dict=True) or {}
    attrs = frappe.parse_json(c.get("attributes")) or {}
    first = (c.get("full_name") or "").split(" ")[0]
    return {**attrs, **c, "first_name": first, "phone": c.get("wa_id")}


# ------------------------------------------------------------------ tags


def add_tag(wa_contact: str, tag: str) -> bool:
    """Add a tag if absent. Returns True when newly added (fires Tag Added)."""
    if not frappe.db.exists("WA Tag", tag):
        frappe.get_doc({"doctype": "WA Tag", "tag_name": tag}).insert(ignore_permissions=True)
    if frappe.db.exists("WA Contact Tag", {"parent": wa_contact, "parenttype": "WA Contact", "tag": tag}):
        return False
    doc = frappe.get_doc("WA Contact", wa_contact)
    doc.append("tags", {"tag": tag})
    doc.save(ignore_permissions=True)
    return True


def remove_tag(wa_contact: str, tag: str) -> bool:
    doc = frappe.get_doc("WA Contact", wa_contact)
    before = len(doc.tags)
    doc.tags = [t for t in doc.tags if t.tag != tag]
    if len(doc.tags) != before:
        doc.save(ignore_permissions=True)
        return True
    return False


def has_tag(wa_contact: str, tag: str) -> bool:
    return bool(frappe.db.exists("WA Contact Tag", {"parent": wa_contact, "parenttype": "WA Contact", "tag": tag}))


# ------------------------------------------------------------------ assignment


def pick_agent(account: str) -> str | None:
    """Round robin / load-balanced pick among the account's agents."""
    acc = frappe.get_doc("WA Account", account)
    if acc.assignment_mode == "None" or not acc.agents:
        return None
    users = [a for a in acc.agents if frappe.db.get_value("User", a.user, "enabled")]
    if not users:
        return None
    open_counts = dict(frappe.db.sql(
        """select assigned_to, count(*) from `tabWA Conversation`
           where wa_account=%s and status!='Resolved' and assigned_to is not null group by assigned_to""",
        account,
    ))
    eligible = [a for a in users if not a.max_open_conversations or open_counts.get(a.user, 0) < a.max_open_conversations]
    if not eligible:
        return None
    if acc.assignment_mode == "Load Balanced":
        choice = min(eligible, key=lambda a: open_counts.get(a.user, 0)).user
    else:
        order = [a.user for a in users]
        start = order.index(acc.last_assigned_user) + 1 if acc.last_assigned_user in order else 0
        rotated = order[start:] + order[:start]
        eligible_users = {a.user for a in eligible}
        choice = next(u for u in rotated if u in eligible_users)
    frappe.db.set_value("WA Account", account, "last_assigned_user", choice, update_modified=False)
    return choice


def assign_conversation(conversation: str, user: str | None = None, auto: bool = False) -> str | None:
    conv = frappe.get_doc("WA Conversation", conversation)
    if not user and auto:
        user = pick_agent(conv.wa_account)
    if user and conv.assigned_to != user:
        conv.assigned_to = user
        conv.save(ignore_permissions=True)
    return user


def add_note(conversation: str, text: str, user: str | None = None):
    """Internal note inside the thread — never sent to the customer."""
    conv = frappe.db.get_value("WA Conversation", conversation, ["wa_account", "wa_contact"], as_dict=True)
    doc = frappe.get_doc({
        "doctype": "WA Message", "conversation": conversation, "wa_account": conv.wa_account,
        "wa_contact": conv.wa_contact, "direction": "Outbound", "message_type": "note", "is_note": 1,
        "status": "Sent", "source": "System" if not user else "Agent", "sent_by": user,
        "content": text, "timestamp": now_datetime(),
    }).insert(ignore_permissions=True)
    publish("message", conversation, {"message": doc.name})
    return doc.name


def human_owns_conversation(conversation: str, wa_contact: str | None = None, window_hours: int = 24) -> bool:
    """True while a person owns the chat, so no bot (flow / fallback menu / AI) should talk.

    Human mode starts when a bot hands off, or the chat is assigned to an agent.
    It ends when an agent marks the chat Resolved or un-pauses the bot (bot_resumed_at),
    or `window_hours` after the handoff with no assignment.
    """
    conv = frappe.db.get_value("WA Conversation", conversation,
                               ["assigned_to", "bot_resumed_at", "wa_contact", "ai_autoreply_disabled"],
                               as_dict=True)
    if not conv:
        return False
    resumed = get_datetime(conv.bot_resumed_at) if conv.bot_resumed_at else None
    handed = frappe.db.get_value("WA Flow Run", {"wa_contact": wa_contact or conv.wa_contact, "status": "Handed Off"},
                                 "modified", order_by="modified desc")
    if handed and (not resumed or get_datetime(handed) > resumed) and \
            get_datetime(handed) > add_to_date(now_datetime(), hours=-window_hours):
        return True
    if conv.assigned_to:
        last_agent = frappe.db.get_value("WA Message", {"conversation": conversation, "direction": "Outbound",
                                                        "source": "Agent"}, "timestamp", order_by="timestamp desc")
        if not resumed or (last_agent and get_datetime(last_agent) > resumed):
            return True
    return False


def resume_bots(conversation: str):
    """End human mode: bots (flows, fallback menu, AI auto-reply) may talk again."""
    frappe.db.set_value("WA Conversation", conversation, {
        "bot_resumed_at": now_datetime(), "ai_autoreply_disabled": 0, "ai_reply_count": 0,
    }, update_modified=False)


def fresh_write(fn, attempts: int = 3):
    """Run a write against a hot row (e.g. WA Conversation) safely on MariaDB 11.8+/12.

    Those versions enforce snapshot isolation: writing a row that another transaction changed
    after ours began raises error 1020 (QueryDeadlockError). We commit first so the write runs
    in a brand-new, short transaction, and retry a couple of times if the row is busy.
    """
    for i in range(attempts):
        frappe.db.commit()
        try:
            result = fn()
            frappe.db.commit()
            return result
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            frappe.db.rollback()
            if i == attempts - 1:
                raise
            time.sleep(0.3 * (i + 1))
