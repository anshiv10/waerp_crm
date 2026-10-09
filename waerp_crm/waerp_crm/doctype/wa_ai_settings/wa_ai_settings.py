# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WAAISettings(Document):
	def validate(self):
		from waerp_crm.ai.providers import model_id

		if self.model:
			self.model = model_id(self)
		if self.base_url:
			self.base_url = self.base_url.strip()

	@frappe.whitelist()
	def test_connection(self):
		"""Lists the models this key can use and makes one tiny call with the saved Model."""
		frappe.only_for(("System Manager", "WA Manager"))
		from waerp_crm.ai.providers import chat, list_models

		out = {"models": [], "models_error": None, "reply": None, "reply_error": None}
		try:
			out["models"] = list_models(self)
		except Exception as e:
			out["models_error"] = str(e)[:500]
		if self.model:
			try:
				out["reply"] = chat(self, "Reply with exactly: OK",
				                    [{"role": "user", "content": "Connection test"}])[0].strip()[:200]
			except Exception as e:
				out["reply_error"] = str(e)[:500]
		else:
			out["reply_error"] = _("No Model set yet: pick one from the list.")
		return out
