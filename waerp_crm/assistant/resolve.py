"""Turn human words into ERPNext things: DocTypes, fields, filter values, document references.

Everything runs as the staff member's own ERPNext user, so `frappe.get_list` and
permission checks apply that person's roles.
"""

from __future__ import annotations

import difflib
import re

import frappe
from frappe import _

from waerp_crm.assistant.parse import parse_date


class AssistantError(Exception):
    """A message for the staff member (not a crash)."""


ALIASES = {
    "so": "Sales Order", "order": "Sales Order", "orders": "Sales Order",
    "po": "Purchase Order", "wo": "Work Order", "bom": "BOM", "boms": "BOM", "bill of material": "BOM",
    "bill of materials": "BOM", "si": "Sales Invoice", "invoice": "Sales Invoice", "invoices": "Sales Invoice",
    "bill": "Sales Invoice", "pi": "Purchase Invoice", "purchase bill": "Purchase Invoice",
    "dn": "Delivery Note", "delivery": "Delivery Note", "challan": "Delivery Note",
    "pr": "Purchase Receipt", "grn": "Purchase Receipt", "mr": "Material Request", "indent": "Material Request",
    "se": "Stock Entry", "qtn": "Quotation", "quote": "Quotation", "quotes": "Quotation",
    "pe": "Payment Entry", "payment": "Payment Entry", "payments": "Payment Entry",
    "je": "Journal Entry", "jv": "Journal Entry", "rfq": "Request for Quotation",
    "sq": "Supplier Quotation", "jc": "Job Card", "customer": "Customer", "customers": "Customer",
    "supplier": "Supplier", "suppliers": "Supplier", "item": "Item", "items": "Item",
}

# Searched when someone gives only a document ID (custom DocTypes come from the user's allowed list).
COMMON_DOCTYPES = [
    "Sales Order", "Sales Invoice", "Delivery Note", "Quotation", "Purchase Order", "Purchase Invoice",
    "Purchase Receipt", "Material Request", "Work Order", "BOM", "Job Card", "Stock Entry",
    "Payment Entry", "Journal Entry", "Supplier Quotation", "Request for Quotation",
]

DATE_FIELDS = ["transaction_date", "posting_date", "schedule_date", "planned_start_date", "bill_date",
               "date", "from_date", "start_date", "expected_delivery_date", "delivery_date"]
PARTY_KEYS = {
    "customer": ["customer", "party_name", "customer_name", "party"],
    "client": ["customer", "party_name", "customer_name", "party"],
    "supplier": ["supplier", "supplier_name", "party"],
    "vendor": ["supplier", "supplier_name", "party"],
    "party": ["party", "party_name", "customer", "supplier"],
}
AMOUNT_FIELDS = ["grand_total", "rounded_total", "total", "paid_amount", "total_amount", "total_cost"]
ITEM_FIELDS = ["item_code", "production_item", "item"]
DOCSTATUS_WORDS = {"draft": 0, "submitted": 1, "cancelled": 2, "canceled": 2}
RANGE_KEYS = {"from": ">=", "since": ">=", "after": ">", "to": "<=", "till": "<=", "until": "<=", "upto": "<=",
              "before": "<"}
UPDATABLE_TYPES = {"Data", "Small Text", "Text", "Long Text", "Text Editor", "Date", "Datetime", "Int", "Float",
                   "Currency", "Percent", "Select", "Link", "Check", "Phone"}


# ------------------------------------------------------------------ doctypes

def _all_doctypes() -> list[str]:
    key = "waerp_crm:assistant:doctypes"
    names = frappe.cache.get_value(key)
    if names is None:
        names = frappe.get_all("DocType", filters={"istable": 0, "issingle": 0}, pluck="name")
        frappe.cache.set_value(key, names, expires_in_sec=600)
    return names


