# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from waerp_crm.core.logic import parse_json


class WAAutomation(Document):
	def validate(self):
		keys = {}
		for s in self.steps:
			if s.config and parse_json(s.config, None) is None:
				frappe.throw(_("Step row {0}: config is not valid JSON.").format(s.idx))
			if s.step_key in keys:
				frappe.throw(_("Duplicate step key {0}.").format(s.step_key))
			keys[s.step_key] = s.step_type
		for s in self.steps:
			if s.parent_step_key:
				if keys.get(s.parent_step_key) != "condition":
					frappe.throw(_("Step {0}: parent {1} must be a condition step.").format(s.step_key, s.parent_step_key))
				if s.branch not in ("yes", "no"):
					frappe.throw(_("Step {0}: choose branch yes or no.").format(s.step_key))
		if self.trigger_type == "Keyword Match" and self.is_active and not (self.keywords or "").strip():
			frappe.throw(_("Add at least one keyword."))
		if self.trigger_type == "Tag Added" and not self.tag:
			frappe.throw(_("Choose the tag."))
