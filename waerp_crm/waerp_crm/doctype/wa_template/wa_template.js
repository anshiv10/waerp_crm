frappe.ui.form.on("WA Template", {
	refresh(frm) {
		if (!frm.is_new() && ["DRAFT", "REJECTED"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Submit to Meta for Approval"), () =>
				frm.call({ method: "submit_to_meta", doc: frm.doc, freeze: true,
					freeze_message: __("Sending to Meta...") }).then(() => {
					frappe.show_alert({ message: __("Submitted. Meta usually reviews within minutes."), indicator: "green" });
					frm.reload_doc();
				})
			);
		}
		if (!frm.is_new() && frm.doc.wa_account) {
			frm.add_custom_button(__("Refresh Status from Meta"), () =>
				frappe.call({ method: "waerp_crm.whatsapp.templates.sync_templates", args: { account: frm.doc.wa_account },
					freeze: true }).then(() => frm.reload_doc())
			);
		}
	},
});