def resolve_doctype(text: str | None) -> str | None:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not t:
        return None
    candidates = [t, t.rstrip("s"), t[:-2] if t.endswith("es") else t]
    for c in candidates:
        if c in ALIASES:
            return ALIASES[c]
    by_lower = {n.lower(): n for n in _all_doctypes()}
    for c in candidates:
        if c in by_lower:
            return by_lower[c]
    close = difflib.get_close_matches(t, list(by_lower), n=1, cutoff=0.85)
    return by_lower[close[0]] if close else None


def split_doctype_prefix(text: str) -> tuple[str | None, str]:
    """'sales order SAL-ORD-1' -> ('Sales Order', 'SAL-ORD-1')"""
    words = (text or "").split()
    for n in range(min(5, len(words)), 0, -1):
        dt = resolve_doctype(" ".join(words[:n]))
        if dt:
            return dt, " ".join(words[n:])
    return None, text or ""


def check_doctype_allowed(doctype: str, erp_user) -> None:
    allowed = [x.strip().lower() for x in (erp_user.allowed_doctypes or "").splitlines() if x.strip()]
    if allowed and doctype.lower() not in allowed:
        raise AssistantError(_("You are not set up to use {0} from WhatsApp.").format(doctype))
    if not frappe.has_permission(doctype, "read"):
        raise AssistantError(_("Your ERPNext user has no permission to read {0}.").format(doctype))


# ------------------------------------------------------------------ fields

def first_field(meta, names: list[str]):
    for n in names:
        df = meta.get_field(n)
        if df:
            return df
    return None


def date_field(meta) -> str:
    df = first_field(meta, DATE_FIELDS)
    return df.fieldname if df else "creation"


def party_field(meta):
    return first_field(meta, ["customer", "supplier", "party_name", "party", "customer_name", "supplier_name"])


def amount_field(meta):
    return first_field(meta, AMOUNT_FIELDS)


def find_field(meta, key: str):
    """A field by fieldname or label (case-insensitive)."""
    k = (key or "").strip().lower()
    fname = re.sub(r"[^a-z0-9]+", "_", k).strip("_")
    df = meta.get_field(fname)
    if df:
        return df
    for f in meta.fields:
        if (f.label or "").strip().lower() == k:
            return f
    labels = {(f.label or "").strip().lower(): f for f in meta.fields if f.label}
    close = difflib.get_close_matches(k, list(labels), n=1, cutoff=0.85)
    return labels[close[0]] if close else None


def items_table(meta):
    """(child_doctype, fieldname) of the main item table, if any."""
    tables = meta.get_table_fields()
    for t in sorted(tables, key=lambda t: t.fieldname != "items"):
        cmeta = frappe.get_meta(t.options)
        if cmeta.get_field("item_code"):
            return t.options, t.fieldname
    return None, None


# ------------------------------------------------------------------ values

def resolve_link(target: str, value: str, limit: int = 20) -> list[str]:
    value = (value or "").strip()
    if not value:
        return []
    exact = frappe.db.get_value(target, value, "name") if frappe.db.exists(target, value) else None
    if exact:
        return [exact]
    meta = frappe.get_meta(target)
    title = meta.get_title_field()
    fields = {"name"}
    if title and meta.get_field(title):
        fields.add(title)
    for f in meta.get_search_fields() or []:
        df = meta.get_field(f)
        if df and df.fieldtype in ("Data", "Small Text", "Link", "Select", "Phone"):
            fields.add(f)
    for extra in ("customer_name", "supplier_name", "item_name", "employee_name", "lead_name", "title"):
        if meta.get_field(extra):
            fields.add(extra)
    rows = frappe.get_list(target, or_filters=[[f, "like", f"%{value}%"] for f in fields],
                           fields=["name"] + ([title] if title and title != "name" and meta.get_field(title) else []),
                           limit=limit + 1)
    for r in rows:  # an exact title match wins over partial matches
        if str(r.get(title) or "").strip().lower() == value.lower() or r.name.lower() == value.lower():
            return [r.name]
    return [r.name for r in rows][:limit]


