# WA CRM — WhatsApp CRM for ERPNext

A native Frappe/ERPNext app that turns the official **WhatsApp Cloud API** into a full CRM inside ERPNext:
shared team inbox, chatbot flows, no-code automations, template broadcasts, an AI assistant grounded in
your knowledge base **and live ERP data**, plus WhatsApp notifications for any ERPNext document.

Built for selling to many clients: **one Frappe site per client**, any number of WhatsApp numbers per site.

| # | Engine | What it does |
|---|--------|--------------|
| 1 | Webhook ingestion | HMAC-verified, durable (logged before processing), idempotent, race-safe, per-chat ordering lock |
| 2 | Dispatch order | Flows → Automations → Away message → AI → Webhooks; deterministic logic always beats the LLM |
| 3 | Flow engine | Per-contact state machine: buttons, lists, input capture with validation, conditions, ERP lookups, handoff |
| 4 | Automations | Trigger → step tree with yes/no branches and waits that resume correctly (frame stack) |
| 5 | Broadcasts | Audience from Tags / Customer Group / Territory / numbers; throttle, pause, resume, cancel; live stats |
| 6 | AI assistant | Anthropic or OpenAI (client's own key), knowledge base search, `[[HANDOFF]]` to humans, reply caps |

**Extras beyond the reference project:** ERP notification rules (Submit/Cancel/Value-change/“N days after
due date”) with the PDF attached · “Send via WhatsApp” button on Sales Invoice/Order, Quotation, Delivery
Note, Payment Entry… · Customer self-service bot node (`erp_lookup`: “status of SO-0042”) · AI gets the
customer's outstanding + recent orders · round-robin / load-balanced assignment · business hours + away
message · STOP/START opt-out compliance · SLA “awaiting reply” view · internal notes · AI draft & summary for
agents · Lead/Opportunity auto-creation · Click-to-WhatsApp ad source capture · signed outbound webhooks
(n8n/Zapier) · public REST API · agent visibility restriction · template submit-to-Meta.

## Install (bench)

```bash
cd ~/frappe-bench
bench get-app https://github.com/<you>/wa_crm.git      # private repo: use a token URL or SSH
bench --site <site> install-app wa_crm
bench --site <site> migrate
bench restart          # workers + scheduler must be running (they are in production setups)
```

Requires Frappe/ERPNext v15 (v16 compatible by design), Redis, background workers and the scheduler enabled
(`bench --site <site> enable-scheduler`). The site must be reachable over **HTTPS** for Meta's webhook.

## Configure (15 minutes)

1. **Meta side** – follow [docs/META_SETUP.md](docs/META_SETUP.md) (app, WABA, permanent token, webhook).
2. **WA Account** – new record: Phone Number ID, WABA ID, permanent token, App Secret, a Verify Token.
   Click **Meta → Test Connection**, then **Subscribe Webhooks**, then **Sync Templates**.
   Copy the *Webhook Callback URL* shown on the form into Meta.
3. **WA CRM Settings** – country code, lead auto-creation, opt-out words.
4. Give users the **WA Agent** (or **WA Manager**) role. Open **/app/wa-inbox**.
5. Optional: WA AI Settings (provider + key), knowledge documents, flows, automations, notification rules.

Examples for flows and automations: [docs/FLOW_EXAMPLES.md](docs/FLOW_EXAMPLES.md).
How everything fits together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Public API

Frappe API keys (`Authorization: token key:secret`) of a user with WA Agent/Manager:

```
POST /api/method/wa_crm.api.v1.send_text       phone, text
POST /api/method/wa_crm.api.v1.send_template   phone, template, params (JSON list), header_file
GET  /api/method/wa_crm.api.v1.conversations   status
GET  /api/method/wa_crm.api.v1.messages        conversation
POST /api/method/wa_crm.api.v1.start_flow      phone, flow
POST /api/method/wa_crm.api.v1.tag             phone, tag, remove
```

Outbound webhooks (WA Webhook Endpoint) sign the body: `X-WA-CRM-Signature: sha256=<HMAC(secret, body)>`.

## Tests

```bash
python -m unittest wa_crm.tests.test_core_logic -v          # pure logic, no bench needed
bench --site test_site set-config allow_tests true
bench --site test_site run-tests --app wa_crm               # end-to-end engines (Meta mocked)
```
