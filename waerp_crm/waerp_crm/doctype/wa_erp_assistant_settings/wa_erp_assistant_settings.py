# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WAERPAssistantSettings(Document):
	def validate(self):
		self.max_results = min(max(self.max_results or 10, 1), 10)
		self.confirm_minutes = max(self.confirm_minutes or 10, 1)
		self.session_minutes = max(self.session_minutes or 30, 5)
		self.command_prefix = (self.command_prefix or "").strip()
