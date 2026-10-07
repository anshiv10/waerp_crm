# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WAWebhookEndpoint(Document):
	def validate(self):
		if not (self.url or "").startswith(("https://", "http://")):
			import frappe

			frappe.throw("URL must start with https://")

	def on_update(self):
		from waerp_crm.engines.outbound_webhooks import clear_cache

		clear_cache()
