frappe.ui.form.on("WA Account", {
	refresh(frm) {
		if (frm.is_new()) return;
		const call = (method, msg) =>
			frm.call(method).then((r) => {
				frappe.show_alert({ message: msg(r.message), indicator: "green" });
				frm.reload_doc();
			});
		frm.add_custom_button(__("Test Connection"), () =>
			call("test_connection", (m) => __("Connected: {0}", [m.verified_name || m.display_phone_number]))
		, __("Meta"));
		frm.add_custom_button(__("Subscribe Webhooks"), () =>
			call("subscribe_webhooks", () => __("App subscribed to this WABA"))
		, __("Meta"));
		frm.add_custom_button(__("Sync Templates"), () =>
			call("sync_templates", (n) => __("{0} templates synced", [n]))
		, __("Meta"));
		frm.set_intro(__("Webhook callback URL: {0}", [`<code>${frm.doc.webhook_url || ""}</code>`]), "blue");
	},
});
