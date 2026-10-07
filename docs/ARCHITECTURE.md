# Architecture

```
Meta Cloud API ──POST──▶ /api/method/wa_crm.api.webhook.receive
                          │ 1. HMAC X-Hub-Signature-256 over RAW body (any account's App Secret)
                          │ 2. insert WA Webhook Log (durable inbox)  3. enqueue  4. 200 OK in ms
                          ▼
              RQ worker (queue: short) ── whatsapp/inbound.py
                          │ statuses  → forward-only ladder → WA Message / WA Broadcast Recipient
                          │ templates → WA Template status / quality / category
                          │ messages  → WA Contact (wa_id | BSUID, ERP Contact/Customer match, Lead)
                          │             WA Conversation (name = account-contact ⇒ race-safe)
                          │             Redis lock per conversation (ordering)
                          │             UNIQUE dedupe_key  ⇒ Meta retries are no-ops
                          │             atomic SQL: unread+1, 24h window, reopen
                          │             STOP / START
                          ▼
                 engines/dispatch.py  (each stage isolated with try/except)
                   1 Flows ──consumed?──┐
                   2 Automations        │ content triggers skipped if consumed
                   3 Away message       │ only if no AI bot
                   4 AI auto-reply      │ only if nothing above replied
                   5 Outbound webhooks  ┘
                          │
                          ▼
              whatsapp/outbound.py — the ONLY send path
                24h rule · opt-out (marketing) · WA Message log · Graph API · realtime → inbox
```

## Data model (30 DocTypes, module "WA CRM")

| Area | DocTypes |
|---|---|
| Setup | WA CRM Settings (single), WA Account (+ Agents, Business Hours), WA Webhook Log |
| People | WA Contact (+ tags), WA Tag |
| Messaging | WA Conversation, WA Message, WA Template, WA Quick Reply |
| Bots | WA Flow (+ Nodes), WA Flow Run (+ Events) |
| Automation | WA Automation (+ Steps), WA Automation Log, WA Pending Execution |
| Campaigns | WA Broadcast (+ Params), WA Broadcast Recipient |
| AI | WA AI Settings (single), WA Knowledge Document, WA Knowledge Chunk, WA AI Usage Log |
| Integrations | WA Webhook Endpoint, WA Notification Rule (+ Params) |

## Reliability guarantees

| Problem | How it's handled |
|---|---|
| Meta retries a slow ack | Ack after one insert. Processing runs in a worker. `dedupe_key` is UNIQUE. |
| A worker crashes mid-payload | The log stays `Queued`. The scheduler re-enqueues it after 5 minutes (max 5 attempts). |
| Two messages from the same chat at once | A Redis lock per conversation processes them one at a time. Messages inside one webhook batch are sorted by timestamp. |
| Two new messages from a new number at once | Unique `wa_id` plus a deterministic conversation name. The loser re-reads. |
| Counters drift | Every counter is an SQL `x = x + 1`. Broadcast stats can be rebuilt with *Recompute Stats*. |
| Late or out-of-order status webhooks | Statuses only move forward. `Failed` is accepted only before delivery. |
| AI over-replies | The reply slot is claimed with a conditional UPDATE. A per-minute account cap protects the client's key. |
| A bad flow graph | Validation runs on save. The runtime stops after `MAX_STEPS`. Every step is logged on the run. |
| A wait inside a condition branch | A frame stack is stored with the pending execution, so the outer steps still run afterwards. |
| SSRF through webhooks | Only public IPs are allowed. No redirects. |
| A partly processed message is retried | Bot side effects are at-most-once: a retried message is skipped, so the customer is never double-texted. |

## Scaling notes (production)

- Webhooks and bots run on the `short` queue, broadcasts on `long`. Size the workers to match. A busy
  site might use `workers: short x4, long x2` in the Procfile or supervisor config.
- A broadcast commits after every recipient, so pause, resume and live stats work at any size.
  Meta's own throughput is the ceiling. Use *Max Messages per Minute* to stay under a new number's limits.
- Flow runs and automation logs grow over time. Archive them with Frappe's *Log Settings* or a
  periodic cleanup.

## Multi-client (selling) model

- Use **one site per client**. That gives full data isolation, and the standard bench and Frappe Cloud
  model fits it. A site can hold many WA Accounts (numbers), for example one per company or brand.
- Each client owns their Meta app/WABA and AI key. Your product carries no per-message cost.

## Roadmap (next phases)

1. **Visual flow builder** (drag-and-drop canvas that writes the same node JSON) and automation tree editor.
2. **Embedded Signup**. Become a Meta *Tech Provider* so clients can connect their number in a few clicks,
   with no manual token copying. This is the biggest onboarding win when selling.
3. **WhatsApp Flows (forms)**, **catalog / product messages**, and **payment links** (Razorpay/UPI) inside chats.
4. **Analytics**: response time, SLA breaches, agent leaderboard, broadcast ROI (reply and conversion to a
   Sales Order), and Click-to-WhatsApp ad attribution.
5. **MCP server / AI agent tools**: let the AI create a quotation or check stock with approval.
6. **CSAT survey** after a conversation is resolved, voice-note transcription, and multilingual auto-translate.
7. **Licensing module**: per-site license key, plan limits, and usage metering for your business.

## Business notes before selling

- **Naming**: Meta's brand rules restrict using "WhatsApp" in a product name. Sell under your own brand
  (for example "<Brand> Chat for ERPNext") and describe it as *for WhatsApp Business Platform*.
- **License**: ERPNext is GPLv3 and Frappe is MIT. An app that imports ERPNext may count as a derivative
  work when you distribute it. Get legal advice on whether to sell it as GPL plus paid services and
  hosting (the common model in the Frappe ecosystem) or as proprietary. The current `license.txt` is a
  placeholder.
- **Meta pricing** is charged by Meta to each client per template message, by category (marketing,
  utility, authentication). Service replies inside the 24-hour window are free. Check Meta's current
  pricing page before you quote clients.
