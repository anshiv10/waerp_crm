# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WAERPUser(Document):
	def validate(self):
		from waerp_crm.utils import norm

		self.phone = norm(self.phone)
		if not self.phone:
			frappe.throw(_("Enter a valid WhatsApp number with country code."))
		lines = [x.strip() for x in (self.allowed_doctypes or "").splitlines() if x.strip()]
		for dt in lines:
			if not frappe.db.exists("DocType", dt):
				frappe.throw(_("Allowed DocTypes: '{0}' is not a DocType.").format(dt))
		self.allowed_doctypes = "\n".join(lines)
