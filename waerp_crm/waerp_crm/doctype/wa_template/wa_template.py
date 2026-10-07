# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

from waerp_crm.core.logic import count_placeholders


class WATemplate(Document):
	def validate(self):
		if not self.flags.from_meta and not re.fullmatch(r"[a-z0-9_]+", self.template_name or ""):
			frappe.throw(_("Template name may only contain lowercase letters, numbers and underscores."))
		self.body_param_count = count_placeholders(self.body)
		self.header_param_count = count_placeholders(self.header_text) if self.header_type == "TEXT" else 0
		if not self.status:
			self.status = "DRAFT"

	@frappe.whitelist()
	def submit_to_meta(self):
		from waerp_crm.whatsapp.templates import submit_template

		return submit_template(self.name)
