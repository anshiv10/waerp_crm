"""WhatsApp-friendly text for ERP records (plain text, *bold*, short lines)."""

from __future__ import annotations

from urllib.parse import quote

import frappe
from frappe import _

from waerp_crm.assistant.resolve import amount_field, date_field, party_field

DOCSTATUS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
SKIP_TYPES = {"Section Break", "Column Break", "Tab Break", "HTML", "Table", "Table MultiSelect", "Button",
              "Image", "Attach Image", "Fold", "Heading", "Code", "JSON", "Password", "Signature", "Geolocation"}
KEY_FIELDS = ["status", "customer", "customer_name", "party_name", "supplier", "supplier_name", "party",
              "item", "production_item", "item_name", "bom_no", "qty", "produced_qty", "uom",
              "transaction_date", "posting_date", "delivery_date", "schedule_date", "due_date",
              "planned_start_date", "expected_delivery_date", "valid_till", "project", "cost_center",
              "total_qty", "net_total", "total_taxes_and_charges", "grand_total", "rounded_total",
              "outstanding_amount", "advance_paid", "paid_amount", "per_delivered", "per_billed", "per_received",
              "total_cost", "is_active", "is_default"]
MAX_TEXT = 3500


def fmt(value, df=None, doc=None) -> str:
    if value in (None, ""):
        return ""
    try:
        out = frappe.format_value(value, df=df, doc=doc)
    except Exception:
        out = str(value)
    return frappe.utils.strip_html(str(out)).strip()


def desk_url(doctype: str, name: str) -> str:
    from waerp_crm.waerp_crm.doctype.wa_account.wa_account import public_base_url

    return f"{public_base_url()}/app/{frappe.scrub(doctype).replace('_', '-')}/{quote(name, safe='')}"


def status_label(row) -> str:
    st = row.get("status")
    ds = DOCSTATUS.get(row.get("docstatus") or 0, "")
    return st if st and st != ds else ds


def list_fields(meta) -> list[str]:
    fields = ["name", "docstatus", "modified"]
    for df in (party_field(meta), amount_field(meta)):
        if df:
            fields.append(df.fieldname)
    d = date_field(meta)
    if d != "creation":
        fields.append(d)
    for f in ("status", "currency", "title", "customer_name", "supplier_name", "item_name", "production_item"):
        if meta.get_field(f):
            fields.append(f)
    title = meta.get_title_field()
    if title and meta.get_field(title):
        fields.append(title)
    return list(dict.fromkeys(fields))


def row_summary(meta, row) -> str:
    """'Harshit Dixit · 29-10-2026 · ₹ 11,800.00 · Draft'"""
    bits = []
    party = row.get("customer_name") or row.get("supplier_name") or row.get(meta.get_title_field() or "")
    pdf = party_field(meta)
    if not party and pdf:
        party = row.get(pdf.fieldname)
    if not party:
        party = row.get("item_name") or row.get("production_item")
    if party and party != row.name:
        bits.append(str(party))
    d = date_field(meta)
    if d != "creation" and row.get(d):
        bits.append(fmt(row.get(d), meta.get_field(d)))
    adf = amount_field(meta)
    if adf and row.get(adf.fieldname) is not None:
        bits.append(fmt(row.get(adf.fieldname), adf, row))
    bits.append(status_label(row))
    return " · ".join(b for b in bits if b)


def results_text(doctype: str, rows: list, described: list[str], more: bool) -> str:
    meta = frappe.get_meta(doctype)
    head = _("*{0} {1} found*").format(len(rows), doctype if len(rows) == 1 else _plural(doctype))
    if described:
        head += f" ({', '.join(described)})"
    lines = [head, ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"*{i}.* {r.name}\n    {row_summary(meta, r)}")
    if more:
        lines.append("")
        lines.append(_("Showing the latest {0}. Add filters to narrow down.").format(len(rows)))
    lines.append("")
    lines.append(_("Reply with a number (e.g. *1*) to open it."))
    return "\n".join(lines)[:MAX_TEXT]


def _plural(doctype: str) -> str:
    return doctype if doctype.endswith("s") else doctype + "s"


