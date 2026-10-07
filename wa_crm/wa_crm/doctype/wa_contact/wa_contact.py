# Copyright (c) 2026, WA CRM and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from wa_crm.utils import norm


class WAContact(Document):
	def validate(self):
		if self.wa_id:
			self.wa_id = norm(self.wa_id)
		if not self.full_name:
			self.full_name = self.wa_id or self.username
		if self.contact and not self.customer:
			self.customer = frappe.db.get_value(
				"Dynamic Link",
				{"parenttype": "Contact", "parent": self.contact, "link_doctype": "Customer"},
				"link_name",
			)
