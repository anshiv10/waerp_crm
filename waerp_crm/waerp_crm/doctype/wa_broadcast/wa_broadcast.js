frappe.ui.form.on("WA Broadcast", {
	refresh(frm) {
		if (frm.is_new()) return;
		const act = (label, method, confirm) =>
			frm.add_custom_button(label, () => {
				const go = () => frappe.call({
					method: "waerp_crm.waerp_crm.doctype.wa_broadcast.wa_broadcast.run_action",
					args: { name: frm.docname, action: method },
					freeze: true,
				}).then(() => frm.reload_doc());
				confirm ? frappe.confirm(confirm, go) : go();
			});
		const s = frm.doc.status;
		if (["Draft"].includes(s)) act(__("Send / Schedule"), "start", __("Send this broadcast now (or at the scheduled time)?"));
		if (s === "Sending") act(__("Pause"), "pause");
		if (["Paused", "Failed"].includes(s)) act(__("Resume"), "resume");
		if (["Draft", "Scheduled", "Queued", "Sending", "Paused"].includes(s)) act(__("Cancel"), "cancel_broadcast", __("Cancel this broadcast?"));
		if (!["Draft"].includes(s)) {
			act(__("Recompute Stats"), "recompute");
			frm.add_custom_button(__("Recipients"), () =>
				frappe.set_route("List", "WA Broadcast Recipient", { broadcast: frm.doc.name })
			);
		}
		clearTimeout(frm._wa_poll);
		if (["Scheduled", "Queued", "Sending"].includes(s)) {
			frm._wa_poll = setTimeout(() => {
				if (cur_frm === frm && !frm.is_dirty()) frm.reload_doc();
			}, 5000);
		}
	},
});
