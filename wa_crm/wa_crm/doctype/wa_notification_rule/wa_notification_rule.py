# Copyright (c) 2026, WA CRM and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WANotificationRule(Document):
	def validate(self):
		meta = frappe.get_meta(self.document_type)
		if not meta.has_field(self.phone_field):
			frappe.throw(_("{0} has no field {1}.").format(self.document_type, self.phone_field))
		if self.event in ("Submit", "Cancel") and not meta.is_submittable:
			frappe.throw(_("{0} is not submittable.").format(self.document_type))
		if self.event == "Days After" and not meta.has_field(self.date_field):
			frappe.throw(_("{0} has no field {1}.").format(self.document_type, self.date_field))
		if self.condition:
			try:
				compile(self.condition, "<condition>", "eval")
			except SyntaxError as e:
				frappe.throw(_("Condition is not a valid expression: {0}").format(e))

	def on_update(self):
		from wa_crm.engines.notifications import clear_cache

		clear_cache()

	def on_trash(self):
		from wa_crm.engines.notifications import clear_cache

		clear_cache()
