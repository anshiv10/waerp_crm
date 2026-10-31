"""ERP Assistant on WhatsApp: registered staff numbers query and act on ERPNext records.

Flow per message (runs inside the inbound worker, before flows / automations / AI auto-reply):
  registered number? -> parse (exact command, button tap, or the AI's JSON intent)
  -> run as that person's ERPNext user (their roles and permissions apply)
  -> reads answer at once; changes (submit / cancel / amend / update) ask Yes/No first
  -> every change and PDF is written to WA ERP Action Log.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from waerp_crm.assistant import render
from waerp_crm.assistant.parse import extract_json, from_ai, parse_command
from waerp_crm.assistant.resolve import (
    AssistantError, build_filters, check_doctype_allowed, date_field, resolve_doctype, resolve_ref,
    update_value, updatable_field,
)

ACTION_LABEL = {"submit": "Submit", "cancel": "Cancel", "amend": "Amend", "update": "Update", "pdf": "PDF"}
BUTTON = {"submit": "✅ Submit", "cancel": "🚫 Cancel", "amend": "✏️ Amend", "pdf": "📄 PDF"}


def settings():
    return frappe.get_cached_doc("WA ERP Assistant Settings")


def erp_user_for(wa_contact: str):
    wa_id = frappe.db.get_value("WA Contact", wa_contact, "wa_id")
    if not wa_id:
        return None
    return frappe.db.get_value("WA ERP User", {"phone": wa_id, "enabled": 1}, "*", as_dict=True)


# ------------------------------------------------------------------ entry point (called by dispatch)

def maybe_handle(ctx) -> bool:
    """True when the message belonged to the ERP Assistant (then customer engines skip it)."""
    s = settings()
    if not s.enabled:
        return False
    eu = erp_user_for(ctx.wa_contact)
    if not eu:
        return False
    rid = ctx.interactive_reply_id or ""
    text = (ctx.text or "").strip()
    prefix = (s.command_prefix or "").strip()
    if rid.startswith("erp:"):
        pass
    elif prefix:
        low, p = text.lower(), prefix.lower()
        if low == p:
            text = "help"
        elif low.startswith(p + " "):
            text = text[len(prefix):].strip()
        else:
            return False  # not for the assistant: let the normal customer engines handle it
    elif ctx.message_type not in ("text", "interactive"):
        _send(ctx, [("text", _("Please type your request as text. Send *help* for examples."))])
        return True

    Session(ctx, eu, s).run(text, rid)
    _quiet(ctx.conversation)
    return True


def _quiet(conversation: str):
    """Assistant chats don't wait for an agent in the inbox."""
    from waerp_crm.utils import fresh_write

    fresh_write(lambda: frappe.db.sql(
        """update `tabWA Conversation` set unread_count = 0, awaiting_reply_since = null where name = %s""",
        conversation))


def _send(ctx, replies: list):
    from waerp_crm.whatsapp.outbound import send_buttons, send_list, send_media, send_text

    for r in replies:
        kind = r[0]
        if kind == "text":
            send_text(ctx.conversation, r[1], source="Assistant", raise_on_error=False)
        elif kind == "buttons":
            send_buttons(ctx.conversation, {"buttons": r[2]}, source="Assistant", raise_on_error=False, text=r[1])
        elif kind == "list":
            send_list(ctx.conversation, {"button_label": _("Open one"), "sections": r[2]}, source="Assistant",
                      raise_on_error=False, text=r[1])
        elif kind == "pdf":
            _k, content, filename, caption, ref_dt, ref_name = r
            send_media(ctx.conversation, content=content, mime="application/pdf", filename=filename,
                       caption=caption, source="Assistant", raise_on_error=False,
                       reference_doctype=ref_dt, reference_name=ref_name)
        frappe.db.commit()


# ------------------------------------------------------------------ one conversation turn

