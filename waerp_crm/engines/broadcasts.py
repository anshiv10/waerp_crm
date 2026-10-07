"""Engine 5 — Broadcasts (template campaigns).

Lifecycle: Draft -> (Scheduled) -> Queued -> Sending -> Completed | Failed
           Sending -> Paused (manual) -> Sending (resume) ; any -> Cancelled

* Audience is materialised ONCE into WA Broadcast Recipient rows (deduped,
  opted-out excluded), so the campaign is resumable and auditable.
* Delivery runs on the `long` queue, commits after every recipient, checks
  for Pause/Cancel every batch, and honours an optional per-minute throttle.
* Status webhooks advance each recipient forward-only along
  Pending -> Sent -> Delivered -> Read -> Replied; Failed only before delivery.
* Counters are cumulative and bumped atomically in SQL on each transition.
"""

from __future__ import annotations

import time

import frappe
from frappe import _
from frappe.utils import now_datetime

from waerp_crm.core import logic
from waerp_crm.utils import get_or_create_conversation, get_or_create_wa_contact, norm

BATCH = 100


# ------------------------------------------------------------------ audience


def _customer_numbers(filters: dict) -> list[tuple[str, str | None]]:
    customers = frappe.get_all("Customer", filters={**filters, "disabled": 0}, pluck="name")
    out = []
    for cust in customers:
        rows = frappe.db.sql(
            """select c.mobile_no, c.phone from `tabContact` c
               join `tabDynamic Link` dl on dl.parent=c.name and dl.parenttype='Contact'
               where dl.link_doctype='Customer' and dl.link_name=%s
               order by c.is_primary_contact desc limit 1""", cust, as_dict=True)
        number = (rows[0].mobile_no or rows[0].phone) if rows else frappe.db.get_value("Customer", cust, "mobile_no")
        if number:
            out.append((number, cust))
    return out


def build_audience(b) -> list[dict]:
    """-> [{'wa_contact', 'phone', 'overrides'}] deduplicated by phone, opted-out removed."""
    raw: list[dict] = []
    if b.audience_type == "Tag":
        for c in frappe.db.sql("""select wc.name, wc.wa_id from `tabWA Contact` wc
                                  join `tabWA Contact Tag` t on t.parent=wc.name and t.parenttype='WA Contact'
                                  where t.tag=%s and wc.wa_id is not null""", b.tag, as_dict=True):
            raw.append({"wa_contact": c.name, "phone": c.wa_id})
    elif b.audience_type == "All Opted-In Contacts":
        for c in frappe.get_all("WA Contact", filters={"opt_status": "Opted In", "wa_id": ("is", "set")},
                                fields=["name", "wa_id"]):
            raw.append({"wa_contact": c.name, "phone": c.wa_id})
    elif b.audience_type in ("Customer Group", "Territory"):
        f = {"customer_group": b.customer_group} if b.audience_type == "Customer Group" else {"territory": b.territory}
        for number, cust in _customer_numbers(f):
            wc, _created = get_or_create_wa_contact(number)
            if cust and not frappe.db.get_value("WA Contact", wc, "customer"):
                frappe.db.set_value("WA Contact", wc, "customer", cust)
            raw.append({"wa_contact": wc, "phone": frappe.db.get_value("WA Contact", wc, "wa_id")})
    elif b.audience_type == "Manual Numbers":
        for line in logic.split_lines(b.manual_numbers):
            parts = [p.strip() for p in line.split(",")]
            wc, _created = get_or_create_wa_contact(parts[0])
            raw.append({"wa_contact": wc, "phone": norm(parts[0]), "overrides": parts[1:]})

    seen, out = set(), []
    opted_out = set(frappe.get_all("WA Contact", filters={"opt_status": "Opted Out"}, pluck="name"))
    for r in raw:
        if not r["phone"] or r["phone"] in seen or r["wa_contact"] in opted_out:
            continue
        seen.add(r["phone"])
        out.append(r)
    return out


