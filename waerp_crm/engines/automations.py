"""Engine 4 — Automations: trigger -> tree of steps, with conditions and waits.

Step tree: each WA Automation Step has a step_key; steps inside a condition
carry parent_step_key=<condition key> and branch=yes|no. Order = row order.

Execution uses an explicit FRAME STACK [{parent, branch, index}]:
  * condition  -> push a frame for the chosen branch
  * wait       -> persist the whole stack in WA Pending Execution (run_at) and stop
  * end of a branch -> pop and continue with the steps AFTER the condition
So a wait inside a branch resumes correctly and then finishes the outer steps.

Step config cheat-sheet (JSON):
  send_message         {"text": "Hi {{contact.first_name}}"}
  send_buttons         {"text": "...", "buttons": [{"reply_id": "yes", "title": "Yes"}]}
  send_list            {"text": "...", "button_label": "Menu", "sections": [...]}
  send_template        {"template": "<WA Template name>", "params": ["{{contact.full_name}}"], "header_file": "/files/x.pdf"}
  add_tag/remove_tag   {"tag": "VIP"}
  assign_conversation  {"user": "agent@x.com"}   (blank user => round-robin / load balanced)
  update_contact_field {"field": "city", "value": "{{message.text}}"}
  create_lead          {}
  create_opportunity   {"opportunity_type": "Sales", "amount": 0}
  send_webhook         {"url": "https://...", "headers": {"X-Key": "..."}}
  resolve_conversation {}
  start_flow           {"flow": "<WA Flow name>"}
  condition            {"subject": "tag|contact_field|message|time|customer_linked|var",
                        "key": "VIP", "operator": "exists|equals|contains|...", "value": "..."}
  wait                 {"amount": 2, "unit": "minutes|hours|days"}
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_to_date, now_datetime

from waerp_crm.core import logic
from waerp_crm.utils import (add_tag, assign_conversation, contact_namespace, ensure_lead, get_or_create_conversation,
                          has_tag, remove_tag, session_open)

MAX_TAG_CHAIN = 5
SEND_STEPS = {"send_message", "send_buttons", "send_list", "send_template"}


class StepError(Exception):
    pass


# ------------------------------------------------------------------ triggers


def trigger_matches(auto, trigger_type: str, ctx) -> bool:
    if auto.trigger_type == "Keyword Match":
        return logic.matches_keyword(ctx.get("text") or "", logic.split_lines(auto.keywords), auto.match_type,
                                     bool(auto.case_sensitive))
    if auto.trigger_type == "Interactive Reply":
        return bool(ctx.get("interactive_reply_id")) and ctx["interactive_reply_id"] in logic.split_lines(auto.reply_ids)
    if auto.trigger_type == "Tag Added":
        return bool(ctx.get("tag")) and ctx.get("tag") == auto.tag
    return True


def run_for_trigger(trigger_type: str, ctx) -> bool:
    """Run every active automation for this trigger. Returns True if any sent a message."""
    account = ctx.get("account")
    filters = {"is_active": 1, "trigger_type": trigger_type}
    autos = frappe.get_all("WA Automation", filters=filters,
                           or_filters=[["wa_account", "=", account], ["wa_account", "is", "not set"]] if account else None,
                           pluck="name", order_by="creation asc")
    sent_any = False
    for name in autos:
        auto = frappe.get_doc("WA Automation", name)
        if not trigger_matches(auto, trigger_type, ctx):
            continue
        try:
            sent_any = execute(auto, trigger_type, ctx) or sent_any
        except Exception:
            frappe.log_error(title=f"WAERP CRM: automation {auto.title} crashed")
    return sent_any


def fire_tag_added(wa_contact: str, tag: str, conversation: str | None = None, depth: int = 0):
    if depth >= MAX_TAG_CHAIN:
        return
    account = frappe.db.get_value("WA Conversation", conversation, "wa_account") if conversation else None
    run_for_trigger("Tag Added", frappe._dict(wa_contact=wa_contact, conversation=conversation, account=account,
                                              tag=tag, tag_depth=depth + 1))


def fire_conversation_event(trigger_type: str, conversation: str):
    c = frappe.db.get_value("WA Conversation", conversation, ["wa_account", "wa_contact", "assigned_to"], as_dict=True)
    run_for_trigger(trigger_type, frappe._dict(account=c.wa_account, wa_contact=c.wa_contact,
                                               conversation=conversation, agent=c.assigned_to))


# ------------------------------------------------------------------ execution


def _context_dict(ctx) -> dict:
    keep = ("account", "conversation", "wa_contact", "text", "interactive_reply_id", "tag", "tag_depth", "agent")
    return {k: ctx.get(k) for k in keep if ctx.get(k) is not None}


def execute(auto, trigger_type: str, ctx) -> bool:
    log = frappe.get_doc({"doctype": "WA Automation Log", "automation": auto.name, "trigger_type": trigger_type,
                          "status": "Running", "wa_contact": ctx.get("wa_contact"),
                          "conversation": ctx.get("conversation"), "results": "[]"}).insert(ignore_permissions=True)
    frappe.db.sql("""update `tabWA Automation` set execution_count = execution_count + 1, last_run_at=%s
                     where name=%s""", (now_datetime(), auto.name))
    frames = [{"parent": None, "branch": None, "index": 0}]
    return _run_frames(auto, frames, _context_dict(ctx), log)


def _steps(auto) -> list[dict]:
    return [{"step_key": s.step_key, "parent_step_key": s.parent_step_key, "branch": s.branch,
             "step_type": s.step_type, "config": logic.parse_json(s.config, {}) or {}, "idx": s.idx}
            for s in auto.steps]


def _run_frames(auto, frames: list[dict], ctx: dict, log) -> bool:
    steps = _steps(auto)
    results = logic.parse_json(log.results, []) or []
    sent = False
    status, error = "Success", None
    guard = 0
    while frames:
        guard += 1
        if guard > 500:
            status, error = "Failed", "step limit exceeded"
            break
        frame = frames[-1]
        scope = logic.scope_steps(steps, frame["parent"], frame["branch"])
        if frame["index"] >= len(scope):
            frames.pop()
            continue
        step = scope[frame["index"]]
        frame["index"] += 1
        stype, cfg = step["step_type"], step["config"]

        if stype == "wait":
            secs = logic.wait_seconds(cfg.get("amount", 1), cfg.get("unit", "minutes"))
            frappe.get_doc({"doctype": "WA Pending Execution", "automation": auto.name, "log": log.name,
                            "wa_contact": ctx.get("wa_contact"), "status": "Pending",
                            "run_at": add_to_date(now_datetime(), seconds=secs),
                            "frames": json.dumps(frames), "context": json.dumps(ctx, default=str)}
                           ).insert(ignore_permissions=True)
            results.append({"step": step["step_key"], "type": "wait", "ok": True, "detail": f"waiting {secs}s"})
            log.db_set({"status": "Waiting", "results": json.dumps(results)})
            return sent

        try:
            if stype == "condition":
                taken = evaluate_condition(cfg, ctx)
                results.append({"step": step["step_key"], "type": stype, "ok": True,
                                "detail": f"branch={'yes' if taken else 'no'}"})
                frames.append({"parent": step["step_key"], "branch": "yes" if taken else "no", "index": 0})
                continue
            detail = run_step(stype, cfg, ctx)
            sent = sent or stype in SEND_STEPS
            results.append({"step": step["step_key"], "type": stype, "ok": True, "detail": detail})
        except Exception as e:
            results.append({"step": step["step_key"], "type": stype, "ok": False, "detail": str(e)[:500]})
            status, error = "Failed", str(e)[:1000]
            if not isinstance(e, (StepError, frappe.ValidationError)):
                frappe.log_error(title=f"WAERP CRM: automation step {step['step_key']} failed")
            break
    log.db_set({"status": status, "results": json.dumps(results), "error": error})
    return sent


def process_pending():
    """Scheduler, every minute: resume waits that are due. Claim = atomic status flip."""
    due = frappe.get_all("WA Pending Execution", filters={"status": "Pending", "run_at": ("<=", now_datetime())},
                         pluck="name", order_by="run_at asc", limit=200)
    for name in due:
        frappe.db.sql("update `tabWA Pending Execution` set status='Running' where name=%s and status='Pending'", name)
        if not frappe.db._cursor.rowcount:
            continue  # another worker claimed it
        frappe.db.commit()
        pe = frappe.get_doc("WA Pending Execution", name)
        try:
            auto = frappe.get_doc("WA Automation", pe.automation)
            if not auto.is_active:
                pe.db_set("status", "Done")
                continue
            log = frappe.get_doc("WA Automation Log", pe.log)
            _run_frames(auto, logic.parse_json(pe.frames, []), logic.parse_json(pe.context, {}), log)
            pe.db_set("status", "Done")
        except Exception:
            frappe.db.rollback()
            frappe.db.set_value("WA Pending Execution", name, "status", "Failed")
            frappe.log_error(title="WAERP CRM: resume automation failed", reference_doctype="WA Pending Execution",
                             reference_name=name)
        frappe.db.commit()


# ------------------------------------------------------------------ conditions


def evaluate_condition(cfg: dict, ctx: dict) -> bool:
    subject = cfg.get("subject")
    key = cfg.get("key")
    op = cfg.get("operator") or "equals"
    wa_contact = ctx.get("wa_contact")
    if subject == "tag":
        present = bool(wa_contact) and has_tag(wa_contact, key)
        return present if op != "not_exists" else not present
    if subject == "contact_field":
        return logic.compare(op, contact_namespace(wa_contact).get(key) if wa_contact else None, cfg.get("value"))
    if subject == "message":
        return logic.compare(op if op != "equals" else "contains", ctx.get("text"), cfg.get("value"))
    if subject == "time":
        return logic.in_time_window(now_datetime(), cfg.get("value") or key or "")
    if subject == "customer_linked":
        return bool(wa_contact and frappe.db.get_value("WA Contact", wa_contact, "customer"))
    if subject == "session_open":
        return bool(ctx.get("conversation")) and session_open(ctx["conversation"])
    return False


# ------------------------------------------------------------------ steps


def _ns(ctx: dict) -> dict:
    return {"contact": contact_namespace(ctx["wa_contact"]) if ctx.get("wa_contact") else {},
            "message": {"text": ctx.get("text") or ""}, "vars": ctx.get("vars") or {}}


def _conversation(ctx: dict) -> str:
    if ctx.get("conversation"):
        return ctx["conversation"]
    if not ctx.get("wa_contact"):
        raise StepError("No contact in context")
    from waerp_crm.utils import get_account
    conv, _ = get_or_create_conversation(ctx.get("account") or get_account().name, ctx["wa_contact"])
    ctx["conversation"] = conv
    return conv


def run_step(stype: str, cfg: dict, ctx: dict) -> str:
    from waerp_crm.whatsapp import outbound

    ns = _ns(ctx)
    if stype == "send_message":
        outbound.send_text(_conversation(ctx), logic.interpolate(cfg.get("text"), ns), source="Automation")
        return "sent"
    if stype == "send_buttons":
        outbound.send_buttons(_conversation(ctx), cfg, source="Automation", text=logic.interpolate(cfg.get("text"), ns))
        return "sent"
    if stype == "send_list":
        outbound.send_list(_conversation(ctx), cfg, source="Automation", text=logic.interpolate(cfg.get("text"), ns))
        return "sent"
    if stype == "send_template":
        params = [logic.interpolate(p, ns) for p in cfg.get("params") or []]
        outbound.send_template(_conversation(ctx), cfg["template"], params, header_file=cfg.get("header_file"),
                               source="Automation")
        return "sent"
    if stype in ("add_tag", "remove_tag"):
        tag = logic.interpolate(cfg.get("tag"), ns)
        if stype == "remove_tag":
            return "removed" if remove_tag(ctx["wa_contact"], tag) else "absent"
        if add_tag(ctx["wa_contact"], tag):
            fire_tag_added(ctx["wa_contact"], tag, ctx.get("conversation"), depth=ctx.get("tag_depth") or 0)
            return "added"
        return "already present"
    if stype == "assign_conversation":
        user = assign_conversation(_conversation(ctx), cfg.get("user"), auto=not cfg.get("user"))
        return f"assigned to {user or 'nobody'}"
    if stype == "update_contact_field":
        from waerp_crm.engines.flows import _set_contact_attr
        _set_contact_attr(ctx["wa_contact"], cfg.get("field"), logic.interpolate(cfg.get("value"), ns))
        return "updated"
    if stype == "create_lead":
        return f"lead {ensure_lead(ctx['wa_contact'])}"
    if stype == "create_opportunity":
        return f"opportunity {_create_opportunity(ctx, cfg)}"
    if stype == "send_webhook":
        from waerp_crm.engines.outbound_webhooks import safe_post
        resp = safe_post(cfg["url"], {"event": "automation", "context": ctx, "contact": ns["contact"]},
                         headers=cfg.get("headers") or {})
        if resp.status_code >= 400:
            raise StepError(f"webhook returned HTTP {resp.status_code}")
        return f"HTTP {resp.status_code}"
    if stype == "resolve_conversation":
        frappe.db.set_value("WA Conversation", _conversation(ctx), "status", "Resolved")
        return "resolved"
    if stype == "start_flow":
        from waerp_crm.engines.flows import start_run
        res = start_run(cfg["flow"], ctx["wa_contact"], _conversation(ctx))
        return f"flow {res.get('outcome')}"
    raise StepError(f"Unknown step type {stype}")


def _create_opportunity(ctx: dict, cfg: dict) -> str:
    c = frappe.db.get_value("WA Contact", ctx["wa_contact"], ["customer", "lead", "full_name", "wa_id"], as_dict=True)
    if c.customer:
        party_type, party = "Customer", c.customer
    else:
        party_type, party = "Lead", c.lead or ensure_lead(ctx["wa_contact"])
    existing = frappe.db.get_value("Opportunity", {"opportunity_from": party_type, "party_name": party,
                                                    "status": ("in", ["Open", "Replied"])}, "name")
    if existing:
        return existing
    opp = frappe.get_doc({
        "doctype": "Opportunity", "opportunity_from": party_type, "party_name": party,
        "opportunity_type": cfg.get("opportunity_type") or "Sales",
        "opportunity_amount": cfg.get("amount") or 0, "contact_mobile": f"+{c.wa_id}" if c.wa_id else None,
        "source": frappe.db.get_single_value("WA CRM Settings", "lead_source") or None,
    })
    opp.flags.ignore_mandatory = True
    opp.insert(ignore_permissions=True)
    return opp.name
