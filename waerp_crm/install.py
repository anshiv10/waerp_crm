import frappe
from frappe.utils import add_days, nowdate

ROLES = ("WA Manager", "WA Agent")


def before_install():
    _roles()


def after_install():
    ensure_setup()


def ensure_setup():
    """Idempotent; also runs after every `bench migrate`."""
    _roles()
    _fulltext_index()


def _roles():
    for role in ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)


def _fulltext_index():
    if frappe.db.db_type != "mariadb" or not frappe.db.table_exists("WA Knowledge Chunk"):
        return
    exists = frappe.db.sql("show index from `tabWA Knowledge Chunk` where Key_name='wa_kb_content_ft'")
    if not exists:
        frappe.db.sql_ddl("alter table `tabWA Knowledge Chunk` add fulltext index wa_kb_content_ft (content)")


def cleanup_logs():
    days = frappe.db.get_single_value("WA CRM Settings", "webhook_log_retention_days") or 7
    cutoff = add_days(nowdate(), -days)
    frappe.db.delete("WA Webhook Log", {"status": "Processed", "creation": ("<", cutoff)})
    frappe.db.delete("WA Pending Execution", {"status": "Done", "modified": ("<", add_days(nowdate(), -30))})
    frappe.db.commit()