def _match_option(df, value: str) -> str | None:
    opts = [o for o in (df.options or "").split("\n") if o.strip()]
    for o in opts:
        if o.lower() == value.strip().lower():
            return o
    close = difflib.get_close_matches(value.strip().lower(), [o.lower() for o in opts], n=1, cutoff=0.75)
    if close:
        return next(o for o in opts if o.lower() == close[0])
    return None


def _date_or_error(value: str) -> str:
    d = parse_date(value, frappe.utils.getdate())
    if not d:
        raise AssistantError(_("I couldn't read the date '{0}'. Use DD-MM-YYYY, e.g. 29-10-2026.").format(value))
    return d


def build_filters(doctype: str, pairs: list[tuple[str, str]]) -> tuple[list, list[str]]:
    """-> (frappe filters [[dt, field, op, value]], human-readable descriptions)"""
    meta = frappe.get_meta(doctype)
    filters, described = [], []
    for key, value in pairs:
        k = key.strip().lower()
        v = (value or "").strip()
        if not v:
            continue
        if k in RANGE_KEYS:
            dfield = date_field(meta)
            filters.append([doctype, dfield, RANGE_KEYS[k], _date_or_error(v)])
            described.append(f"{k} {v}")
            continue
        if k in ("date", "on", "dated", "day"):
            dfield = date_field(meta)
            d = _date_or_error(v)
            df = meta.get_field(dfield)
            if dfield == "creation" or (df and df.fieldtype == "Datetime"):
                filters.append([doctype, dfield, "between", [d, d]])
            else:
                filters.append([doctype, dfield, "=", d])
            described.append(f"date {v}")
            continue
        if k in ("id", "name", "number", "no", "doc", "document"):
            filters.append([doctype, "name", "like", f"%{v}%"])
            described.append(f"ID {v}")
            continue
        if k in ("status", "state") and v.lower() in DOCSTATUS_WORDS:
            sdf = meta.get_field("status")
            opt = _match_option(sdf, v) if sdf and sdf.fieldtype == "Select" else None
            if opt:
                filters.append([doctype, "status", "=", opt])
            else:
                filters.append([doctype, "docstatus", "=", DOCSTATUS_WORDS[v.lower()]])
            described.append(f"status {v}")
            continue
        if k in ("item", "product", "item code") and not meta.get_field("item"):
            df = first_field(meta, ITEM_FIELDS)
            names = resolve_link("Item", v)
            if not names:
                raise AssistantError(_("No Item matches '{0}'.").format(v))
            if df:
                filters.append([doctype, df.fieldname, "in", names])
            else:
                child, _f = items_table(meta)
                if not child:
                    raise AssistantError(_("{0} has no item field to filter on.").format(doctype))
                filters.append([child, "item_code", "in", names])
            described.append(f"item {v}")
            continue
        df = first_field(meta, PARTY_KEYS[k]) if k in PARTY_KEYS else None
        if k in ("amount", "total", "value", "grand total"):
            df = amount_field(meta)
        df = df or find_field(meta, k)
        if not df:
            raise AssistantError(_("{0} has no field called '{1}'.").format(doctype, key))
        filters.append([doctype, df.fieldname, *_filter_value(df, v)])
        described.append(f"{(df.label or df.fieldname).lower()} {v}")
    return filters, described


def _filter_value(df, v: str) -> tuple[str, object]:
    ft = df.fieldtype
    if ft == "Link":
        names = resolve_link(df.options, v)
        if not names:
            raise AssistantError(_("No {0} matches '{1}'.").format(df.options, v))
        return ("=", names[0]) if len(names) == 1 else ("in", names)
    if ft in ("Date",):
        return "=", _date_or_error(v)
    if ft == "Datetime":
        d = _date_or_error(v)
        return "between", [d, d]
    if ft == "Select":
        opt = _match_option(df, v)
        return ("=", opt) if opt else ("like", f"%{v}%")
    if ft == "Check":
        return "=", 1 if v.lower() in ("1", "yes", "true", "y", "checked") else 0
    if ft in ("Int", "Float", "Currency", "Percent"):
        return "=", frappe.utils.flt(re.sub(r"[^\d.\-]", "", v))
    return "like", f"%{v}%"


