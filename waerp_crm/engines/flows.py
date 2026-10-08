"""Engine 3 — Flow engine: a per-contact chatbot state machine.

Node kinds
  auto-advancing : start, send_message, send_media, condition, set_tag, update_contact,
                   create_lead, erp_lookup        -> run and move on immediately
  suspending     : send_buttons, send_list, collect_input
                                                  -> send prompt, save current node, WAIT
  terminal       : handoff, end                   -> close the run

Edges live inside each node's JSON config as `next_node_key` (stable string ids,
so flows can be cloned / exported without rewriting references).

Invariants
  * at most ONE Active run per contact
  * a reply only ever advances the run's *current* suspended node
  * unmatched replies -> fallback policy (reprompt N times, then handoff / end / ignore)
  * auto-advance is capped (MAX_STEPS) so a bad graph can't spin forever
  * every transition is logged to the run's events table for debugging

Node config cheat-sheet (JSON in WA Flow Node.config)
  start          {"next_node_key": "menu"}
  send_message   {"text": "Hi {{contact.first_name}}!", "next_node_key": "menu"}
  send_media     {"media_url": "/files/brochure.pdf", "caption": "...", "next_node_key": "x"}
  send_buttons   {"text": "...", "buttons": [{"reply_id": "sales", "title": "Sales", "next_node_key": "s"}]}
  send_list      {"text": "...", "button_label": "Menu", "sections": [{"title": "..", "rows": [
                     {"reply_id": "r1", "title": "..", "description": "..", "next_node_key": ".."}]}]}
  collect_input  {"prompt_text": "Your order no?", "var_key": "order_no", "validation": "any|number|email|phone|regex",
                  "pattern": "^SO-", "invalid_text": "Please send a valid number", "save_to_contact": false,
                  "next_node_key": "lookup"}
  condition      {"subject": "var|tag|contact_field|customer_linked|time", "key": "order_no",
                  "operator": "equals|not_equals|contains|starts_with|exists|not_exists|gt|lt",
                  "value": "x", "true_node_key": "a", "false_node_key": "b"}
  set_tag        {"tag": "Hot Lead", "remove": false, "next_node_key": "x"}
  update_contact {"field": "full_name|<attribute>", "value": "{{vars.name}}", "next_node_key": "x"}
  create_lead    {"next_node_key": "x"}
  erp_lookup     {"doctype": "Sales Order", "match_field": "name", "value": "{{vars.order_no}}",
                  "restrict_to_customer": true, "customer_field": "customer",
                  "fields": ["status", "grand_total", "delivery_date"],
                  "found_text": "Order {{doc.name}} is {{doc.status}}", "not_found_text": "Not found",
                  "next_node_key": "menu", "not_found_node_key": "menu"}
  handoff        {"note": "Wants a quote", "assign_to": "user@x.com", "text": "Connecting you to our team"}
  end            {"text": "Thank you!"}
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from waerp_crm.core import logic
from waerp_crm.utils import add_note, add_tag, assign_conversation, contact_namespace, ensure_lead, has_tag, remove_tag

MAX_STEPS = 50
ERP_LOOKUP_ALLOWED = {"Sales Order", "Sales Invoice", "Delivery Note", "Quotation", "Purchase Order",
                      "Item", "Payment Entry", "Issue", "Project", "Task", "Serial No", "Warranty Claim"}


# ------------------------------------------------------------------ loading


def _nodes(flow) -> dict[str, dict]:
    out = {}
    for n in flow.nodes:
        out[n.node_key] = {"node_key": n.node_key, "node_type": n.node_type,
                           "config": logic.parse_json(n.config, {}) or {}}
    return out


def _active_run(wa_contact: str):
    name = frappe.db.get_value("WA Flow Run", {"wa_contact": wa_contact, "status": "Active"}, "name",
                               order_by="creation desc")
    if not name:
        return None
    run = frappe.get_doc("WA Flow Run", name)
    timeout = frappe.db.get_value("WA Flow", run.flow, "run_timeout_hours") or 24
    if run.last_activity_at and get_datetime(run.last_activity_at) < add_to_date(now_datetime(), hours=-timeout):
        _end(run, "Expired", "timeout")
        return None
    return run


def _log(run, event: str, node_key: str | None = None, payload: dict | None = None):
    run.append("events", {"event": event, "node_key": node_key, "at": now_datetime(),
                          "payload": json.dumps(payload, default=str)[:1000] if payload else None})


def _save(run):
    run.last_activity_at = now_datetime()
    run.save(ignore_permissions=True)


def _end(run, status: str, reason: str | None = None):
    run.status = status
    run.ended_reason = reason
    run.current_node_key = None
    _log(run, "run_ended", payload={"status": status, "reason": reason})
    _save(run)


def _ns(run) -> dict:
    vars_ = logic.parse_json(run.vars, {}) or {}
    return {"vars": vars_, "contact": contact_namespace(run.wa_contact)}


# ------------------------------------------------------------------ public entry points


def dispatch_inbound_to_flows(ctx) -> dict:
    run = _active_run(ctx.wa_contact)
    if run:
        flow = frappe.get_doc("WA Flow", run.flow)
        return _handle_reply(flow, run, ctx)
    flow = _find_entry_flow(ctx)
    if not flow:
        return {"consumed": False, "outcome": "no_match"}
    return start_run(flow.name, ctx.wa_contact, ctx.conversation)


def start_run(flow_name: str, wa_contact: str, conversation: str, initial_vars: dict | None = None) -> dict:
    """Also used by automations ('start_flow' step) and the API for Manual flows."""
    flow = frappe.get_doc("WA Flow", flow_name)
    existing = _active_run(wa_contact)
    if existing:
        _end(existing, "Completed", "superseded")
    run = frappe.get_doc({
        "doctype": "WA Flow Run", "flow": flow.name, "wa_contact": wa_contact, "conversation": conversation,
        "status": "Active", "current_node_key": flow.entry_node_key, "vars": json.dumps(initial_vars or {}),
        "reprompt_count": 0, "last_activity_at": now_datetime(),
    })
    _log(run, "run_started", flow.entry_node_key)
    run.insert(ignore_permissions=True)
    frappe.db.sql("update `tabWA Flow` set execution_count = execution_count + 1 where name=%s", flow.name)
    outcome = _advance(flow, run, flow.entry_node_key)
    return {"consumed": True, "run": run.name, "outcome": outcome}


def _find_entry_flow(ctx):
    flows = frappe.get_all(
        "WA Flow", filters={"status": "Active"},
        or_filters=[["wa_account", "=", ctx.account], ["wa_account", "is", "not set"]],
        fields=["name", "trigger_type", "keywords", "match_type", "case_sensitive"], order_by="creation asc",
    )
    candidates = [t for t in (ctx.text, ctx.interactive_reply_id) if t]
    for f in flows:
        if f.trigger_type == "Keyword":
            kws = logic.split_lines(f.keywords)
            if any(logic.matches_keyword(c, kws, f.match_type, bool(f.case_sensitive)) for c in candidates):
                return f
        elif f.trigger_type == "First Inbound Message" and ctx.is_first_inbound:
            return f
    # Lowest priority: a catch-all welcome/menu flow for any other message.
    for f in flows:
        if f.trigger_type == "Any Message (Fallback)" and _fallback_allowed(f.name, ctx):
            return f
    return None


def _fallback_allowed(flow_name: str, ctx) -> bool:
    """Start the catch-all flow only if (a) no keyword automation will answer this message
    and (b) the contact hasn't had this flow within its cooldown window."""
    from waerp_crm.engines.automations import trigger_matches

    for name in frappe.get_all(
        "WA Automation", filters={"is_active": 1, "trigger_type": "Keyword Match"},
        or_filters=[["wa_account", "=", ctx.account], ["wa_account", "is", "not set"]], pluck="name",
    ):
        if trigger_matches(frappe.get_cached_doc("WA Automation", name), "Keyword Match", ctx):
            return False
    # Never interrupt a chat a human owns: assigned, or recently handed off by a bot.
    if frappe.db.get_value("WA Conversation", ctx.conversation, "assigned_to"):
        return False
    hours = frappe.db.get_value("WA Flow", flow_name, "fallback_cooldown_hours")
    hours = 24 if hours is None else int(hours)
    window = add_to_date(now_datetime(), hours=-(hours or 24))
    if frappe.db.exists("WA Flow Run", {"wa_contact": ctx.wa_contact, "status": "Handed Off",
                                        "modified": (">", window)}):
        return False
    if hours <= 0:
        return True
    recent = frappe.db.exists("WA Flow Run", {
        "flow": flow_name, "wa_contact": ctx.wa_contact,
        "creation": (">", add_to_date(now_datetime(), hours=-hours)),
    })
    return not recent


