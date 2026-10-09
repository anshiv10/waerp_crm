// Copyright (c) 2026, WAERP CRM and contributors
// For license information, please see license.txt

frappe.ui.form.on("WA AI Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test AI Connection"), () => {
			const run = () =>
				frm.call({ doc: frm.doc, method: "test_connection", freeze: true,
					freeze_message: __("Talking to the AI provider...") }).then((r) => show(frm, r.message || {}));
			frm.is_dirty() ? frm.save().then(run) : run();
		});
	},
	provider(frm) {
		const defaults = {
			"Anthropic": "claude-haiku-4-5-20251001",
			"OpenAI": "gpt-4.1-mini",
			"Gemini (Google)": "gemini-3.5-flash-lite",
		};
		if (defaults[frm.doc.provider]) frm.set_value("model", defaults[frm.doc.provider]);
		if (frm.doc.provider !== "OpenAI-Compatible" && frm.doc.provider !== "Gemini (Google)") frm.set_value("base_url", "");
	},
});

function show(frm, res) {
	const esc = frappe.utils.escape_html;
	let html = "";
	if (res.reply_error) {
		html += `<p class="text-danger"><b>${__("Test call with model")} <code>${esc(frm.doc.model || "")}</code> ${__("failed")}:</b><br>${esc(res.reply_error)}</p>`;
	} else {
		html += `<p class="text-success"><b>✔ ${__("Works!")}</b> ${__("Model")} <code>${esc(frm.doc.model)}</code> ${__("replied")}: “${esc(res.reply || "")}”</p>`;
	}
	if (res.models_error) {
		html += `<p class="text-muted">${__("Could not list models")}: ${esc(res.models_error)}</p>`;
	} else if (res.models.length) {
		html += `<p>${__("Models your key can use (click one to use it):")}</p>
			<input class="form-control input-sm wa-model-filter" placeholder="${__("Filter, e.g. flash")}" style="margin-bottom:6px">
			<div class="wa-model-list" style="max-height:300px;overflow:auto">${res.models
				.map((m) => `<div><a href="#" class="wa-model" data-model="${esc(m)}">${esc(m)}</a></div>`).join("")}</div>`;
	}
	const d = new frappe.ui.Dialog({ title: __("AI Connection Test"), fields: [{ fieldtype: "HTML", fieldname: "out" }] });
	d.fields_dict.out.$wrapper.html(html);
	d.$wrapper.on("input", ".wa-model-filter", (e) => {
		const q = e.target.value.toLowerCase();
		d.$wrapper.find(".wa-model").each((_, a) => $(a).parent().toggle(a.dataset.model.toLowerCase().includes(q)));
	});
	d.$wrapper.on("click", ".wa-model", (e) => {
		e.preventDefault();
		frm.set_value("model", e.currentTarget.dataset.model);
		d.hide();
		frm.save().then(() => frappe.show_alert({ message: __("Model saved. Click Test AI Connection again to confirm."), indicator: "green" }));
	});
	d.show();
}
