# Copyright (c) 2026, WA CRM and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url


class WAAccount(Document):
	def validate(self):
		self.phone_number_id = (self.phone_number_id or "").strip()
		self.waba_id = (self.waba_id or "").strip()
		self.webhook_url = get_url("/api/method/wa_crm.api.webhook.receive")
		if self.is_default:
			frappe.db.sql("update `tabWA Account` set is_default=0 where name!=%s", self.name)
		users = [a.user for a in self.agents]
		if len(users) != len(set(users)):
			frappe.throw(_("Each agent can be listed only once."))

	def on_update(self):
		frappe.clear_document_cache("WA Account", self.name)

	@frappe.whitelist()
	def test_connection(self):
		"""Fetch phone details from Meta; also proves the token works."""
		from wa_crm.whatsapp.graph import GraphClient

		info = GraphClient(self).phone_info()
		self.db_set({
			"display_phone_number": info.get("display_phone_number"),
			"verified_name": info.get("verified_name"),
			"quality_rating": info.get("quality_rating"),
			"messaging_limit": info.get("messaging_limit_tier"),
		})
		return info

	@frappe.whitelist()
	def subscribe_webhooks(self):
		from wa_crm.whatsapp.graph import GraphClient

		return GraphClient(self).subscribe_app()

	@frappe.whitelist()
	def sync_templates(self):
		from wa_crm.whatsapp.templates import sync_templates

		return sync_templates(self.name)
