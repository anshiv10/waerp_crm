"""Knowledge base: chunk + (optional) embed on save; hybrid retrieval at answer time.

Semantic search (cosine over stored embeddings) when an embeddings key is set,
topped up with lexical search (MariaDB FULLTEXT when available, else a portable
word-overlap score). Re-indexing is idempotent: a document's chunks are replaced.
"""

from __future__ import annotations

import json

import frappe

from waerp_crm.core.logic import chunk_text, cosine, lexical_score


SMALL_KB_CHARS = 8000  # ~2,000 tokens


def index_document(name: str):
    doc = frappe.get_doc("WA Knowledge Document", name)
    frappe.db.delete("WA Knowledge Chunk", {"document": name})
    if not doc.enabled:
        doc.db_set({"chunk_count": 0, "index_status": "Disabled"})
        return
    chunks = chunk_text(doc.content or "")
    settings = frappe.get_cached_doc("WA AI Settings")
    vectors, status = None, "Indexed (keyword search)"
    if settings.get_password("embeddings_api_key", raise_exception=False) and chunks:
        try:
            from waerp_crm.ai.providers import embed
            vectors = embed(settings, chunks)
            status = "Indexed (semantic + keyword)"
        except Exception as e:
            status = f"Keyword only — embedding failed: {str(e)[:100]}"
    for i, text in enumerate(chunks):
        frappe.get_doc({"doctype": "WA Knowledge Chunk", "document": name, "chunk_index": i, "content": text,
                        "embedding": json.dumps(vectors[i]) if vectors else None}).insert(ignore_permissions=True)
    doc.db_set({"chunk_count": len(chunks), "index_status": status})
    frappe.cache.delete_value("waerp_crm:kb_vectors")


def _enabled_chunks() -> list[dict]:
    return frappe.db.sql(
        """select c.name, c.content, c.embedding from `tabWA Knowledge Chunk` c
           join `tabWA Knowledge Document` d on d.name = c.document where d.enabled = 1""", as_dict=True)


def retrieve(query: str, k: int = 5) -> list[str]:
    if not (query or "").strip():
        return []
    settings = frappe.get_cached_doc("WA AI Settings")
    chunks = _enabled_chunks()
    if not chunks:
        return []
    if sum(len(c.content or "") for c in chunks) <= SMALL_KB_CHARS:
        # A small knowledge base fits in the prompt: give the AI all of it rather than
        # risk a search missing the one relevant line (common with short FAQs).
        return [c.content for c in chunks]
    scored: dict[str, float] = {}
    if settings.get_password("embeddings_api_key", raise_exception=False) and any(c.embedding for c in chunks):
        try:
            from waerp_crm.ai.providers import embed
            qv = embed(settings, [query])[0]
            for c in chunks:
                if c.embedding:
                    scored[c.name] = cosine(qv, json.loads(c.embedding))
        except Exception:
            frappe.log_error(title="WAERP CRM: query embedding failed")
    lexical = _fulltext(query, k * 2)
    if not lexical:  # no FULLTEXT index (Postgres) or no hits (tiny KBs, stop-words) -> portable scoring
        lexical = {c.name: lexical_score(query, c.content) for c in chunks}
    for name, s in lexical.items():
        scored[name] = max(scored.get(name, 0.0), s * 0.9)
    by_name = {c.name: c.content for c in chunks}
    best = sorted((n for n, s in scored.items() if s > 0.15), key=lambda n: -scored[n])[:k]
    return [by_name[n] for n in best if n in by_name]


def _fulltext(query: str, limit: int) -> dict[str, float] | None:
    if frappe.db.db_type != "mariadb":
        return None
    try:
        rows = frappe.db.sql(
            """select name, match(content) against (%(q)s in natural language mode) as score
               from `tabWA Knowledge Chunk` where match(content) against (%(q)s in natural language mode)
               order by score desc limit %(limit)s""", {"q": query, "limit": limit}, as_dict=True)
    except Exception:
        return None
    if not rows:
        return {}
    top = float(rows[0].score) or 1.0
    return {r.name: float(r.score) / top for r in rows}


def erp_context(wa_contact: str) -> str:
    """Short, factual customer context from ERPNext for grounded answers."""
    customer = frappe.db.get_value("WA Contact", wa_contact, "customer")
    if not customer:
        return ""
    lines = [f"Customer: {frappe.db.get_value('Customer', customer, 'customer_name')}"]
    outstanding = frappe.db.sql("""select sum(outstanding_amount) from `tabSales Invoice`
                                   where customer=%s and docstatus=1""", customer)[0][0] or 0
    lines.append(f"Total outstanding: {frappe.utils.fmt_money(outstanding)}")
    for so in frappe.get_all("Sales Order", filters={"customer": customer, "docstatus": 1},
                             fields=["name", "status", "grand_total", "delivery_date", "per_delivered"],
                             order_by="transaction_date desc", limit=3):
        lines.append(f"Order {so.name}: {so.status}, total {frappe.utils.fmt_money(so.grand_total)}, "
                     f"{int(so.per_delivered or 0)}% delivered, expected {so.delivery_date}")
    return "\n".join(lines)
