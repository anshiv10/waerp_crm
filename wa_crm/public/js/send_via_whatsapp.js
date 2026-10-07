// Adds "Send via WhatsApp" to common ERP forms. Loaded once via hooks.app_include_js.
(() => {
	const DOCTYPES = ["Sales Invoice", "Sales Order", "Quotation", "Delivery Note", "Payment Entry",
		"Purchase Order", "Customer", "Lead", "Opportunity"];
	const PHONE_FIELDS = ["contact_mobile", "mobile_no", "contact_phone", "whatsapp_no", "phone"];
	const handler = {
		refresh(frm) {
			if (frm.is_new() || !frappe.user.has_role(["WA Agent", "WA Manager", "System Manager"])) return;
			frm.add_custom_button(__("Send via WhatsApp"), () => {
				const phone = PHONE_FIELDS.map((f) => frm.doc[f]).find(Boolean) || "";
				const d = new frappe.ui.Dialog({
					title: __("Send via WhatsApp"),
					fields: [
						{ fieldname: "phone", fieldtype: "Data", label: __("Mobile"), reqd: 1, default: phone },
						{ fieldname: "template", fieldtype: "Link", options: "WA Template", label: __("Template"), reqd: 1,
							get_query: () => ({ filters: { status: "APPROVED" } }) },
						{ fieldname: "params", fieldtype: "Small Text", label: __("Variables (one per line)"),
							default: [frm.doc.customer_name || frm.doc.party_name || frm.doc.title || "", frm.doc.name,
								frm.doc.grand_total ? format_currency(frm.doc.grand_total, frm.doc.currency) : ""].join("\n") },
						{ fieldname: "attach_pdf", fieldtype: "Check", label: __("Attach PDF (template must have a DOCUMENT header)"), default: 1 },
						{ fieldname: "print_format", fieldtype: "Link", options: "Print Format", label: __("Print Format"),
							get_query: () => ({ filters: { doc_type: frm.doctype } }) },
					],
					primary_action_label: __("Send"),
					primary_action: (v) => frappe.call({
						method: "wa_crm.engines.notifications.send_document", freeze: true,
						args: { doctype: frm.doctype, name: frm.doc.name, template: v.template, phone: v.phone,
							attach_pdf: v.attach_pdf, print_format: v.print_format,
							params: JSON.stringify((v.params || "").split("\n").filter((x) => x.trim())) },
					}).then((r) => {
						d.hide();
						frappe.show_alert({ message: __("WhatsApp {0}", [r.message.status]), indicator: r.message.status === "Failed" ? "red" : "green" });
					}),
				});
				d.show();
			}, __("Send"));
		},
	};
	DOCTYPES.forEach((dt) => frappe.ui.form.on(dt, handler));
})();