def _exact_keyword_flow(ctx) -> str | None:
    text = (ctx.text or "").strip().lower()
    for f in frappe.get_all("WA Flow", filters={"status": "Active", "trigger_type": "Keyword"},
                            or_filters=[["wa_account", "=", ctx.account], ["wa_account", "is", "not set"]],
                            fields=["name", "keywords"], order_by="creation asc"):
        if text in {k.lower() for k in logic.split_lines(f.keywords)}:
            return f.name
    return None


# ------------------------------------------------------------------ reply handling


def _handle_reply(flow, run, ctx) -> dict:
    nodes = _nodes(flow)
    _log(run, "reply_received", run.current_node_key,
         {"reply_id": ctx.interactive_reply_id, "text_len": len(ctx.text or "")})
    node = nodes.get(run.current_node_key or "")
    if not node:
        _end(run, "Failed", "current_node_missing")
        return {"consumed": False, "outcome": "failed"}

    cfg, ntype = node["config"], node["node_type"]
    next_key = None
    invalid_text = None
    if ntype in ("send_buttons", "send_list"):
        next_key = logic.match_reply(ntype, cfg, ctx.interactive_reply_id, ctx.text)

    # Customer typed a flow keyword exactly (e.g. "MENU") -> (re)start that flow.
    if not next_key and ctx.text:
        restart = _exact_keyword_flow(ctx)
        if restart:
            _save(run)
            return start_run(restart, run.wa_contact, run.conversation)

    if ntype == "collect_input":
        value = (ctx.text or "").strip()
        if value and logic.validate_input(value, cfg.get("validation"), cfg.get("pattern")):
            vars_ = logic.parse_json(run.vars, {}) or {}
            vars_[cfg["var_key"]] = value
            run.vars = json.dumps(vars_)
            if cfg.get("save_to_contact"):
                _set_contact_attr(run.wa_contact, cfg["var_key"], value)
            _log(run, "input_captured", node["node_key"], {"var": cfg["var_key"]})
            next_key = cfg.get("next_node_key")
        else:
            invalid_text = cfg.get("invalid_text")

    if next_key:
        run.reprompt_count = 0
        outcome = _advance(flow, run, next_key)
        return {"consumed": True, "run": run.name, "outcome": outcome}

    # ---- fallback policy
    run.reprompt_count = (run.reprompt_count or 0) + 1
    action = logic.decide_fallback(flow.fallback_action, run.reprompt_count, flow.max_reprompts)
    _log(run, "fallback", node["node_key"], {"action": action, "reprompts": run.reprompt_count})
    if action == "ignore":
        _save(run)
        return {"consumed": False, "outcome": "ignored"}
    if action == "reprompt":
        _save(run)
        from waerp_crm.whatsapp.outbound import send_text
        if invalid_text or flow.fallback_message:
            send_text(run.conversation, logic.interpolate(invalid_text or flow.fallback_message, _ns(run)),
                      source="Flow", raise_on_error=False)
        _send_prompt(run, node)
        return {"consumed": True, "outcome": "reprompted"}
    if action == "handoff":
        _handoff(run, {"note": f"Customer replies didn't match '{node['node_key']}' in flow {flow.title}."})
        return {"consumed": True, "outcome": "handed_off"}
    _end(run, "Completed", "fallback_end")
    return {"consumed": True, "outcome": "ended"}