def details_text(doc) -> str:
    meta = frappe.get_meta(doc.doctype)
    lines = [f"📄 *{doc.doctype} {doc.name}*", f"{_('State')}: {DOCSTATUS.get(doc.docstatus, '')}"]
    seen = {"name", "docstatus"}
    order = [f for f in KEY_FIELDS if meta.get_field(f)]
    order += [df.fieldname for df in meta.fields
              if (df.in_list_view or df.bold or df.in_standard_filter) and df.fieldname not in order]
    title = meta.get_title_field()
    if title and title not in order and meta.get_field(title):
        order.insert(0, title)
    count = 0
    for f in order:
        df = meta.get_field(f)
        if not df or df.fieldtype in SKIP_TYPES or f in seen or df.hidden:
            continue
        seen.add(f)
        val = doc.get(f)
        if val in (None, "", 0) and df.fieldtype != "Check":
            continue
        if f in ("customer_name", "supplier_name") and val in (doc.get("customer"), doc.get("supplier")):
            continue
        text = fmt(val, df, doc)
        if not text:
            continue
        lines.append(f"{df.label or f}: {text}")
        count += 1
        if count >= 16:
            break
    table = _main_table(meta)
    if table:
        rows = doc.get(table.fieldname) or []
        if rows:
            lines.append("")
            lines.append(f"*{table.label or table.fieldname} ({len(rows)})*")
            cmeta = frappe.get_meta(table.options)
            for row in rows[:10]:
                lines.append("• " + _child_line(cmeta, row, doc))
            if len(rows) > 10:
                lines.append(_("…and {0} more").format(len(rows) - 10))
    lines.append("")
    lines.append(f"🔗 {desk_url(doc.doctype, doc.name)}")
    return "\n".join(lines)[:MAX_TEXT]


def _main_table(meta):
    tables = meta.get_table_fields()
    if not tables:
        return None
    return next((t for t in tables if t.fieldname == "items"), tables[0])


def _child_line(cmeta, row, parent) -> str:
    if cmeta.get_field("item_code"):
        name = row.get("item_code") or ""
        if row.get("item_name") and row.get("item_name") != name:
            name += f" {row.get('item_name')}"
        qty = row.get("qty")
        uom = row.get("uom") or row.get("stock_uom") or ""
        part = f"{name} × {fmt(qty, cmeta.get_field('qty'), row)} {uom}".strip()
        if cmeta.get_field("rate") and row.get("rate") is not None:
            part += f" @ {fmt(row.get('rate'), cmeta.get_field('rate'), parent)}"
        if cmeta.get_field("amount") and row.get("amount") is not None:
            part += f" = {fmt(row.get('amount'), cmeta.get_field('amount'), parent)}"
        return part
    cols = [df for df in cmeta.fields if df.in_list_view and df.fieldtype not in SKIP_TYPES][:4]
    return " · ".join(fmt(row.get(df.fieldname), df, parent) or "-" for df in cols) or row.name


def help_text(erp_user, user_full_name: str, prefix: str | None) -> str:
    p = f"{prefix} " if prefix else ""
    allowed = []
    if erp_user.can_submit:
        allowed.append("submit")
    if erp_user.can_cancel:
        allowed.append("cancel")
    if erp_user.can_amend:
        allowed.append("amend")
    if erp_user.can_update:
        allowed.append("update")
    lines = [
        _("🤖 *ERP Assistant* — signed in as {0}").format(user_full_name),
        "",
        _("Ask in your own words, for example:"),
        f"• {p}sales orders of Harshit Dixit on 29-10-2026",
        f"• {p}open work orders for item FAB-001",
        f"• {p}show SAL-ORD-2026-00012",
        f"• {p}send the pdf of it",
        "",
        _("Or use exact commands:"),
        f"• {p}find sales order customer: Harshit Dixit, date: 29-10-2026",
        f"• {p}show 2   (a number from the last list)",
        f"• {p}submit / cancel / amend / pdf  <ID or number>",
        f"• {p}set <ID> delivery date = 05-11-2026",
        "",
        _("You may: view, pdf{0}.").format(", " + ", ".join(allowed) if allowed else ""),
        _("Every change asks you to confirm first."),
    ]
    return "\n".join(lines)
