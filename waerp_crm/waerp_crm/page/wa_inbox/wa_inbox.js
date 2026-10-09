// WhatsApp shared inbox — /app/wa-inbox
const API = "waerp_crm.api.inbox.";
const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
const call = (m, args) => frappe.call({ method: API + m, args }).then((r) => r.message);
const TICKS = { Queued: "🕓", Sent: "✓", Delivered: "✓✓", Read: "✓✓ read", Failed: "⚠ failed", Received: "" };

frappe.pages["wa-inbox"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("WhatsApp Inbox"), single_column: true });
	wrapper.inbox = new WAInbox(page);
};
frappe.pages["wa-inbox"].on_page_show = function (wrapper) {
	const conv = frappe.get_route()[1];
	if (conv && wrapper.inbox) wrapper.inbox.open(conv);
};

class WAInbox {
	constructor(page) {
		this.page = page;
		this.state = { view: "all", status: "Open", search: "", current: null, data: null, note: false };
		this.page.set_primary_action(__("New Chat"), () => this.new_chat(), "add");
		this.page.add_menu_item(__("Templates"), () => frappe.set_route("List", "WA Template"));
		this.page.add_menu_item(__("Flows"), () => frappe.set_route("List", "WA Flow"));
		this.page.add_menu_item(__("Automations"), () => frappe.set_route("List", "WA Automation"));
		this.page.add_menu_item(__("Broadcasts"), () => frappe.set_route("List", "WA Broadcast"));
		this.render_shell();
		this.load_list();
		frappe.realtime.doctype_subscribe("WA Conversation");
		frappe.realtime.on("waerp_crm_event", (e) => this.on_event(e));
		this.start_sync();
	}

	// Background call that never shows dialogs (a failed poll must not interrupt the agent).
	async quiet_call(method, args) {
		const res = await fetch("/api/method/" + API + method, {
			method: "POST",
			headers: { "Content-Type": "application/json", Accept: "application/json",
				"X-Frappe-CSRF-Token": frappe.csrf_token },
			body: JSON.stringify(args || {}),
		});
		if (!res.ok) return null;
		return (await res.json()).message;
	}

	// Live updates normally arrive over Frappe realtime (socket.io). When the socket is not
	// connected (e.g. desk opened through a tunnel/proxy, or a dev bench whose socket port is
	// not reachable) we fall back to a cheap poll every 4 s; with a live socket it is only a
	// 30 s safety net. Re-rendering happens only when the server-side fingerprint changes.
	start_sync() {
		const tick = async () => {
			try {
				const on_page = (frappe.get_route_str() || "").startsWith("wa-inbox");
				const live = frappe.realtime.socket && frappe.realtime.socket.connected;
				const due = !live || Date.now() - (this.last_poll || 0) > 30000;
				if (on_page && !document.hidden && due) {
					this.last_poll = Date.now();
					const conv = this.state.current;
					const s = await this.quiet_call("sync_state", { conversation: conv });
					if (!s) throw new Error("sync unavailable");
					if (this.sig_list !== undefined && s.list !== this.sig_list) this.load_list();
					if (conv && conv === this.state.current && this.sig_thread !== undefined && s.thread !== this.sig_thread) {
						this.open(conv, true);
					}
					this.sig_list = s.list;
					if (conv === this.state.current) this.sig_thread = s.thread;
				}
			} catch (e) {
				// network hiccup: try again on the next tick
			}
			setTimeout(tick, 4000);
		};
		setTimeout(tick, 4000);
	}