def update_value(meta, df, raw: str):
    """Typed value for doc.set(), or AssistantError."""
    ft = df.fieldtype
    v = (raw or "").strip()
    if ft == "Link":
        if not v:
            return None
        names = resolve_link(df.options, v)
        if len(names) != 1:
            raise AssistantError(
                _("'{0}' matches {1} {2} records; please give the exact one.").format(v, len(names), df.options)
                if names else _("No {0} matches '{1}'.").format(df.options, v))
        return names[0]
    if ft == "Date":
        return _date_or_error(v)
    if ft == "Datetime":
        return _date_or_error(v) + " 00:00:00"
    if ft == "Select":
        opt = _match_option(df, v)
        if opt is None:
            raise AssistantError(_("'{0}' is not an option for {1}. Options: {2}").format(
                v, df.label, ", ".join(o for o in (df.options or "").split("\n") if o)))
        return opt
    if ft == "Check":
        return 1 if v.lower() in ("1", "yes", "true", "y", "checked", "on") else 0
    if ft == "Int":
        return frappe.utils.cint(re.sub(r"[^\d\-]", "", v))
    if ft in ("Float", "Currency", "Percent"):
        return frappe.utils.flt(re.sub(r"[^\d.\-]", "", v))
    return v


def updatable_field(doc, key: str):
    meta = frappe.get_meta(doc.doctype)
    df = find_field(meta, key)
    if not df:
        raise AssistantError(_("{0} has no field called '{1}'.").format(doc.doctype, key))
    if df.fieldtype not in UPDATABLE_TYPES or df.read_only or df.hidden or df.fetch_from:
        raise AssistantError(_("'{0}' can't be changed from WhatsApp.").format(df.label or df.fieldname))
    if doc.docstatus == 2:
        raise AssistantError(_("{0} is cancelled; amend it first.").format(doc.name))
    if doc.docstatus == 1 and not df.allow_on_submit:
        raise AssistantError(_("{0} is submitted, and '{1}' can't be changed after submit.").format(
            doc.name, df.label or df.fieldname))
    if not doc.has_permission("write"):
        raise AssistantError(_("You don't have permission to edit {0}.").format(doc.name))
    return meta, df


# ------------------------------------------------------------------ references

PRONOUN_REFS = {"", "it", "this", "that", "current", "last", "same"}


def resolve_ref(ref: str | None, session: dict, erp_user, doctype_hint: str | None = None) -> tuple[str, str]:
    """-> (doctype, name). ref: None/'it' (current), '2' (list number), an ID, or '<doctype> <ID>'."""
    r = (ref or "").strip().lstrip("#")
    if r.lower() in PRONOUN_REFS:
        cur = session.get("current")
        if not cur:
            raise AssistantError(_("Which document? Search first, or give its ID, e.g. 'show SAL-ORD-2026-00012'."))
        return tuple(cur)
    results = session.get("results") or []
    if r.isdigit() and int(r) <= 50:
        i = int(r)
        if 1 <= i <= len(results):
            return tuple(results[i - 1])
        raise AssistantError(_("There's no number {0} in the last list.").format(i))
    dt, rest = split_doctype_prefix(r)
    if dt and rest:
        doctype_hint, r = dt, rest.strip()
    for d, n in results + ([session["current"]] if session.get("current") else []):
        if n.lower() == r.lower() and (not doctype_hint or d == doctype_hint):
            return d, n
    candidates = [doctype_hint] if doctype_hint else []
    allowed = [x.strip() for x in (erp_user.allowed_doctypes or "").splitlines() if x.strip()]
    candidates += [d for d in allowed + COMMON_DOCTYPES if d not in candidates]
    for d in candidates:
        if not frappe.db.exists("DocType", d):
            continue
        name = frappe.db.get_value(d, r, "name")
        if name:
            return d, name
    raise AssistantError(_("I couldn't find a document '{0}'. Try '<doctype> <ID>', e.g. 'show Work Order {0}'.")
                         .format(r))
