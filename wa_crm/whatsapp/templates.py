"""Template lifecycle: sync from Meta, submit to Meta, status/quality webhooks."""

from __future__ import annotations

import json

import frappe
from frappe import _

from wa_crm.core.logic import count_placeholders
from wa_crm.whatsapp.graph import GraphClient


def _upsert(account: str, t: dict):
    comps = t.get("components") or []
    header = next((c for c in comps if c.get("type") == "HEADER"), {})
    body = next((c for c in comps if c.get("type") == "BODY"), {})
    footer = next((c for c in comps if c.get("type") == "FOOTER"), {})
    buttons = next((c for c in comps if c.get("type") == "BUTTONS"), {})
    name = f"{t['name']}-{t['language']}-{account}"
    values = {
        "template_name": t["name"], "language": t["language"], "wa_account": account,
        "category": t.get("category") if t.get("category") in ("UTILITY", "MARKETING", "AUTHENTICATION") else "UTILITY",
        "status": t.get("status"), "meta_template_id": t.get("id"),
        "quality": (t.get("quality_score") or {}).get("score"),
        "rejected_reason": t.get("rejected_reason") if t.get("rejected_reason") not in (None, "NONE") else None,
        "header_type": (header.get("format") or "NONE") if header else "NONE",
        "header_text": header.get("text") if header.get("format") == "TEXT" else None,
        "body": body.get("text") or "",
        "footer": footer.get("text"),
        "buttons": json.dumps(buttons.get("buttons") or []),
        "components": json.dumps(comps),
        "body_param_count": count_placeholders(body.get("text")),
        "header_param_count": count_placeholders(header.get("text")),
    }
    if values["header_type"] not in ("NONE", "TEXT", "IMAGE", "VIDEO", "DOCUMENT"):
        values["header_type"] = "NONE"  # LOCATION headers etc.
    if frappe.db.exists("WA Template", name):
        doc = frappe.get_doc("WA Template", name)
        doc.update(values)
        doc.flags.from_meta = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({"doctype": "WA Template", **values})
        doc.flags.from_meta = True
        doc.insert(ignore_permissions=True)


@frappe.whitelist()
def sync_templates(account: str | None = None):
    frappe.only_for(("System Manager", "WA Manager"))
    accounts = [account] if account else frappe.get_all("WA Account", filters={"enabled": 1}, pluck="name")
    count = 0
    for acc in accounts:
        for t in GraphClient(acc).list_templates():
            _upsert(acc, t)
            count += 1
    frappe.db.commit()
    return count


def sync_all_daily():
    try:
        frappe.set_user("Administrator")
        sync_templates()
    except Exception:
        frappe.log_error(title="WA CRM: daily template sync failed")


@frappe.whitelist()
def submit_template(name: str):
    """Create the template on Meta for approval from our WA Template doc."""
    frappe.only_for(("System Manager", "WA Manager"))
    doc = frappe.get_doc("WA Template", name)
    comps = []
    if doc.header_type == "TEXT" and doc.header_text:
        comps.append({"type": "HEADER", "format": "TEXT", "text": doc.header_text})
    elif doc.header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
        frappe.throw(_("Media-header templates must be created in Meta WhatsApp Manager (needs a sample upload); then click Sync."))
    body = {"type": "BODY", "text": doc.body}
    n = count_placeholders(doc.body)
    if n:
        samples = [s.strip() for s in (doc.sample_values or "").split(",") if s.strip()]
        if len(samples) < n:
            frappe.throw(_("Meta requires {0} sample values (comma separated) for the body variables.").format(n))
        body["example"] = {"body_text": [samples[:n]]}
    comps.append(body)
    if doc.footer:
        comps.append({"type": "FOOTER", "text": doc.footer})
    buttons = frappe.parse_json(doc.buttons) or []
    if buttons:
        comps.append({"type": "BUTTONS", "buttons": buttons})
    res = GraphClient(doc.wa_account).create_template(
        {"name": doc.template_name, "language": doc.language, "category": doc.category, "components": comps}
    )
    doc.db_set({"meta_template_id": res.get("id"), "status": res.get("status") or "PENDING",
                "components": json.dumps(comps)})
    return res


def handle_template_event(field: str, value: dict, waba_id: str | None = None):
    tid = str(value.get("message_template_id") or "")
    name = frappe.db.get_value("WA Template", {"meta_template_id": tid}, "name") if tid else None
    if not name and value.get("message_template_name"):
        name = frappe.db.get_value("WA Template", {"template_name": value["message_template_name"],
                                                    "language": value.get("message_template_language")}, "name")
    if not name:
        return
    updates = {}
    if field == "message_template_status_update":
        updates["status"] = value.get("event")
        if value.get("reason") and value.get("reason") != "NONE":
            updates["rejected_reason"] = value.get("reason")
    elif field == "message_template_quality_update":
        updates["quality"] = value.get("new_quality_score")
    elif field == "template_category_update" and value.get("new_category") in ("UTILITY", "MARKETING", "AUTHENTICATION"):
        updates["category"] = value["new_category"]
    if updates:
        frappe.db.set_value("WA Template", name, updates)
