"""ERP superpower — WA Notification Rules: send a template (optionally with the
document's PDF) when any ERPNext document is submitted / cancelled / created /
saved / changes a field, or N days after/before a date (payment reminders).

doc_events hook "*" calls `on_doc_event`; it is O(1) for doctypes without rules
(cached set of doctypes). Sending is enqueued after commit so a Submit never
waits on Meta.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, nowdate

CACHE_KEY = "waerp_crm:notification_doctypes"
EVENT_MAP = {"on_submit": "Submit", "on_cancel": "Cancel", "after_insert": "New", "on_update": "Save"}


def _rule_doctypes() -> set[str]:
    val = frappe.cache.get_value(CACHE_KEY)
    if val is None:
        val = list(set(frappe.get_all("WA Notification Rule", filters={"enabled": 1}, pluck="document_type")))
        frappe.cache.set_value(CACHE_KEY, val)
    return set(val)


def clear_cache(doc=None, method=None):
    frappe.cache.delete_value(CACHE_KEY)


def on_doc_event(doc, method=None):
    if doc.doctype.startswith("WA ") or frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
        return
    try:
        if doc.doctype not in _rule_doctypes():
            return
    except Exception:
        return
    event = EVENT_MAP.get(method)
    rules = frappe.get_all("WA Notification Rule", filters={"enabled": 1, "document_type": doc.doctype},
                           fields=["name", "event", "value_changed", "condition"])
    for r in rules:
        fire = r.event == event
        if method == "on_update" and r.event == "Value Change" and r.value_changed:
            fire = doc.get_doc_before_save() is not None and doc.has_value_changed(r.value_changed)
        if not fire:
            continue
        if r.condition and not frappe.safe_eval(r.condition, None, {"doc": doc}):
            continue
        frappe.enqueue("waerp_crm.engines.notifications.send_for_rule", queue="short", rule=r.name,
                       doctype=doc.doctype, name=doc.name, enqueue_after_commit=True)


def resolve_phone(doc, fieldname: str) -> str | None:
    """Field may hold a number, or link to Contact / Customer / Lead / Supplier (e.g. contact_person, party)."""
    value = doc.get(fieldname)
    if not value:
        return None
    df = doc.meta.get_field(fieldname)
    target = None
    if df and df.fieldtype == "Link":
        target = df.options
    elif df and df.fieldtype == "Dynamic Link":
        target = doc.get(df.options)
    if target == "Contact":
        return frappe.db.get_value("Contact", value, "mobile_no") or frappe.db.get_value("Contact", value, "phone")
    if target in ("Customer", "Lead", "Supplier"):
        if frappe.get_meta(target).has_field("mobile_no"):
            direct = frappe.db.get_value(target, value, "mobile_no")
            if direct:
                return direct
        row = frappe.db.sql(
            """select c.mobile_no from `tabContact` c join `tabDynamic Link` dl
               on dl.parent=c.name and dl.parenttype='Contact'
               where dl.link_doctype=%s and dl.link_name=%s and coalesce(c.mobile_no, '') != ''
               order by c.is_primary_contact desc limit 1""", (target, value))
        return row[0][0] if row else None
    return value


def send_for_rule(rule: str, doctype: str, name: str):
    from waerp_crm.whatsapp.outbound import conversation_for_number, send_template

    frappe.set_user("Administrator")
    r = frappe.get_doc("WA Notification Rule", rule)
    doc = frappe.get_doc(doctype, name)
    phone = resolve_phone(doc, r.phone_field)
    if not phone:
        frappe.log_error(title=f"WAERP CRM: {doctype} {name} has no {r.phone_field}", reference_doctype=doctype,
                         reference_name=name)
        return
    params = [frappe.render_template(p.value, {"doc": doc})
              for p in sorted(r.params, key=lambda p: p.position)]
    header_pdf = None
    if r.attach_print:
        from waerp_crm.utils import render_pdf
        pdf = render_pdf(doctype, name, r.print_format)
        header_pdf = (pdf, f"{name}.pdf")
    party_name = doc.get("customer_name") or doc.get("contact_display") or doc.get("party_name")
    conv = conversation_for_number(phone, r.wa_account, name=party_name)
    send_template(conv, r.template, params, header_pdf=header_pdf, source="Notification",
                  reference_doctype=doctype, reference_name=name)
    frappe.db.commit()


def run_date_rules():
    """Daily: 'Days After' rules, e.g. reminder 3 days after due_date while unpaid."""
    for r in frappe.get_all("WA Notification Rule", filters={"enabled": 1, "event": "Days After"},
                            fields=["name", "document_type", "date_field", "days", "condition"]):
        target = add_days(getdate(nowdate()), -(r.days or 0))
        filters = {r.date_field: target}
        if frappe.get_meta(r.document_type).is_submittable:
            filters["docstatus"] = 1
        for name in frappe.get_all(r.document_type, filters=filters, pluck="name", limit=5000):
            doc = frappe.get_doc(r.document_type, name)
            if r.condition and not frappe.safe_eval(r.condition, None, {"doc": doc}):
                continue
            frappe.enqueue("waerp_crm.engines.notifications.send_for_rule", queue="short", rule=r.name,
                           doctype=r.document_type, name=name)


@frappe.whitelist()
def send_document(doctype: str, name: str, template: str, phone: str | None = None, attach_pdf: int = 1,
                  print_format: str | None = None, params: str | None = None):
    """'Send via WhatsApp' button on any ERP form."""
    from waerp_crm.whatsapp.outbound import conversation_for_number, send_template

    doc = frappe.get_doc(doctype, name)
    doc.check_permission("read")
    phone = phone or next((resolve_phone(doc, f) for f in ("contact_mobile", "mobile_no", "contact_phone",
                                                             "contact_person", "customer", "party")
                           if doc.meta.has_field(f) and resolve_phone(doc, f)), None)
    if not phone:
        frappe.throw("No mobile number on this document. Enter one.")
    tpl = frappe.get_doc("WA Template", template)
    body = frappe.parse_json(params) if params else []
    header_pdf = None
    if attach_pdf and tpl.header_type == "DOCUMENT":
        from waerp_crm.utils import render_pdf
        header_pdf = (render_pdf(doctype, name, print_format), f"{name}.pdf")
    conv = conversation_for_number(phone, tpl.wa_account, name=doc.get("customer_name") or doc.get("party_name"))
    msg = send_template(conv, template, body, header_pdf=header_pdf, source="Agent",
                        reference_doctype=doctype, reference_name=name)
    return {"conversation": conv, "message": msg.name, "status": msg.status}
