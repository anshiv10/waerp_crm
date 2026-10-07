# Ready-to-use examples

## 1. Main-menu chatbot (WA Flow)

Trigger **Keyword**, keywords `hi`, `hello`, `menu`, match **Whole Word**, entry node `start`.
Typing a flow keyword *exactly* (e.g. `menu`) at any point restarts that flow.

| node_key | node_type | config |
|---|---|---|
| start | start | `{"next_node_key": "welcome"}` |
| welcome | send_message | `{"text": "Hi {{contact.first_name}} 👋 Welcome to ACME.", "next_node_key": "menu"}` |
| menu | send_list | see below |
| ask_order | collect_input | `{"prompt_text": "Please send your order number (e.g. SO-0042)", "var_key": "order_no", "validation": "regex", "pattern": "(?i)[A-Z-]*\\d[\\d-]*", "invalid_text": "That doesn't look like an order number.", "next_node_key": "lookup"}` |
| lookup | erp_lookup | `{"doctype": "Sales Order", "match_field": "name", "value": "{{vars.order_no}}", "restrict_to_customer": true, "customer_field": "customer", "fields": ["status", "grand_total", "delivery_date", "per_delivered"], "found_text": "Order {{doc.name}}: {{doc.status}} — {{doc.per_delivered}}% delivered, expected {{doc.delivery_date}}.", "not_found_text": "I couldn't find that order on your account.", "next_node_key": "more", "not_found_node_key": "more"}` |
| catalogue | send_media | `{"media_url": "/files/catalogue.pdf", "caption": "Our latest catalogue", "filename": "ACME-Catalogue.pdf", "next_node_key": "more"}` |
| ask_qty | collect_input | `{"prompt_text": "What product and quantity do you need?", "var_key": "requirement", "save_to_contact": true, "next_node_key": "lead"}` |
| lead | create_lead | `{"next_node_key": "tag_hot"}` |
| tag_hot | set_tag | `{"tag": "Quote Requested", "next_node_key": "human"}` |
| more | send_buttons | `{"text": "Anything else?", "buttons": [{"reply_id": "m_menu", "title": "Main menu", "next_node_key": "menu"}, {"reply_id": "m_human", "title": "Talk to a person", "next_node_key": "human"}, {"reply_id": "m_done", "title": "No, thanks", "next_node_key": "bye"}]}` |
| human | handoff | `{"text": "Connecting you to our team 🙌", "note": "Bot handoff"}` |
| bye | end | `{"text": "Thank you! Type MENU anytime."}` |

`menu` config:

```json
{"text": "How can we help?", "button_label": "Choose", "sections": [
  {"title": "Orders", "rows": [
    {"reply_id": "r_status", "title": "Order status", "description": "Track an order", "next_node_key": "ask_order"},
    {"reply_id": "r_quote", "title": "Get a quote", "next_node_key": "ask_qty"}]},
  {"title": "Info", "rows": [
    {"reply_id": "r_cat", "title": "Catalogue (PDF)", "next_node_key": "catalogue"},
    {"reply_id": "r_human", "title": "Talk to a person", "next_node_key": "human"}]}]}
```

The flow is checked when you save it. That includes missing or unknown targets, unreachable nodes,
button and row limits, duplicate reply ids, and loops that never wait for the customer. Problems
block activation.

## 2. Automations

**Welcome new contacts, then follow up 1 day later if they're not tagged "Customer"**
Trigger: *New Contact Created*

| step_key | parent | branch | type | config |
|---|---|---|---|---|
| s1 | | | send_message | `{"text": "Thanks for contacting ACME, {{contact.first_name}}!"}` |
| s2 | | | assign_conversation | `{}` (round robin) |
| s3 | | | wait | `{"amount": 1, "unit": "days"}` |
| c1 | | | condition | `{"subject": "tag", "key": "Customer", "operator": "not_exists"}` |
| y1 | c1 | yes | send_template | `{"template": "follow_up-en-Main", "params": ["{{contact.first_name}}"]}` |
| y2 | c1 | yes | add_tag | `{"tag": "Followed Up"}` |
| s4 | | | create_opportunity | `{"opportunity_type": "Sales"}` |

`s4` runs after the condition finishes, even when the wait came first.

**After-hours triage** — Trigger *New Message Received*:
`c1 condition {"subject": "time", "value": "19:00-09:00"}` → yes: `send_message`, `add_tag "After Hours"`.

## 3. ERP notification rules (WA Notification Rule)

| Use case | Document | Send when | Condition | Template variables |
|---|---|---|---|---|
| Invoice with PDF | Sales Invoice | Submit | — | `{{ doc.customer_name }}`, `{{ doc.name }}`, `{{ frappe.utils.fmt_money(doc.grand_total, currency=doc.currency) }}` (template header = DOCUMENT, tick *Attach PDF*) |
| Order confirmation | Sales Order | Submit | — | customer, order no, delivery date |
| Dispatch alert | Delivery Note | Submit | — | `{{ doc.lr_no }}`, `{{ doc.vehicle_no }}` |
| Payment reminder | Sales Invoice | Days After, date field `due_date`, days `3` | `doc.outstanding_amount > 0` | name, invoice, outstanding |
| Pre-due reminder | Sales Invoice | Days After, `due_date`, days `-2` | `doc.outstanding_amount > 0` | … |
| Receipt | Payment Entry | Submit | `doc.payment_type == "Receive"` | party, amount |
| Status change | Sales Order | Value Change, field `status` | `doc.status == "To Bill"` | … |

The *Phone Field* can hold a number (`contact_mobile` on sales documents, `mobile_no` on Customer or Lead). It can also be a link: `contact_person`, `customer`, or `party` on Payment Entry. The app then looks up that Contact's or Customer's mobile number.
