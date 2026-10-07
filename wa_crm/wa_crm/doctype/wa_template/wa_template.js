frappe.ui.form.on("WA Template", {
	refresh(frm) {
		if (!frm.is_new() && ["DRAFT", "REJECTED"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Submit to Meta for Approval"), () =>
				frm.call("submit_to_meta").then(() => frm.reload_doc())
			);
		}
	},
});