# ------------------------------------------------------------------ the runner


def _advance(flow, run, key: str) -> str:
    nodes = _nodes(flow)
    for _step in range(MAX_STEPS):
        node = nodes.get(key or "")
        if not node:
            _end(run, "Failed", f"node_not_found:{key}")
            return "failed"
        ntype, cfg = node["node_type"], node["config"]
        run.current_node_key = key
        _log(run, "node_entered", key, {"type": ntype})
        try:
            if ntype in logic.SUSPENDING:
                _send_prompt(run, node)
                _save(run)
                return "suspended"
            if ntype == "handoff":
                _handoff(run, cfg)
                return "handed_off"
            if ntype == "end":
                if cfg.get("text"):
                    _text(run, cfg["text"])
                _end(run, "Completed", "end_node")
                return "completed"
            key = _run_auto_node(run, ntype, cfg)
        except Exception as e:
            frappe.log_error(title=f"WAERP CRM: flow node {key} failed", reference_doctype="WA Flow Run",
                             reference_name=run.name)
            _end(run, "Failed", f"{ntype}: {str(e)[:120]}")
            return "failed"
    _end(run, "Failed", "max_steps_exceeded")
    return "failed"


def _text(run, text: str):
    from waerp_crm.whatsapp.outbound import send_text
    send_text(run.conversation, logic.interpolate(text, _ns(run)), source="Flow")