	render_shell() {
		const views = [["all", __("All")], ["mine", __("Mine")], ["unassigned", __("Unassigned")],
			["unread", __("Unread")], ["waiting", __("Awaiting reply")]];
		this.$root = $(`
			<div class="wa-inbox">
				<div class="wa-col">
					<div class="wa-list-head">
						<input class="form-control input-sm wa-search" placeholder="${__("Search name, number, text")}">
						<div class="wa-views">${views.map(([k, l]) =>
							`<button class="btn btn-xs btn-default" data-view="${k}">${l}</button>`).join("")}</div>
						<select class="form-control input-sm wa-status">
							${["Open", "Pending", "Resolved", "All"].map((s) => `<option>${s}</option>`).join("")}
						</select>
					</div>
					<div class="wa-list"></div>
				</div>
				<div class="wa-col wa-main"><div class="wa-empty">${__("Select a conversation")}</div></div>
				<div class="wa-col wa-side"></div>
			</div>`).appendTo(this.page.body);
		this.$list = this.$root.find(".wa-list");
		this.$main = this.$root.find(".wa-main");
		this.$side = this.$root.find(".wa-side");
		this.$root.find(`[data-view=all]`).addClass("btn-primary");
		this.$root.on("click", "[data-view]", (e) => {
			this.state.view = $(e.currentTarget).data("view");
			this.$root.find("[data-view]").removeClass("btn-primary");
			$(e.currentTarget).addClass("btn-primary");
			this.load_list();
		});
		this.$root.find(".wa-status").on("change", (e) => { this.state.status = e.target.value; this.load_list(); });
		this.$root.find(".wa-search").on("input", frappe.utils.debounce((e) => {
			this.state.search = e.target.value; this.load_list();
		}, 300));
		this.$list.on("click", ".wa-item", (e) => this.open($(e.currentTarget).data("name")));
	}

	async load_list() {
		const rows = await call("get_conversations", { view: this.state.view, status: this.state.status,
			search: this.state.search });
		this.$list.html(rows.length ? rows.map((r) => `
			<div class="wa-item ${r.name === this.state.current ? "active" : ""}" data-name="${esc(r.name)}">
				<div class="top"><span class="name">${esc(r.contact_name || r.wa_id)}</span>
					<span class="time">${r.last_message_at ? comment_when(r.last_message_at, true) : ""}</span></div>
				<div class="top"><span class="preview">${esc(r.last_message)}</span>
					${r.unread_count ? `<span class="wa-badge">${r.unread_count}</span>` : ""}</div>
				<div class="preview">${r.assigned_to ? "👤 " + esc(frappe.user.full_name(r.assigned_to)) : __("Unassigned")}
					${r.customer ? " · 🏢 " + esc(r.customer) : ""}${r.session_open ? "" : " · ⏱ " + __("window closed")}</div>
			</div>`).join("") : `<div class="wa-empty" style="padding:20px">${__("No conversations")}</div>`);
	}

	async open(name, silent) {
		if (name !== this.state.current) this.sig_thread = undefined;
		this.state.current = name;
		this.$list.find(".wa-item").removeClass("active");
		this.$list.find(`[data-name="${name}"]`).addClass("active");
		this.state.data = await call("get_conversation", { conversation: name });
		this.render_thread();
		this.render_side();
		if (!silent || this.state.data.conversation.unread_count) {
			call("mark_read", { conversation: name }).then(() => this.load_list());
		}
	}

