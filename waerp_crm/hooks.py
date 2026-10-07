app_name = "waerp_crm"
app_title = "WAERP CRM"
app_publisher = "WAERP CRM"
app_description = "WhatsApp Cloud API CRM for ERPNext: shared inbox, chatbot flows, automations, broadcasts, AI assistant and ERP notifications"
app_email = "dixitanshiv1455@gmail.com"
app_license = "proprietary"
required_apps = ["erpnext"]

before_install = "waerp_crm.install.before_install"
after_install = "waerp_crm.install.after_install"
after_migrate = "waerp_crm.install.ensure_setup"

# "Send via WhatsApp" button on common ERP transactions (plain file, no build step needed)
app_include_js = ["/assets/waerp_crm/js/send_via_whatsapp.js"]

doc_events = {
    "*": {
        "on_submit": "waerp_crm.engines.notifications.on_doc_event",
        "on_cancel": "waerp_crm.engines.notifications.on_doc_event",
        "after_insert": "waerp_crm.engines.notifications.on_doc_event",
        "on_update": "waerp_crm.engines.notifications.on_doc_event",
    },
}

permission_query_conditions = {
    "WA Conversation": "waerp_crm.waerp_crm.doctype.wa_conversation.wa_conversation.get_permission_query_conditions",
}
has_permission = {
    "WA Conversation": "waerp_crm.waerp_crm.doctype.wa_conversation.wa_conversation.has_permission",
}

scheduler_events = {
    "cron": {
        "* * * * *": [
            "waerp_crm.engines.automations.process_pending",
            "waerp_crm.engines.broadcasts.process_scheduled",
        ],
        "*/5 * * * *": [
            "waerp_crm.whatsapp.inbound.requeue_stuck_logs",
        ],
    },
    "hourly": [
        "waerp_crm.engines.flows.expire_stale_runs",
    ],
    "daily": [
        "waerp_crm.whatsapp.templates.sync_all_daily",
        "waerp_crm.engines.notifications.run_date_rules",
        "waerp_crm.install.cleanup_logs",
    ],
}