def _run_auto_node(run, ntype: str, cfg: dict) -> str | None:
    ns = _ns(run)
    if ntype == "start":
        return cfg.get("next_node_key")
    if ntype == "send_message":
        _text(run, cfg.get("text") or "")
        return cfg.get("next_node_key")
    if ntype == "send_media":
        from waerp_crm.whatsapp.outbound import send_media
        send_media(run.conversation, file_url=cfg.get("media_url"), caption=logic.interpolate(cfg.get("caption"), ns),
                   filename=cfg.get("filename"), source="Flow")
        return cfg.get("next_node_key")
    if ntype == "condition":
        return cfg.get("true_node_key") if _condition(run, cfg, ns) else cfg.get("false_node_key")
    if ntype == "set_tag":
        tag = logic.interpolate(cfg.get("tag"), ns)
        if cfg.get("remove"):
            remove_tag(run.wa_contact, tag)
        elif add_tag(run.wa_contact, tag):
            from waerp_crm.engines.automations import fire_tag_added
            fire_tag_added(run.wa_contact, tag, run.conversation)
        return cfg.get("next_node_key")
    if ntype == "update_contact":
        _set_contact_attr(run.wa_contact, cfg.get("field"), logic.interpolate(cfg.get("value"), ns))
        return cfg.get("next_node_key")
    if ntype == "create_lead":
        ensure_lead(run.wa_contact)
        return cfg.get("next_node_key")
    if ntype == "erp_lookup":
        return _erp_lookup(run, cfg, ns)
    raise ValueError(f"Unknown node type {ntype}")


def _condition(run, cfg: dict, ns: dict) -> bool:
    subject = cfg.get("subject", "var")
    key = cfg.get("key")
    if subject == "var":
        actual = ns["vars"].get(key)
    elif subject == "tag":
        return has_tag(run.wa_contact, key) == (cfg.get("operator", "exists") != "not_exists")
    elif subject == "contact_field":
        actual = ns["contact"].get(key)
    elif subject == "customer_linked":
        return bool(ns["contact"].get("customer"))
    elif subject == "time":
        return logic.in_time_window(now_datetime(), cfg.get("value") or "")
    else:
        return False
    return logic.compare(cfg.get("operator", "equals"), actual, logic.interpolate(str(cfg.get("value") or ""), ns))