class Session:
    def __init__(self, ctx, erp_user, s):
        self.ctx, self.eu, self.s = ctx, erp_user, s
        self.key = f"waerp_crm:erp_session:{erp_user.phone}"
        self.state = frappe.cache.get_value(self.key) or {}
        self.replies: list = []

    def save_state(self):
        frappe.cache.set_value(self.key, self.state, expires_in_sec=max(5, self.s.session_minutes or 30) * 60)

    def say(self, text):
        self.replies.append(("text", text))

    def run(self, text: str, rid: str):
        previous = frappe.session.user
        frappe.set_user(self.eu.user)
        try:
            self._route(text, rid)
            self.save_state()
            frappe.db.commit()
        except AssistantError as e:
            frappe.db.rollback()
            self.say(f"⚠️ {e}")
        except frappe.PermissionError:
            frappe.db.rollback()
            self.say(_("⛔ Your ERPNext user doesn't have permission for that."))
        except frappe.ValidationError as e:
            frappe.db.rollback()
            self.say("⚠️ " + (_clean(e) or _("ERPNext refused the request.")))
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="WAERP CRM: ERP Assistant failed")
            self.say(_("Something went wrong; it has been logged for your administrator (Error Log)."))
        finally:
            frappe.clear_messages()
            frappe.set_user(previous)
        _send(self.ctx, self.replies)

    # -------------------------------------------------------------- routing
    def _route(self, text: str, rid: str):
        if rid.startswith("erp:"):
            return self._button(rid)
        cmd = parse_command(text)
        if cmd["intent"] == "free":
            cmd = self._ai(text)
            if cmd is None:
                raise AssistantError(_("I didn't understand that. Send *help* to see what I can do."))
            if cmd["intent"] == "unknown":
                return self.say(cmd.get("reply") or _("I didn't understand that. Send *help* for examples."))
        intent = cmd["intent"]
        if intent == "help":
            return self.say(render.help_text(self.eu, frappe.utils.get_fullname(self.eu.user), self.s.command_prefix))
        if intent == "confirm":
            return self._confirm(None)
        if intent == "decline":
            return self._decline(None)
        if intent == "find":
            return self._find(cmd)
        # show / submit / cancel / amend / pdf / update: needs one document
        target = self._target(cmd)
        if not target:
            return  # a list was shown so the person can pick
        dt, name = target
        if intent == "show":
            return self._show(dt, name)
        if intent == "pdf":
            return self._pdf(dt, name)
        if intent == "update":
            return self._ask_update(dt, name, cmd["updates"])
        return self._ask_action(intent, dt, name)

    def _button(self, rid: str):
        parts = rid.split(":", 2)
        kind = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        if kind == "show":
            dt, name = arg.split("|", 1)
            return self._show(dt, name)
        if kind == "do":
            action, ref = arg.split(":", 1)
            dt, name = ref.split("|", 1)
            if action == "pdf":
                return self._pdf(dt, name)
            return self._ask_action(action, dt, name)
        if kind == "yes":
            return self._confirm(arg)
        if kind == "no":
            return self._decline(arg)
        raise AssistantError(_("That button has expired. Send *help*."))

    def _target(self, cmd) -> tuple[str, str] | None:
        """The one document a command is about: an explicit ref, the current one, or a search."""
        hint = resolve_doctype(cmd.get("doctype")) if cmd.get("doctype") else None
        if cmd.get("ref") or (not cmd.get("filters") and not hint):
            dt, name = resolve_ref(cmd.get("ref"), self.state, self.eu, hint)
            return dt, name
        rows = self._search(hint, cmd.get("filters") or [], show_list=False)
        if rows is None:
            return None
        dt, found = rows
        if len(found) == 1:
            return dt, found[0].name
        self._list(dt, found, [], more=False)
        if found:
            self.say(_("Several match: pick one from the list, then send your request again (e.g. *{0} it*).")
                     .format(cmd["intent"] if cmd["intent"] != "show" else "show"))
        return None

    # -------------------------------------------------------------- AI
    def _ai(self, text: str):
        ai = frappe.get_cached_doc("WA AI Settings")
        if not (self.s.use_ai and ai.enabled):
            return None
        from waerp_crm.ai.providers import chat

        cur = self.state.get("current")
        results = self.state.get("results") or []
        system = AI_PROMPT.format(
            today=frappe.utils.formatdate(frappe.utils.nowdate(), "dd-MM-yyyy"),
            current=f"{cur[0]} {cur[1]}" if cur else "none",
            results="; ".join(f"{i}. {d} {n}" for i, (d, n) in enumerate(results[:10], 1)) or "none",
        )
        from waerp_crm.ai.providers import AIError

        try:
            raw, usage = chat(ai, system, [{"role": "user", "content": text}])
        except AIError as e:
            frappe.log_error(title="WAERP CRM: ERP Assistant AI call failed")
            raise AssistantError(_("I couldn't reach the AI to understand that ({0}). Exact commands still work: "
                                   "send *help*.").format(str(e)[:150])) from None
        try:
            frappe.get_doc({"doctype": "WA AI Usage Log", "conversation": self.ctx.conversation,
                            "mode": "erp_assistant", "provider": ai.provider, "model": ai.model,
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0)}).insert(ignore_permissions=True)
        except Exception:
            pass
        data = extract_json(raw)
        return from_ai(data, text) if data else None

    # -------------------------------------------------------------- reads
    def _search(self, doctype: str | None, pairs, show_list=True):
        if not doctype:
            raise AssistantError(_("Which document type? e.g. *find sales order customer: Harshit Dixit*"))
        check_doctype_allowed(doctype, self.eu)
        meta = frappe.get_meta(doctype)
        filters, described = build_filters(doctype, pairs)
        limit = min(max(self.s.max_results or 10, 1), 10)
        dfield = date_field(meta)
        child = any(f[0] != doctype for f in filters)
        rows = frappe.get_list(doctype, filters=filters, fields=render.list_fields(meta),
                               order_by=f"`tab{doctype}`.`{dfield}` desc, `tab{doctype}`.`modified` desc",
                               limit=limit + 1, distinct=child)
        more = len(rows) > limit
        rows = rows[:limit]
        if not rows:
            self.say(_("No {0} found{1}.").format(doctype, f" ({', '.join(described)})" if described else ""))
            return None
        if show_list:
            if len(rows) == 1:
                return self._show(doctype, rows[0].name)
            self._list(doctype, rows, described, more)
        return doctype, rows

    def _find(self, cmd):
        text = (cmd.get("doctype") or "").strip()
        pairs = list(cmd.get("filters") or [])
        dt = resolve_doctype(text)
        if text and not dt and pairs:
            # 'find sales order delivery date: 05-11-2026': the label's first words were read as the DocType
            words = text.split()
            for n in range(len(words) - 1, 0, -1):
                dt = resolve_doctype(" ".join(words[:n]))
                if dt:
                    pairs[0] = (" ".join(words[n:] + [pairs[0][0]]).lower(), pairs[0][1])
                    break
        if text and not dt:
            raise AssistantError(_("I don't know a document type called '{0}'.").format(text))
        self._search(dt, pairs)

    def _list(self, doctype, rows, described, more):
        meta = frappe.get_meta(doctype)
        self.state["results"] = [[doctype, r.name] for r in rows]
        body = render.results_text(doctype, rows, described, more)
        section_rows = [{"reply_id": f"erp:show:{doctype}|{r.name}"[:200], "title": r.name[:24],
                         "description": render.row_summary(meta, r)[:72]} for r in rows[:10]]
        self.replies.append(("list", body, [{"title": doctype[:24], "rows": section_rows}]))

    def _load(self, dt: str, name: str):
        check_doctype_allowed(dt, self.eu)
        doc = frappe.get_doc(dt, name)
        doc.check_permission("read")
        self.state["current"] = [dt, name]
        return doc

    def _show(self, dt: str, name: str):
        doc = self._load(dt, name)
        self.say(render.details_text(doc))
        buttons = [{"reply_id": f"erp:do:{a}:{dt}|{name}"[:200], "title": BUTTON[a]}
                   for a in self._available_actions(doc)][:3]
        if buttons:
            self.replies.append(("buttons", _("What would you like to do with {0}?").format(name), buttons))

    def _available_actions(self, doc) -> list[str]:
        out = []
        meta = frappe.get_meta(doc.doctype)
        if meta.is_submittable:
            if doc.docstatus == 0 and self.eu.can_submit and doc.has_permission("submit"):
                out.append("submit")
            if doc.docstatus == 1 and self.eu.can_cancel and doc.has_permission("cancel"):
                out.append("cancel")
            if doc.docstatus == 2 and self.eu.can_amend and doc.has_permission("amend"):
                out.append("amend")
        if doc.has_permission("print"):
            out.append("pdf")
        return out

    def _pdf(self, dt: str, name: str):
        doc = self._load(dt, name)
        if not doc.has_permission("print"):
            raise AssistantError(_("Your ERPNext user can't print {0}.").format(dt))
        from waerp_crm.utils import render_pdf

        pdf = render_pdf(dt, name)
        self.replies.append(("pdf", pdf, f"{name}.pdf", f"{dt} {name}", dt, name))
        self._log("PDF", doc, "Done")

    # -------------------------------------------------------------- changes (always confirmed)
    def _ask_action(self, action: str, dt: str, name: str):
        doc = self._load(dt, name)
        self._check_action(action, doc)
        summary = render.row_summary(frappe.get_meta(dt), doc)
        body = _("Please confirm: *{0}* {1} *{2}*\n{3}").format(ACTION_LABEL[action], dt, name, summary)
        if action == "cancel":
            body += "\n" + _("Cancelling can't be undone (you can amend it afterwards).")
        self._ask(action, doc, body, {})

    def _check_action(self, action: str, doc):
        meta = frappe.get_meta(doc.doctype)
        flag = {"submit": "can_submit", "cancel": "can_cancel", "amend": "can_amend"}[action]
        if not self.eu.get(flag):
            raise AssistantError(_("You are not allowed to {0} from WhatsApp.").format(action))
        if not meta.is_submittable:
            raise AssistantError(_("{0} documents can't be submitted, cancelled or amended.").format(doc.doctype))
        need = {"submit": 0, "cancel": 1, "amend": 2}[action]
        if doc.docstatus != need:
            raise AssistantError(_("{0} is {1}, so it can't be {2}.").format(
                doc.name, render.DOCSTATUS[doc.docstatus].lower(),
                {"submit": "submitted", "cancel": "cancelled", "amend": "amended"}[action]))
        if not doc.has_permission(action):
            raise AssistantError(_("Your ERPNext user doesn't have {0} permission on {1}.").format(action, doc.doctype))

    def _ask_update(self, dt: str, name: str, updates):
        if not self.eu.can_update:
            raise AssistantError(_("You are not allowed to update documents from WhatsApp."))
        if not updates:
            raise AssistantError(_("What should I change? e.g. *set {0} delivery date = 05-11-2026*").format(name))
        doc = self._load(dt, name)
        changes, lines = {}, []
        for key, raw in updates:
            meta, df = updatable_field(doc, key)
            value = update_value(meta, df, raw)
            changes[df.fieldname] = value
            old = render.fmt(doc.get(df.fieldname), df, doc) or "—"
            new = render.fmt(value, df, doc) or "—"
            lines.append(f"• {df.label or df.fieldname}: {old} → *{new}*")
        body = _("Please confirm the change to {0} *{1}*:").format(dt, name) + "\n" + "\n".join(lines)
        self._ask("update", doc, body, changes)

    def _ask(self, action: str, doc, body: str, payload: dict):
        for old in frappe.get_all("WA ERP Action Log", filters={"phone": self.eu.phone, "status": "Pending"},
                                  pluck="name"):
            frappe.db.set_value("WA ERP Action Log", old, "status", "Expired", update_modified=False)
        log = self._log(ACTION_LABEL[action], doc, "Pending", payload=payload,
                        expires_at=add_to_date(now_datetime(), minutes=self.s.confirm_minutes or 10))
        frappe.db.commit()
        verb = {"submit": "Submit", "cancel": "Cancel it", "amend": "Amend", "update": "Update"}[action]
        self.replies.append(("buttons", body, [
            {"reply_id": f"erp:yes:{log}", "title": f"✅ Yes, {verb}"[:20]},
            {"reply_id": f"erp:no:{log}", "title": "❌ No"},
        ]))

    def _pending(self, log_name: str | None):
        if log_name:
            log = frappe.db.get_value("WA ERP Action Log", log_name, "*", as_dict=True)
            if not log or log.phone != self.eu.phone:
                raise AssistantError(_("That confirmation doesn't belong to you."))
        else:
            name = frappe.db.get_value("WA ERP Action Log", {"phone": self.eu.phone, "status": "Pending"},
                                       "name", order_by="creation desc")
            log = frappe.db.get_value("WA ERP Action Log", name, "*", as_dict=True) if name else None
        if not log or log.status != "Pending":
            raise AssistantError(_("Nothing is waiting for your confirmation."))
        if log.expires_at and get_datetime(log.expires_at) < now_datetime():
            frappe.db.set_value("WA ERP Action Log", log.name, "status", "Expired", update_modified=False)
            frappe.db.commit()
            raise AssistantError(_("That request expired. Please ask again."))
        return log

    def _decline(self, log_name):
        log = self._pending(log_name)
        frappe.db.set_value("WA ERP Action Log", log.name, "status", "Declined", update_modified=False)
        frappe.db.commit()
        self.say(_("OK, nothing was changed."))

    def _confirm(self, log_name):
        log = self._pending(log_name)
        dt, name, action = log.reference_doctype, log.reference_name, log.action.lower()
        try:
            doc = self._load(dt, name)
            if action == "update":
                if not self.eu.can_update:
                    raise AssistantError(_("You are not allowed to update documents from WhatsApp."))
                payload = json.loads(log.payload or "{}")
                for f in payload:
                    updatable_field(doc, f)
                doc.update(payload)
                doc.save()
                result_doc = doc
                result = _("Updated {0}.").format(name)
            else:
                self._check_action(action, doc)
                if action == "submit":
                    doc.submit()
                    result_doc = doc
                elif action == "cancel":
                    doc.cancel()
                    result_doc = doc
                else:  # amend: a new draft copy linked to the cancelled original
                    new = frappe.copy_doc(doc)
                    new.amended_from = doc.name
                    new.docstatus = 0
                    new.insert()
                    result_doc = new
                result = {"submit": _("Submitted {0}."), "cancel": _("Cancelled {0}."),
                          "amend": _("Created amended draft {0} from {1}.")}[action].format(result_doc.name, name)
            frappe.db.commit()
        except Exception as e:
            frappe.db.rollback()
            msg = str(e) if isinstance(e, AssistantError) else (_clean(e) or e.__class__.__name__)
            frappe.db.set_value("WA ERP Action Log", log.name, {"status": "Failed", "result": msg[:1000]},
                                update_modified=False)
            frappe.db.commit()
            raise AssistantError(_("{0} failed: {1}").format(log.action, msg)) from None
        frappe.db.set_value("WA ERP Action Log", log.name,
                            {"status": "Done", "result": result, "result_name": result_doc.name},
                            update_modified=False)
        frappe.db.commit()
        self.say("✅ " + result)
        self._show(result_doc.doctype, result_doc.name)

    def _log(self, action, doc, status, payload=None, expires_at=None) -> str:
        return frappe.get_doc({
            "doctype": "WA ERP Action Log", "erp_user": self.eu.name, "user": self.eu.user,
            "phone": self.eu.phone, "action": action, "reference_doctype": doc.doctype,
            "reference_name": doc.name, "status": status, "expires_at": expires_at,
            "payload": json.dumps(payload or {}, default=str), "conversation": self.ctx.conversation,
        }).insert(ignore_permissions=True).name


