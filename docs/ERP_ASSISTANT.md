# ERP Assistant on WhatsApp

Staff chat with ERPNext from WhatsApp: search any DocType (standard or custom), see details with
item lines, get the PDF, and Submit / Cancel / Amend / Update — with a Yes/No confirmation for every change.

## Setup
1. **WA ERP Assistant Settings**: tick *Enable ERP Assistant*; keep *Understand Plain Language (AI)* on
   (uses WA AI Settings). Optional *Command Prefix* (e.g. `erp`) when the same number also chats as a customer.
2. **WA ERP User** (one per staff member): ERPNext User, WhatsApp number, what they may do
   (Submit / Cancel / Amend / Update), optional *Allowed DocTypes* (one per line).

## Security model
- Only registered numbers reach the assistant; everyone else gets the normal customer engines.
- Everything runs **as that ERPNext user**: their roles, user permissions and workflow rules apply.
- Changes need a Yes/No confirmation that expires (default 10 min) and belongs to that number only.
- Every change and PDF is recorded in **WA ERP Action Log** (who, what, when, result).
- The AI only receives the message text and document IDs, never document contents.

## Examples
| Message | Result |
|---|---|
| `sales orders of Harshit Dixit on 29-10-2026` | AI → search; 1 hit opens it, several give a pick-list |
| `find sales order customer: Harshit Dixit, date: 29-10-2026` | exact command (no AI needed) |
| `find purchase order supplier: ABC, from: 01-10-2026, to: 31-10-2026` | date range |
| `find work order item: FAB-001, status: draft` | item / status filters |
| `show 2` · `show SAL-ORD-2026-00012` · `show Fabric Lot FL-0001` | details + action buttons |
| `submit it` · `cancel 1` · `amend SAL-ORD-2026-00012` | asks Yes/No, then runs |
| `set delivery date = 05-11-2026` | update (drafts, or "Allow on Submit" fields) |
| `pdf` | the document's PDF (default Print Format and Letter Head) |
