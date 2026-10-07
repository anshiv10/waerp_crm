# Copyright (c) 2026, WAERP CRM and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WAKnowledgeDocument(Document):
	def on_update(self):
		self.db_set("index_status", "Queued")
		frappe.enqueue("waerp_crm.ai.knowledge.index_document", queue="short", name=self.name,
			enqueue_after_commit=True)

	def on_trash(self):
		frappe.db.delete("WA Knowledge Chunk", {"document": self.name})