def _set_contact_attr(wa_contact: str, field: str | None, value):
    if not field:
        return
    if field in ("full_name",):
        frappe.db.set_value("WA Contact", wa_contact, field, value)
        return
    attrs = logic.parse_json(frappe.db.get_value("WA Contact", wa_contact, "attributes"), {}) or {}
    attrs[field] = value
    frappe.db.set_value("WA Contact", wa_contact, "attributes", json.dumps(attrs))


def _erp_lookup(run, cfg: dict, ns: dict) -> str | None:
    """Customer self-service against live ERP data (e.g. 'where is my order?').

    Security: only allow-listed doctypes, and by default only documents that
    belong to the Customer linked to this WhatsApp number.
    """
    doctype = cfg.get("doctype")
    if doctype not in ERP_LOOKUP_ALLOWED:
        raise ValueError(f"erp_lookup not allowed for {doctype}")
    value = logic.interpolate(cfg.get("value"), ns).strip()
    filters = {cfg.get("match_field") or "name": value}
    if cfg.get("restrict_to_customer", True) and doctype not in ("Item",):
        customer = ns["contact"].get("customer")
        if not customer:
            filters = None
        else:
            filters[cfg.get("customer_field") or "customer"] = customer
    fields = list({"name", *(cfg.get("fields") or [])})
    row = frappe.get_all(doctype, filters=filters, fields=fields, limit=1) if (filters and value) else []
    if row:
        doc_ns = {k: (frappe.utils.fmt_money(v) if isinstance(v, float) else v) for k, v in row[0].items()}
        _text(run, logic.interpolate(cfg.get("found_text") or "{{doc.name}}: {{doc.status}}", {**ns, "doc": doc_ns}))
        return cfg.get("next_node_key")
    if cfg.get("not_found_text"):
        _text(run, cfg["not_found_text"])
    return cfg.get("not_found_node_key") or cfg.get("next_node_key")


def _send_prompt(run, node: dict):
    from waerp_crm.whatsapp.outbound import send_buttons, send_list, send_text
    cfg, ns = node["config"], _ns(run)
    if node["node_type"] == "send_buttons":
        send_buttons(run.conversation, cfg, text=logic.interpolate(cfg.get("text"), ns))
    elif node["node_type"] == "send_list":
        send_list(run.conversation, cfg, text=logic.interpolate(cfg.get("text"), ns))
    else:
        send_text(run.conversation, logic.interpolate(cfg.get("prompt_text"), ns), source="Flow")


def _handoff(run, cfg: dict):
    if cfg.get("text"):
        try:
            _text(run, cfg["text"])
        except Exception:
            pass
    assign_conversation(run.conversation, cfg.get("assign_to"), auto=not cfg.get("assign_to"))
    frappe.db.set_value("WA Conversation", run.conversation, {"status": "Open", "ai_autoreply_disabled": 1})
    vars_ = logic.parse_json(run.vars, {}) or {}
    note = cfg.get("note") or "Flow handed off to an agent."
    if vars_:
        note += "\nCaptured: " + ", ".join(f"{k}={v}" for k, v in vars_.items())
    add_note(run.conversation, note)
    _end(run, "Handed Off", "handoff")


# ------------------------------------------------------------------ maintenance


def expire_stale_runs():
    """Hourly: close runs nobody answered (timeout per flow)."""
    for r in frappe.get_all("WA Flow Run", filters={"status": "Active"}, fields=["name", "flow", "last_activity_at"],
                            limit=5000):
        timeout = frappe.db.get_value("WA Flow", r.flow, "run_timeout_hours") or 24
        if r.last_activity_at and get_datetime(r.last_activity_at) < add_to_date(now_datetime(), hours=-timeout):
            frappe.db.set_value("WA Flow Run", r.name, {"status": "Expired", "ended_reason": "timeout",
                                                        "current_node_key": None})
    frappe.db.commit()