def _clean(e) -> str:
    text = frappe.utils.strip_html(str(e) or "").strip()
    if not text:
        msgs = frappe.get_message_log() if hasattr(frappe, "get_message_log") else []
        text = " ".join(frappe.utils.strip_html(m.get("message", "")) for m in msgs if isinstance(m, dict))
    return text[:500]


AI_PROMPT = """You turn a staff member's WhatsApp message into ONE JSON command for an ERPNext assistant.
Reply with a single JSON object and nothing else.

Today is {today}. Dates the user writes are day-first (29-10-2026 = 29 October 2026). Output dates as YYYY-MM-DD.

JSON fields:
- "intent": one of "find" (search several records), "show" (details of one record), "submit", "cancel",
  "amend", "update" (change field values), "pdf" (send its PDF), "help", "unknown".
- "doctype": the ERPNext DocType, e.g. "Sales Order", "Purchase Order", "Work Order", "BOM",
  "Sales Invoice", "Delivery Note", "Quotation", "Material Request", or a custom DocType name the user says. null if not said.
- "filters": object of conditions, keys like "customer", "supplier", "item", "status", "date",
  "from", "to", or any field label. Example: {{"customer": "Harshit Dixit", "date": "2026-10-29"}}.
- "ref": the document ID if the user gives one (e.g. "SAL-ORD-2026-00012"), a number from the last list
  (e.g. "2"), "it" when they mean the document being discussed, otherwise null. Never invent IDs.
- "updates": for "update" only, object of field label -> new value, e.g. {{"delivery date": "2026-11-05"}}.
- "reply": for "unknown" only, one short question asking what they need.

When the user asks to submit / cancel / show / pdf / update a record described by filters (not an ID),
keep that intent and put the description in "doctype" and "filters".

Document being discussed: {current}
Last list shown: {results}"""
