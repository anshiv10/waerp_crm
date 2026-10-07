app_name = "wa_crm"
app_title = "WA CRM"
app_publisher = "WA CRM"
app_description = "WhatsApp Cloud API CRM for ERPNext: shared inbox, chatbot flows, automations, broadcasts, AI assistant and ERP notifications"
app_email = "dixitanshiv1455@gmail.com"
app_license = "proprietary"
required_apps = ["erpnext"]

before_install = "wa_crm.install.before_install"
after_install = "wa_crm.install.after_install"
after_migrate = "wa_crm.install.ensure_setup"

# "Send via WhatsApp" button on common ERP transactions (plain file, no build step needed)
app_include_js = ["/assets/wa_crm/js/send_via_whatsapp.js"]

doc_events = {
    "*": {
        "on_submit": "wa_crm.engines.notifications.on_doc_event",
        "on_cancel": "wa_crm.engines.notifications.on_doc_event",
        "after_insert": "wa_crm.engines.notifications.on_doc_event",
        "on_update": "wa_crm.engines.notifications.on_doc_event",
    },
}

permission_query_conditions = {
    "WA Conversation": "wa_crm.wa_crm.doctype.wa_conversation.wa_conversation.get_permission_query_conditions",
}
has_permission = {
    "WA Conversation": "wa_crm.wa_crm.doctype.wa_conversation.wa_conversation.has_permission",
}

scheduler_events = {
    "cron": {
        "* * * * *": [
            "wa_crm.engines.automations.process_pending",
            "wa_crm.engines.broadcasts.process_scheduled",
        ],
        "*/5 * * * *": [
            "wa_crm.whatsapp.inbound.requeue_stuck_logs",
        ],
    },
    "hourly": [
        "wa_crm.engines.flows.expire_stale_runs",
    ],
    "daily": [
        "wa_crm.whatsapp.templates.sync_all_daily",
        "wa_crm.engines.notifications.run_date_rules",
        "wa_crm.install.cleanup_logs",
    ],
}
