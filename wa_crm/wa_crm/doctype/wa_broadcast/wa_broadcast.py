# Copyright (c) 2026, WA CRM and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


class WABroadcast(Document):
	def validate(self):
		if self.template and frappe.db.get_value("WA Template", self.template, "wa_account") != self.wa_account:
			frappe.throw(_("Pick a template of the same WA Account."))

	def _check(self):
		frappe.only_for(("System Manager", "WA Manager"))

	@frappe.whitelist()
	def start(self):
		self._check()
		from wa_crm.engines.broadcasts import queue_broadcast

		if self.scheduled_at and get_datetime(self.scheduled_at) > now_datetime():
			self.db_set("status", "Scheduled")
			return {"scheduled": True}
		return {"recipients": queue_broadcast(self.name)}

	@frappe.whitelist()
	def pause(self):
		self._check()
		if self.status == "Sending":
			self.db_set("status", "Paused")

	@frappe.whitelist()
	def resume(self):
		self._check()
		if self.status in ("Paused", "Failed") and frappe.db.exists(
			"WA Broadcast Recipient", {"broadcast": self.name, "status": "Pending"}
		):
			self.db_set("status", "Queued")
			frappe.enqueue("wa_crm.engines.broadcasts.deliver", queue="long", timeout=6 * 3600,
				broadcast=self.name, job_id=f"wa_broadcast::{self.name}", deduplicate=True)

	@frappe.whitelist()
	def cancel_broadcast(self):
		self._check()
		if self.status in ("Draft", "Scheduled", "Queued", "Sending", "Paused"):
			self.db_set("status", "Cancelled")

	@frappe.whitelist()
	def recompute(self):
		self._check()
		from wa_crm.engines.broadcasts import recompute_counts

		recompute_counts(self.name)
