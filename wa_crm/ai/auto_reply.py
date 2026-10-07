"""AI auto-reply bot + agent-side drafts/summaries.

Gates for auto-reply (any fails -> silently do nothing; the message stays in the inbox):
  AI enabled and auto-reply on; no human assigned; not paused on this thread;
  under the per-conversation cap; under the account-wide per-minute rate limit.

The model answers with the literal [[HANDOFF]] when it can't help safely; we
then pause the bot on that thread, leave a summary note and assign a human.
The reply slot is claimed with a conditional UPDATE so two concurrent inbounds
can never overshoot the cap.
"""

from __future__ import annotations

import frappe
from frappe import _

from wa_crm.ai.knowledge import erp_context, retrieve
from wa_crm.ai.providers import chat
from wa_crm.core.logic import HANDOFF_SENTINEL, handoff_summary, parse_ai_output, to_chat_turns
from wa_crm.utils import add_note, assign_conversation


def _history(conversation: str, limit: int) -> list[dict]:
    rows = frappe.get_all("WA Message", filters={"conversation": conversation, "is_note": 0},
                          fields=["direction", "content", "message_type"], order_by="timestamp desc, creation desc",
                          limit=limit)
    return list(reversed(rows))


def build_system_prompt(s, mode: str, knowledge: list[str], erp: str) -> str:
    parts = [s.system_prompt or "You are a helpful customer-support assistant."]
    parts.append("You are chatting on WhatsApp: keep replies short, plain text, no markdown headings.")
    if knowledge:
        parts.append("Answer ONLY from this business knowledge when relevant:\n---\n" + "\n---\n".join(knowledge))
    if erp:
        parts.append("Facts about this customer from the ERP (confidential, use only to help them):\n" + erp)
    if mode == "auto_reply":
        parts.append(
            "You are replying automatically with no human in the loop. If you cannot confidently and safely help — "
            "the customer asks for a human, is upset, wants to negotiate price or place a complex order, or the answer "
            f"is not in the knowledge above — reply with exactly {HANDOFF_SENTINEL} and nothing else. "
            "Never invent prices, stock, dates or policies.")
    else:
        parts.append("Draft a reply for a human agent to review and send.")
    return "\n\n".join(parts)


def _log_usage(conversation, mode, s, usage):
    try:
        frappe.get_doc({"doctype": "WA AI Usage Log", "conversation": conversation, "mode": mode,
                        "provider": s.provider, "model": s.model, "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0)}).insert(ignore_permissions=True)
    except Exception:
        pass


def _rate_limited(limit: int) -> bool:
    key = f"{frappe.local.site}:wa_crm:ai_rpm:{frappe.utils.now_datetime().strftime('%Y%m%d%H%M')}"
    count = frappe.cache.incr(key)
    if count == 1:
        frappe.cache.expire(key, 120)
    return count > (limit or 30)


def dispatch_ai_reply(ctx):
    s = frappe.get_cached_doc("WA AI Settings")
    if not (s.enabled and s.auto_reply_enabled):
        return
    conv = frappe.db.get_value("WA Conversation", ctx.conversation,
                               ["assigned_to", "ai_autoreply_disabled", "ai_reply_count", "wa_account"], as_dict=True)
    if not conv or conv.assigned_to or conv.ai_autoreply_disabled:
        return
    cap = s.max_auto_replies or 5
    if (conv.ai_reply_count or 0) >= cap or _rate_limited(s.requests_per_minute):
        return

    try:  # "typing..." + blue ticks while we think (best-effort)
        from wa_crm.whatsapp.graph import GraphClient
        GraphClient(conv.wa_account).mark_read(ctx.wamid, typing=True)
    except Exception:
        pass

    history = _history(ctx.conversation, s.history_messages or 20)
    turns = to_chat_turns(history)
    if not turns:
        return
    knowledge = retrieve(ctx.text, s.knowledge_chunks or 5)
    erp = erp_context(ctx.wa_contact) if s.include_erp_context else ""
    raw, usage = chat(s, build_system_prompt(s, "auto_reply", knowledge, erp), turns)
    _log_usage(ctx.conversation, "auto_reply", s, usage)
    text, handoff = parse_ai_output(raw)

    if handoff or not text:
        frappe.db.set_value("WA Conversation", ctx.conversation, {
            "ai_autoreply_disabled": 1, "ai_handoff_summary": handoff_summary(history)})
        assign_conversation(ctx.conversation, s.handoff_agent, auto=not s.handoff_agent)
        add_note(ctx.conversation, handoff_summary(history))
        return

    frappe.db.sql("""update `tabWA Conversation` set ai_reply_count = ai_reply_count + 1
                     where name=%s and ai_reply_count < %s""", (ctx.conversation, cap))
    if not frappe.db._cursor.rowcount:
        return  # lost the race for the last slot
    from wa_crm.whatsapp.outbound import send_text
    send_text(ctx.conversation, text, source="AI", is_ai=True, raise_on_error=False)


# ------------------------------------------------------------------ agent tools (inbox buttons)


@frappe.whitelist()
def draft_reply(conversation: str, instruction: str | None = None) -> str:
    frappe.has_permission("WA Conversation", "write", conversation, throw=True)
    s = frappe.get_cached_doc("WA AI Settings")
    if not s.enabled:
        frappe.throw(_("Enable AI in WA AI Settings first."))
    history = _history(conversation, s.history_messages or 20)
    turns = to_chat_turns(history)
    if not turns:
        frappe.throw(_("Nothing to reply to yet."))
    wa_contact = frappe.db.get_value("WA Conversation", conversation, "wa_contact")
    last_customer = next((m.content for m in reversed(history) if m.direction == "Inbound" and m.content), "")
    system = build_system_prompt(s, "draft", retrieve(last_customer, s.knowledge_chunks or 5),
                                 erp_context(wa_contact) if s.include_erp_context else "")
    if instruction:
        system += f"\n\nAgent instruction for this draft: {instruction}"
    raw, usage = chat(s, system, turns)
    _log_usage(conversation, "draft", s, usage)
    return parse_ai_output(raw)[0]


@frappe.whitelist()
def summarize(conversation: str) -> str:
    frappe.has_permission("WA Conversation", "read", conversation, throw=True)
    s = frappe.get_cached_doc("WA AI Settings")
    history = _history(conversation, 60)
    transcript = "\n".join(f"{'Customer' if m.direction == 'Inbound' else 'Us'}: {m.content}" for m in history if m.content)
    raw, usage = chat(s, "Summarise this WhatsApp support conversation in 3-5 bullet points: need, status, next step.",
                      [{"role": "user", "content": transcript or "(empty)"}])
    _log_usage(conversation, "summary", s, usage)
    return raw.strip()