def _params_for(b, tpl, wa_contact: str, overrides: list[str] | None) -> list[str]:
    from waerp_crm.utils import contact_namespace
    contact = contact_namespace(wa_contact)
    customer = {}
    if contact.get("customer"):
        customer = frappe.db.get_value("Customer", contact["customer"], "*", as_dict=True) or {}
    by_pos = {int(p.position): p.value for p in b.params}
    values = []
    for i in range(1, (tpl.body_param_count or 0) + 1):
        if overrides and len(overrides) >= i and overrides[i - 1]:
            values.append(overrides[i - 1])
        else:
            values.append(logic.interpolate(by_pos.get(i, ""), {"contact": contact, "customer": customer}) or "-")
    return values


# ------------------------------------------------------------------ actions (whitelisted via doc controller)


def queue_broadcast(name: str):
    b = frappe.get_doc("WA Broadcast", name)
    if b.status not in ("Draft", "Scheduled"):
        frappe.throw(_("Only Draft or Scheduled broadcasts can be started."))
    tpl = frappe.get_doc("WA Template", b.template)
    if tpl.status != "APPROVED":
        frappe.throw(_("Template {0} is not APPROVED (current: {1}). Sync templates first.").format(tpl.template_name, tpl.status))
    if tpl.wa_account != b.wa_account:
        frappe.throw(_("Template belongs to a different WA Account."))
    if tpl.header_type in ("IMAGE", "VIDEO", "DOCUMENT") and not b.header_media:
        frappe.throw(_("This template needs Header Media."))
    audience = build_audience(b)
    if not audience:
        frappe.throw(_("The audience is empty (or everyone opted out)."))
    frappe.db.delete("WA Broadcast Recipient", {"broadcast": b.name})
    now = now_datetime()
    rows = []
    for r in audience:
        rows.append((frappe.generate_hash(length=12), now, now, frappe.session.user, frappe.session.user, b.name,
                     r["wa_contact"], r["phone"], "Pending",
                     frappe.as_json(_params_for(b, tpl, r["wa_contact"], r.get("overrides")))))
    frappe.db.bulk_insert("WA Broadcast Recipient",
                          ["name", "creation", "modified", "owner", "modified_by", "broadcast", "wa_contact",
                           "phone", "status", "params"], rows)
    b.db_set({"status": "Queued", "total_count": len(rows), "sent_count": 0, "delivered_count": 0, "read_count": 0,
              "replied_count": 0, "failed_count": 0, "started_at": now, "completed_at": None})
    frappe.db.commit()
    frappe.enqueue("waerp_crm.engines.broadcasts.deliver", queue="long", timeout=6 * 3600, broadcast=b.name,
                   job_id=f"wa_broadcast::{b.name}", deduplicate=True)
    return len(rows)


def deliver(broadcast: str):
    frappe.set_user("Administrator")
    b = frappe.get_doc("WA Broadcast", broadcast)
    if b.status not in ("Queued", "Sending"):
        return
    b.db_set("status", "Sending")
    frappe.db.commit()
    gap = 60.0 / b.throttle_per_minute if b.throttle_per_minute else 0
    while True:
        status = frappe.db.get_value("WA Broadcast", broadcast, "status")
        if status != "Sending":
            return  # paused or cancelled
        batch = frappe.get_all("WA Broadcast Recipient", filters={"broadcast": broadcast, "status": "Pending"},
                               fields=["name", "wa_contact", "phone", "params"], limit=BATCH, order_by="creation asc")
        if not batch:
            break
        for r in batch:
            started = time.monotonic()
            _send_one(b, r)
            frappe.db.commit()
            if gap:
                time.sleep(max(0.0, gap - (time.monotonic() - started)))
    finalize(broadcast)


