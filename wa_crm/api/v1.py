"""Public REST API for integrators.

Auth: standard Frappe API keys —  Authorization: token <api_key>:<api_secret>
of a user with the WA Manager or WA Agent role.

  POST /api/method/wa_crm.api.v1.send_text      {phone, text, account?}
  POST /api/method/wa_crm.api.v1.send_template  {phone, template, params?: [..], header_file?, account?}
  GET  /api/method/wa_crm.api.v1.conversations  {status?, limit?}
  GET  /api/method/wa_crm.api.v1.messages       {conversation, limit?}
  POST /api/method/wa_crm.api.v1.start_flow     {phone, flow, account?}
  POST /api/method/wa_crm.api.v1.tag            {phone, tag, remove?}

Plus Frappe's generic REST on every DocType: /api/resource/WA Contact etc.
"""

from __future__ import annotations

import frappe

from wa_crm.whatsapp import outbound


def _guard():
    frappe.only_for(("WA Manager", "WA Agent", "System Manager"))


@frappe.whitelist(methods=["POST"])
def send_text(phone: str, text: str, account: str | None = None):
    _guard()
    conv = outbound.conversation_for_number(phone, account)
    msg = outbound.send_text(conv, text, source="API")
    return {"conversation": conv, "message": msg.name, "wamid": msg.message_id, "status": msg.status}


@frappe.whitelist(methods=["POST"])
def send_template(phone: str, template: str, params=None, header_file: str | None = None, account: str | None = None,
                  name: str | None = None):
    _guard()
    account = account or frappe.db.get_value("WA Template", template, "wa_account")
    conv = outbound.conversation_for_number(phone, account, name=name)
    msg = outbound.send_template(conv, template, frappe.parse_json(params) or [], header_file=header_file, source="API")
    return {"conversation": conv, "message": msg.name, "wamid": msg.message_id, "status": msg.status}


@frappe.whitelist(methods=["GET"])
def conversations(status: str | None = None, limit: int = 50):
    _guard()
    return frappe.get_list("WA Conversation", filters={"status": status} if status else {},
                           fields=["name", "contact_name", "wa_id", "status", "assigned_to", "unread_count",
                                   "last_message", "last_message_at"],
                           order_by="last_message_at desc", page_length=min(int(limit), 500))


@frappe.whitelist(methods=["GET"])
def messages(conversation: str, limit: int = 50):
    _guard()
    frappe.has_permission("WA Conversation", "read", conversation, throw=True)
    return frappe.get_all("WA Message", filters={"conversation": conversation, "is_note": 0},
                          fields=["name", "direction", "message_type", "content", "status", "timestamp", "message_id"],
                          order_by="timestamp desc", limit=min(int(limit), 500))


@frappe.whitelist(methods=["POST"])
def start_flow(phone: str, flow: str, account: str | None = None):
    _guard()
    from wa_crm.engines.flows import start_run
    conv = outbound.conversation_for_number(phone, account)
    return start_run(flow, frappe.db.get_value("WA Conversation", conv, "wa_contact"), conv)


@frappe.whitelist(methods=["POST"])
def tag(phone: str, tag: str, remove: int = 0):
    _guard()
    from wa_crm.utils import add_tag, get_or_create_wa_contact, remove_tag
    wa_contact, _ = get_or_create_wa_contact(phone)
    return remove_tag(wa_contact, tag) if int(remove) else add_tag(wa_contact, tag)
