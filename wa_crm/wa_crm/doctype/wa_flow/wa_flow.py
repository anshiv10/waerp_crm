# Copyright (c) 2026, WA CRM and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from wa_crm.core.logic import parse_json, validate_flow


class WAFlow(Document):
	def validate(self):
		nodes = []
		for n in self.nodes:
			cfg = parse_json(n.config, None) if n.config else {}
			if cfg is None:
				frappe.throw(_("Row {0} ({1}): config is not valid JSON.").format(n.idx, n.node_key))
			nodes.append({"node_key": n.node_key, "node_type": n.node_type, "config": cfg})
		errors = validate_flow(nodes, self.entry_node_key or "start")
		if errors and self.status == "Active":
			frappe.throw("<br>".join(errors), title=_("Fix the flow before activating it"))
		elif errors:
			frappe.msgprint("<br>".join(errors), title=_("Flow has issues (saved as draft)"), indicator="orange")
		if self.status == "Active" and self.trigger_type == "Keyword" and not (self.keywords or "").strip():
			frappe.throw(_("Keyword flows need at least one keyword."))