def _send_one(b, r):
    from waerp_crm.whatsapp.graph import WhatsAppAPIError
    from waerp_crm.whatsapp.outbound import send_template

    conv, _ = get_or_create_conversation(b.wa_account, r.wa_contact)
    last_error = None
    for variant in logic.phone_variants(r.phone):
        try:
            if variant != r.phone:
                frappe.db.set_value("WA Contact", r.wa_contact, "wa_id", variant)
            msg = send_template(conv, b.template, logic.parse_json(r.params, []), header_file=b.header_media,
                                source="Broadcast", raise_on_error=True)
            frappe.db.set_value("WA Broadcast Recipient", r.name, {"status": "Sent", "message_id": msg.message_id,
                                                                   "sent_at": now_datetime(), "error_message": None})
            _bump(b.name, logic.recipient_counter_columns("Pending", "Sent"))
            return
        except WhatsAppAPIError as e:
            last_error = str(e)
            if not e.is_recipient_not_allowed:
                break
        except Exception as e:  # opt-out guard, bad template data, ...
            last_error = str(e)
            break
    if frappe.db.get_value("WA Contact", r.wa_contact, "wa_id") != r.phone:
        frappe.db.set_value("WA Contact", r.wa_contact, "wa_id", r.phone)  # undo variant attempt
    frappe.db.set_value("WA Broadcast Recipient", r.name, {"status": "Failed", "error_message": (last_error or "")[:1000]})
    _bump(b.name, ["failed_count"])


def _bump(broadcast: str, cols: list[str]):
    if not cols:
        return
    sets = ", ".join(f"`{c}` = `{c}` + 1" for c in cols)
    frappe.db.sql(f"update `tabWA Broadcast` set {sets} where name=%s", broadcast)


def transition_recipient(rcpt, incoming: str, error: str | None = None):
    """Called from status webhooks and inbound replies."""
    if not logic.is_valid_transition(rcpt.status, incoming, logic.RECIPIENT_LADDER):
        return
    # Conditional UPDATE = no lost update if two status events race.
    frappe.db.sql("update `tabWA Broadcast Recipient` set status=%s, error_message=coalesce(%s, error_message) "
                  "where name=%s and status=%s", (incoming, error, rcpt.name, rcpt.status))
    if not frappe.db._cursor.rowcount:
        return
    if incoming == "Failed":
        # It was counted as sent; move it to failed.
        frappe.db.sql("update `tabWA Broadcast` set failed_count=failed_count+1, "
                      "sent_count=greatest(sent_count-1, 0) where name=%s", rcpt.broadcast)
    else:
        _bump(rcpt.broadcast, logic.recipient_counter_columns(rcpt.status, incoming))


def finalize(broadcast: str):
    counts = dict(frappe.db.sql("select status, count(*) from `tabWA Broadcast Recipient` where broadcast=%s "
                                "group by status", broadcast))
    if counts.get("Pending"):
        return
    total = sum(counts.values())
    status = "Failed" if total and counts.get("Failed", 0) == total else "Completed"
    frappe.db.set_value("WA Broadcast", broadcast, {"status": status, "completed_at": now_datetime()})
    frappe.db.commit()


def recompute_counts(broadcast: str):
    """Repair tool: rebuild the cumulative counters from recipient rows."""
    counts = dict(frappe.db.sql("select status, count(*) from `tabWA Broadcast Recipient` where broadcast=%s "
                                "group by status", broadcast))
    ladder = logic.RECIPIENT_LADDER

    def at_least(s):
        return sum(v for k, v in counts.items() if k in ladder and ladder.index(k) >= ladder.index(s))

    frappe.db.set_value("WA Broadcast", broadcast, {
        "total_count": sum(counts.values()), "sent_count": at_least("Sent"), "delivered_count": at_least("Delivered"),
        "read_count": at_least("Read"), "replied_count": at_least("Replied"), "failed_count": counts.get("Failed", 0),
    })


def process_scheduled():
    """Scheduler, every minute: start broadcasts whose time has come."""
    for name in frappe.get_all("WA Broadcast", filters={"status": "Scheduled", "scheduled_at": ("<=", now_datetime())},
                               pluck="name"):
        try:
            queue_broadcast(name)
        except Exception:
            frappe.db.rollback()
            frappe.db.set_value("WA Broadcast", name, "status", "Failed")
            frappe.log_error(title=f"WAERP CRM: scheduled broadcast {name} failed to start")
        frappe.db.commit()
