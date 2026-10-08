# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WAConversation(Document):
	def on_update(self):
		before = self.get_doc_before_save()
		if not before:
			return
		from waerp_crm.engines.outbound_webhooks import emit
		from waerp_crm.utils import publish

		if self.assigned_to and self.assigned_to != before.assigned_to:
			self._notify_assignee()
			emit("conversation.assigned", {"conversation": self.name, "assigned_to": self.assigned_to})
			frappe.enqueue("waerp_crm.engines.automations.fire_conversation_event", queue="short",
				trigger_type="Conversation Assigned", conversation=self.name, enqueue_after_commit=True)
		if self.status == "Resolved" and before.status != "Resolved":
			from waerp_crm.utils import resume_bots

			resume_bots(self.name)
			emit("conversation.resolved", {"conversation": self.name})
			frappe.enqueue("waerp_crm.engines.automations.fire_conversation_event", queue="short",
				trigger_type="Conversation Resolved", conversation=self.name, enqueue_after_commit=True)
		publish("conversation", self.name, {"status": self.status, "assigned_to": self.assigned_to})

	def _notify_assignee(self):
		frappe.get_doc({
			"doctype": "Notification Log",
			"subject": f"WhatsApp conversation with {self.contact_name or self.wa_id} assigned to you",
			"for_user": self.assigned_to,
			"type": "Assignment",
			"document_type": "WA Conversation",
			"document_name": self.name,
		}).insert(ignore_permissions=True)


def get_permission_query_conditions(user=None):
	"""Optional: agents only see conversations assigned to them or unassigned."""
	user = user or frappe.session.user
	if user == "Administrator" or not frappe.db.get_single_value("WA CRM Settings", "restrict_agent_visibility"):
		return ""
	roles = frappe.get_roles(user)
	if "WA Manager" in roles or "System Manager" in roles:
		return ""
	u = frappe.db.escape(user)
	return f"(`tabWA Conversation`.assigned_to = {u} or ifnull(`tabWA Conversation`.assigned_to, '') = '')"


def has_permission(doc, ptype=None, user=None):
	user = user or frappe.session.user
	if user == "Administrator" or not frappe.db.get_single_value("WA CRM Settings", "restrict_agent_visibility"):
		return True
	roles = frappe.get_roles(user)
	if "WA Manager" in roles or "System Manager" in roles:
		return True
	return not doc.assigned_to or doc.assigned_to == user