	render_thread() {
		const d = this.state.data, c = d.conversation;
		// keep whatever the agent was typing when the thread refreshes in the background
		const draft = this.rendered_conv === c.name ? this.$main.find("textarea").val() : "";
		const was_at_bottom = !this.$thread || this.rendered_conv !== c.name ||
			this.$thread[0].scrollHeight - this.$thread.scrollTop() - this.$thread.innerHeight() < 80;
		this.rendered_conv = c.name;
		this.$main.html(`
			<div class="wa-thread-head">
				<div><b>${esc(d.contact.full_name || d.contact.wa_id)}</b>
					<span class="text-muted small">+${esc(d.contact.wa_id || "")}</span>
					${d.active_flow ? `<span class="indicator-pill blue">${__("Bot")}: ${esc(d.active_flow.flow)}</span>` : ""}
					${d.human_mode ? `<span class="indicator-pill orange" title="${__("Bots are quiet. Mark Resolved or untick 'Paused' to resume them.")}">👤 ${__("Human mode")}</span>` : ""}</div>
				<div class="btn-group">
					${["Open", "Pending", "Resolved"].map((s) =>
						`<button class="btn btn-xs ${c.status === s ? "btn-primary" : "btn-default"}" data-status="${s}">${__(s)}</button>`).join("")}
				</div>
			</div>
			<div class="wa-thread"></div>
			${d.session_open ? "" : `<div class="wa-window">${__("24-hour window closed — only approved templates can be sent.")}</div>`}
			<div class="wa-composer">
				<textarea class="form-control" placeholder="${d.session_open ? __("Type a message, / for quick replies") : __("Window closed: use Template, or write an internal note")}"></textarea>
				<div class="row-actions">
					<button class="btn btn-primary btn-sm wa-send">${__("Send")}</button>
					<button class="btn btn-default btn-sm wa-note">${__("Note")}</button>
					<button class="btn btn-default btn-sm wa-attach">📎 ${__("File")}</button>
					<button class="btn btn-default btn-sm wa-template">${__("Template")}</button>
					<button class="btn btn-default btn-sm wa-ai">✨ ${__("AI Draft")}</button>
				</div>
			</div>`);
		this.$thread = this.$main.find(".wa-thread");
		this.$thread.html(d.messages.map((m) => this.bubble(m)).join(""));
		if (was_at_bottom) this.scroll();
		const $ta = this.$main.find("textarea");
		if (draft) $ta.val(draft);
		this.$main.find("[data-status]").on("click", (e) =>
			call("set_status", { conversation: c.name, status: $(e.currentTarget).data("status") }).then(() => this.open(c.name)));
		this.$main.find(".wa-send").on("click", () => this.send($ta));
		$ta.on("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.send($ta); } });
		$ta.on("input", () => this.quick_reply($ta));
		this.$main.find(".wa-note").on("click", () => {
			const text = $ta.val().trim();
			if (text) call("note", { conversation: c.name, text }).then(() => { $ta.val(""); this.open(c.name); });
		});
		this.$main.find(".wa-attach").on("click", () => this.attach());
		this.$main.find(".wa-template").on("click", () => this.template_dialog());
		this.$main.find(".wa-ai").on("click", () => frappe.call({ method: "waerp_crm.ai.auto_reply.draft_reply",
			args: { conversation: c.name }, freeze: true, freeze_message: __("Drafting...") })
			.then((r) => $ta.val(r.message || "").focus()));
	}

	bubble(m) {
		const cls = m.is_note ? "note" : m.direction === "Inbound" ? "in" : "out";
		let body = esc(m.content);
		if (m.media_file) {
			const url = esc(m.media_file);
			body = (m.message_type === "image" || m.message_type === "sticker"
				? `<a href="${url}" target="_blank"><img src="${url}"></a>`
				: `<a href="${url}" target="_blank">📄 ${esc(m.filename || m.message_type)}</a>`) + (body ? "<br>" + body : "");
		} else if (!body && m.message_type !== "text") body = `<i>[${esc(m.message_type)}]</i>`;
		const who = m.is_note ? "📝 " + esc(m.sent_by ? frappe.user.full_name(m.sent_by) : __("System"))
			: m.direction === "Outbound" ? esc(m.source === "Agent" ? frappe.user.full_name(m.sent_by) : m.source) : "";
		const ref = m.reference_name ? ` · <a href="/app/${frappe.router.slug(m.reference_doctype)}/${esc(m.reference_name)}">${esc(m.reference_name)}</a>` : "";
		return `<div class="wa-msg ${cls} ${m.status === "Failed" ? "failed" : ""}" title="${esc(m.error_message || "")}">`
			+ `<div class="body">${body}</div>`
			+ `<div class="meta">${who ? who + " · " : ""}${m.is_ai_generated ? "✨AI · " : ""}${frappe.datetime.str_to_user(m.timestamp)}`
			+ `${m.direction === "Outbound" && !m.is_note ? " " + (TICKS[m.status] || "") : ""}${ref}</div>`
			+ `${m.reaction ? `<span class="reaction">${esc(m.reaction)}</span>` : ""}</div>`;
	}

	scroll() { this.$thread && this.$thread.scrollTop(this.$thread[0].scrollHeight); }

	send($ta) {
		const text = $ta.val().trim();
		if (!text) return;
		if (!this.state.data.session_open) return frappe.msgprint(__("The 24-hour window is closed. Use a Template."));
		$ta.val("");
		call("send_text", { conversation: this.state.current, text }).then(() => this.open(this.state.current))
			.catch(() => $ta.val(text));
	}

	async quick_reply($ta) {
		const v = $ta.val();
		if (!v.startsWith("/") || v.includes(" ")) return;
		this.qr = this.qr || (await call("get_quick_replies"));
		const hit = this.qr.find((q) => "/" + q.shortcut === v);
		if (hit) $ta.val(hit.message);
	}

	attach() {
		new frappe.ui.FileUploader({
			doctype: "WA Conversation", docname: this.state.current, make_attachments_public: 0,
			on_success: (file) => {
				frappe.prompt({ fieldname: "caption", fieldtype: "Data", label: __("Caption") }, (v) =>
					call("send_media", { conversation: this.state.current, file_url: file.file_url, caption: v.caption })
						.then(() => this.open(this.state.current)), __("Send file"));
			},
		});
	}

	async template_dialog() {
		const c = this.state.data.conversation;
		const templates = await call("get_templates", { account: c.wa_account });
		if (!templates.length) return frappe.msgprint(__("No APPROVED templates. Sync from WA Account."));
		const d = new frappe.ui.Dialog({
			title: __("Send Template"),
			fields: [
				{ fieldname: "template", fieldtype: "Select", label: __("Template"), reqd: 1,
					options: templates.map((t) => ({ value: t.name, label: `${t.template_name} (${t.language})` })),
					onchange: () => preview() },
				{ fieldname: "preview", fieldtype: "HTML" },
				{ fieldname: "params", fieldtype: "Small Text", label: __("Variables (one per line)") },
				{ fieldname: "header_file", fieldtype: "Attach", label: __("Header media (if required)") },
			],
			primary_action_label: __("Send"),
			primary_action: (v) => call("send_template", { conversation: c.name, template: v.template,
				params: JSON.stringify((v.params || "").split("\n").filter((x) => x.trim())), header_file: v.header_file })
				.then(() => { d.hide(); this.open(c.name); }),
		});
		const preview = () => {
			const t = templates.find((x) => x.name === d.get_value("template"));
			d.fields_dict.preview.$wrapper.html(t ? `<div class="wa-msg out" style="max-width:100%">${esc(t.body)}</div>
				<p class="text-muted small">${__("Needs {0} variable(s)", [t.body_param_count || 0])}${t.header_type !== "NONE" ? " · " + __("Header") + ": " + t.header_type : ""}</p>` : "");
			if (t && !d.get_value("params")) d.set_value("params", Array.from({ length: t.body_param_count || 0 }, (_, i) =>
				i === 0 ? (this.state.data.contact.full_name || "") : "").join("\n"));
		};
		d.show();
		d.set_value("template", templates[0].name);
	}

	async render_side() {
		const d = this.state.data, c = d.conversation, k = d.contact;
		const agents = (await call("get_agents", { account: c.wa_account })) || [];
		if (c.assigned_to && !agents.some((a) => a.name === c.assigned_to)) {
			agents.push({ name: c.assigned_to, full_name: frappe.user.full_name(c.assigned_to) });
		}
		const link = (dt, n) => n ? `<a href="/app/${frappe.router.slug(dt)}/${encodeURIComponent(n)}">${esc(n)}</a>` : "—";
		this.$side.html(`
			<h6>${__("Contact")}</h6>
			<div><b>${esc(k.full_name)}</b><br>+${esc(k.wa_id || "")}<br>
				<span class="text-muted small">${esc(k.opt_status)}</span></div>
			<h6>${__("ERP")}</h6>
			<div class="small">${__("Customer")}: ${link("Customer", k.customer)}<br>${__("Lead")}: ${link("Lead", k.lead)}<br>
				${__("Contact")}: ${link("Contact", k.contact)}<br>${link("WA Contact", k.name)}</div>
			<h6>${__("Assigned To")}</h6>
			<select class="form-control input-sm wa-assign">
				<option value="">${__("Unassigned")}</option><option value="__auto__">${__("Auto (round robin)")}</option>
				${agents.map((a) => `<option value="${esc(a.name)}" ${a.name === c.assigned_to ? "selected" : ""}>${esc(a.full_name || a.name)}</option>`).join("")}
			</select>
			<h6>${__("Tags")}</h6><div>${k.tags.map((t) => `<span class="wa-tag">${esc(t)}</span>`).join("") || "—"}</div>
			${Object.keys(k.attributes).length ? `<h6>${__("Captured")}</h6><div class="small">${Object.entries(k.attributes)
				.map(([a, b]) => `${esc(a)}: <b>${esc(b)}</b>`).join("<br>")}</div>` : ""}
			<h6>${__("AI Bot")}</h6>
			<label class="small"><input type="checkbox" class="wa-ai-pause" ${c.ai_autoreply_disabled ? "checked" : ""}> ${__("Paused on this chat")}</label>
			${c.ai_handoff_summary ? `<div class="small text-muted">${esc(c.ai_handoff_summary)}</div>` : ""}
			<div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
				<button class="btn btn-xs btn-default wa-summary">✨ ${__("Summarize")}</button>
				<button class="btn btn-xs btn-default wa-flow">🤖 ${__("Start Flow")}</button>
			</div>`);
		this.$side.find(".wa-assign").on("change", (e) =>
			call("assign", { conversation: c.name, user: e.target.value }).then(() => this.open(c.name)));
		this.$side.find(".wa-ai-pause").on("change", (e) =>
			call("toggle_ai", { conversation: c.name, paused: e.target.checked ? 1 : 0 }));
		this.$side.find(".wa-summary").on("click", () => frappe.call({ method: "waerp_crm.ai.auto_reply.summarize",
			args: { conversation: c.name }, freeze: true }).then((r) => frappe.msgprint(esc(r.message).replace(/\n/g, "<br>"), __("Summary"))));
		this.$side.find(".wa-flow").on("click", () => frappe.prompt({ fieldname: "flow", fieldtype: "Link", options: "WA Flow",
			label: __("Flow"), reqd: 1, get_query: () => ({ filters: { status: "Active" } }) },
			(v) => call("start_flow", { conversation: c.name, flow: v.flow }).then(() => this.open(c.name)), __("Start Flow")));
	}

	new_chat() {
		frappe.prompt([
			{ fieldname: "phone", fieldtype: "Data", label: __("WhatsApp number (with country code)"), reqd: 1 },
			{ fieldname: "name", fieldtype: "Data", label: __("Name") },
			{ fieldname: "account", fieldtype: "Link", options: "WA Account", label: __("Account") },
		], (v) => call("start_chat", v).then((conv) => { this.open(conv).then(() => this.template_dialog()); this.load_list(); }),
		__("New Chat"));
	}

	on_event(e) {
		this.reload_list = this.reload_list || frappe.utils.debounce(() => this.load_list(), 500);
		this.reload_list();
		if (e.conversation === this.state.current) {
			this.reload_thread = this.reload_thread || frappe.utils.debounce(() => this.open(this.state.current), 400);
			this.reload_thread();
			if (e.inbound && document.hidden) frappe.utils.play_sound("alert");
		} else if (e.inbound) {
			frappe.show_alert({ message: __("New WhatsApp message"), indicator: "green" });
		}
	}
}
