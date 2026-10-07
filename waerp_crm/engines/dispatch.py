"""Engine 2 — the dispatch order. Deterministic logic always beats the LLM.

    inbound
      -> Flow engine            (active run? advance it. else maybe start one)
      -> Automations            relationship triggers always; content triggers
                                only if no flow consumed the message
      -> Away message           outside business hours, once per cooldown
      -> AI auto-reply          only if nothing above answered and gates pass
      -> Outbound webhooks      message.received for external systems

Every stage owns its own try/except: one failing stage never blocks the next,
and nothing here can raise back into the webhook worker.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from waerp_crm.core.logic import within_business_hours


def _safe(title, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        frappe.log_error(title=f"WAERP CRM: {title} failed")
        return None


def dispatch_inbound(ctx):
    from waerp_crm.ai.auto_reply import dispatch_ai_reply
    from waerp_crm.engines.automations import run_for_trigger
    from waerp_crm.engines.flows import dispatch_inbound_to_flows
    from waerp_crm.engines.outbound_webhooks import emit

    flow_result = _safe("flow dispatch", dispatch_inbound_to_flows, ctx) or {}
    consumed = bool(flow_result.get("consumed"))
    frappe.db.commit()

    triggers = []
    if ctx.is_first_inbound:
        triggers.append("First Inbound Message")
    if ctx.contact_created:
        triggers.append("New Contact Created")
    if not consumed:
        triggers += ["New Message Received", "Keyword Match"]
        if ctx.interactive_reply_id:
            triggers.append("Interactive Reply")
    answered_by_automation = False
    for trig in triggers:
        sent = _safe(f"automation trigger {trig}", run_for_trigger, trig, ctx)
        answered_by_automation = answered_by_automation or bool(sent)
        frappe.db.commit()

    away_sent = False
    if not consumed:
        away_sent = bool(_safe("away message", maybe_send_away_message, ctx))

    if not consumed and not answered_by_automation and not away_sent and not ctx.interactive_reply_id \
            and (ctx.text or "").strip() and ctx.message_type == "text":
        _safe("AI auto-reply", dispatch_ai_reply, ctx)
        frappe.db.commit()

    emit("message.received", {"conversation": ctx.conversation, "wa_contact": ctx.wa_contact,
                              "message": ctx.message, "wamid": ctx.wamid, "type": ctx.message_type,
                              "text": ctx.text})


def maybe_send_away_message(ctx) -> bool:
    acc = frappe.get_cached_doc("WA Account", ctx.account)
    if not acc.business_hours_enabled or not acc.away_message or not acc.business_hours:
        return False
    ai = frappe.get_cached_doc("WA AI Settings")
    if ai.enabled and ai.auto_reply_enabled:
        return False  # the 24x7 AI bot answers instead of a static away message
    rows = [r.as_dict() for r in acc.business_hours]
    if within_business_hours(now_datetime(), rows):
        return False
    last = frappe.db.get_value("WA Conversation", ctx.conversation, "away_sent_at")
    cooldown = acc.away_message_cooldown_hours or 12
    if last and get_datetime(last) > add_to_date(now_datetime(), hours=-cooldown):
        return False
    from waerp_crm.whatsapp.outbound import send_text
    send_text(ctx.conversation, acc.away_message, source="System", raise_on_error=False)
    frappe.db.set_value("WA Conversation", ctx.conversation, "away_sent_at", now_datetime(), update_modified=False)
    return True
