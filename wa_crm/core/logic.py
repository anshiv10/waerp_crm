"""Pure business logic — NO frappe imports.

Everything here is deterministic and unit-tested in tests/test_core_logic.py
(runs with plain `python -m unittest`, no bench needed). The engines import
these helpers so the tricky rules live in exactly one, tested place.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import datetime, time, timedelta
from typing import Any, Iterable

# ============================================================== phones


def normalize_phone(raw: str | None, default_cc: str = "91") -> str:
    """Return digits-only E.164 without '+', e.g. '+91 98765-43210' -> '919876543210'.

    10-digit local numbers get the default country code; a leading trunk '0'
    on an 11-digit local number is dropped first.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if s.startswith("00"):
        s = s[2:]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and default_cc:
        digits = f"{default_cc}{digits}"
    return digits


def phone_variants(phone: str) -> list[str]:
    """Alternate spellings worth retrying when Meta says 'recipient not allowed'.

    Brazil (55) mobiles exist with and without the extra 9th digit.
    """
    out = [phone]
    if phone.startswith("55") and len(phone) == 13 and phone[4] == "9":
        out.append(phone[:4] + phone[5:])
    elif phone.startswith("55") and len(phone) == 12:
        out.append(phone[:4] + "9" + phone[4:])
    return out


def last_digits(phone: str, n: int = 10) -> str:
    return re.sub(r"\D", "", phone or "")[-n:]


# ============================================================== signatures


def verify_meta_signature(raw_body: bytes, header: str | None, secrets: Iterable[str]) -> bool:
    """Verify Meta's X-Hub-Signature-256 against ANY of the configured app secrets.

    Must be computed over the exact raw bytes Meta sent.
    """
    if not header or not header.startswith("sha256="):
        return False
    supplied = header.split("=", 1)[1].strip()
    for secret in secrets:
        if not secret:
            continue
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, supplied):
            return True
    return False


def sign_body(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ============================================================== status ladders

MESSAGE_LADDER = ["Queued", "Sent", "Delivered", "Read"]
RECIPIENT_LADDER = ["Pending", "Sent", "Delivered", "Read", "Replied"]
META_STATUS_MAP = {"sent": "Sent", "delivered": "Delivered", "read": "Read", "failed": "Failed"}


def is_valid_transition(current: str | None, incoming: str, ladder: list[str]) -> bool:
    """Forward-only along the ladder; 'Failed' allowed only before delivery.

    Webhook replays / out-of-order deliveries must never move a status back.
    """
    if incoming == current:
        return False
    if incoming == "Failed":
        return current in (None, "", ladder[0], ladder[1])
    if current == "Failed":
        return False
    if incoming not in ladder:
        return False
    if current not in ladder:
        return True
    return ladder.index(incoming) > ladder.index(current)


def recipient_counter_columns(old: str | None, new: str) -> list[str]:
    """Cumulative broadcast counters to bump on a recipient transition.

    sent_count counts everything that reached >= Sent, delivered_count >= Delivered...
    So Pending -> Read bumps sent, delivered and read at once.
    """
    if new == "Failed":
        return ["failed_count"]
    cols = {"Sent": "sent_count", "Delivered": "delivered_count", "Read": "read_count",
            "Replied": "replied_count"}
    lo = RECIPIENT_LADDER.index(old) if old in RECIPIENT_LADDER else 0
    hi = RECIPIENT_LADDER.index(new)
    return [cols[s] for s in RECIPIENT_LADDER[lo + 1: hi + 1] if s in cols]


# ============================================================== keyword matching


def matches_keyword(text: str, keywords: Iterable[str], match_type: str = "Whole Word",
                    case_sensitive: bool = False) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    for raw in keywords:
        kw = (raw or "").strip()
        if not kw:
            continue
        if match_type == "Whole Word":
            pattern = r"(?<!\w)" + re.escape(kw) + r"(?!\w)"
            if re.search(pattern, text, 0 if case_sensitive else re.IGNORECASE):
                return True
            continue
        hay, needle = (text, kw) if case_sensitive else (text.lower(), kw.lower())
        if match_type == "Exact" and hay == needle:
            return True
        if match_type == "Contains" and needle in hay:
            return True
    return False


def split_lines(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").splitlines() if x.strip()]


# ============================================================== interpolation

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][\w]*)\.([\w]+)\s*\}\}")


def interpolate(template: str | None, namespaces: dict[str, dict[str, Any]]) -> str:
    """Replace {{ns.key}} using dict namespaces; unknown keys become ''."""
    if not template:
        return ""

    def repl(m):
        ns, key = m.group(1), m.group(2)
        val = (namespaces.get(ns) or {}).get(key)
        return "" if val is None else str(val)

    return _VAR.sub(repl, template)


def parse_json(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ============================================================== comparisons (flows + automations)


def compare(operator: str, actual: Any, expected: Any = None) -> bool:
    """Shared predicate used by flow condition nodes and automation conditions."""
    op = (operator or "equals").lower()
    if op == "exists":
        return actual not in (None, "", [], {})
    if op == "not_exists":
        return actual in (None, "", [], {})
    if actual is None:
        return op == "not_equals"
    a, e = str(actual).strip(), "" if expected is None else str(expected).strip()
    if op == "equals":
        return a.lower() == e.lower()
    if op == "not_equals":
        return a.lower() != e.lower()
    if op == "contains":
        return e.lower() in a.lower()
    if op == "starts_with":
        return a.lower().startswith(e.lower())
    if op in ("gt", "lt", "gte", "lte"):
        try:
            x, y = float(a), float(e)
        except ValueError:
            return False
        return {"gt": x > y, "lt": x < y, "gte": x >= y, "lte": x <= y}[op]
    return False


def in_time_window(now: datetime, window: str) -> bool:
    """'HH:MM-HH:MM' window check; supports overnight ranges like 18:00-09:00."""
    try:
        start_s, end_s = window.split("-")
        start = _to_minutes(start_s)
        end = _to_minutes(end_s)
    except (ValueError, AttributeError):
        return False
    cur = now.hour * 60 + now.minute
    return start <= cur < end if start <= end else (cur >= start or cur < end)


def _to_minutes(s: str) -> int:
    h, m = (s.strip().split(":") + ["0"])[:2]
    return int(h) * 60 + int(m)


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def within_business_hours(now: datetime, rows: list[dict]) -> bool:
    """rows: [{'day': 'Monday', 'from_time': '09:00:00', 'to_time': '18:00:00'}]."""
    today = WEEKDAYS[now.weekday()]
    for row in rows:
        if row.get("day") != today:
            continue
        f = _as_time(row.get("from_time"))
        t = _as_time(row.get("to_time"))
        if f and t and f <= now.time() < t:
            return True
    return False


def _as_time(v: Any) -> time | None:
    if v is None:
        return None
    if isinstance(v, time):
        return v
    if isinstance(v, timedelta):  # MariaDB returns TIME as timedelta
        secs = int(v.total_seconds())
        return time(secs // 3600 % 24, secs % 3600 // 60, secs % 60)
    parts = str(v).split(":")
    try:
        return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0,
                    int(float(parts[2])) if len(parts) > 2 else 0)
    except ValueError:
        return None


def wait_seconds(amount: Any, unit: str) -> int:
    mult = {"minutes": 60, "hours": 3600, "days": 86400}.get((unit or "minutes").lower(), 60)
    try:
        return max(1, int(float(amount) * mult))
    except (TypeError, ValueError):
        return 60


# ============================================================== flow graph

AUTO_ADVANCING = {"start", "send_message", "send_media", "condition", "set_tag", "update_contact",
                  "create_lead", "erp_lookup"}
SUSPENDING = {"send_buttons", "send_list", "collect_input"}
TERMINAL = {"handoff", "end"}
ALL_NODE_TYPES = AUTO_ADVANCING | SUSPENDING | TERMINAL


def node_edges(node_type: str, cfg: dict) -> list[str]:
    """All next_node_key targets a node can jump to."""
    if node_type == "send_buttons":
        return [b.get("next_node_key") for b in cfg.get("buttons", [])]
    if node_type == "send_list":
        return [r.get("next_node_key") for s in cfg.get("sections", []) for r in s.get("rows", [])]
    if node_type == "condition":
        return [cfg.get("true_node_key"), cfg.get("false_node_key")]
    if node_type == "erp_lookup":
        return [cfg.get("next_node_key"), cfg.get("not_found_node_key") or cfg.get("next_node_key")]
    if node_type in TERMINAL:
        return []
    return [cfg.get("next_node_key")]


def validate_flow(nodes: list[dict], entry_key: str) -> list[str]:
    """nodes: [{'node_key', 'node_type', 'config': dict}] -> list of human errors."""
    errors: list[str] = []
    by_key: dict[str, dict] = {}
    for n in nodes:
        key = (n.get("node_key") or "").strip()
        if not key:
            errors.append("Every node needs a Node Key.")
            continue
        if key in by_key:
            errors.append(f"Duplicate node key '{key}'.")
        by_key[key] = n
        if n.get("node_type") not in ALL_NODE_TYPES:
            errors.append(f"Node '{key}': unknown type '{n.get('node_type')}'.")
    if entry_key not in by_key:
        errors.append(f"Entry node '{entry_key}' does not exist.")
        return errors

    for key, n in by_key.items():
        cfg = n.get("config") or {}
        ntype = n.get("node_type")
        if not isinstance(cfg, dict):
            errors.append(f"Node '{key}': config must be a JSON object.")
            continue
        if ntype == "send_buttons":
            buttons = cfg.get("buttons") or []
            if not 1 <= len(buttons) <= 3:
                errors.append(f"Node '{key}': WhatsApp allows 1-3 reply buttons.")
            ids = [b.get("reply_id") for b in buttons]
            if len(set(ids)) != len(ids) or not all(ids):
                errors.append(f"Node '{key}': every button needs a unique reply_id.")
            for b in buttons:
                if len(b.get("title") or "") > 20 or not b.get("title"):
                    errors.append(f"Node '{key}': button titles are required and max 20 characters.")
            if len(cfg.get("text") or "") > 1024 or not cfg.get("text"):
                errors.append(f"Node '{key}': body text is required (max 1024).")
        if ntype == "send_list":
            rows = [r for s in cfg.get("sections", []) for r in s.get("rows", [])]
            if not 1 <= len(rows) <= 10:
                errors.append(f"Node '{key}': lists need 1-10 rows in total.")
            ids = [r.get("reply_id") for r in rows]
            if len(set(ids)) != len(ids) or not all(ids):
                errors.append(f"Node '{key}': every row needs a unique reply_id.")
            for r in rows:
                if len(r.get("title") or "") > 24 or not r.get("title"):
                    errors.append(f"Node '{key}': row titles are required and max 24 characters.")
            if len(cfg.get("button_label") or "") > 20 or not cfg.get("button_label"):
                errors.append(f"Node '{key}': button_label is required (max 20).")
        if ntype == "collect_input" and not cfg.get("var_key"):
            errors.append(f"Node '{key}': collect_input needs var_key.")
        if ntype == "send_media" and not cfg.get("media_url"):
            errors.append(f"Node '{key}': send_media needs media_url.")
        for target in node_edges(ntype, cfg):
            if not target:
                errors.append(f"Node '{key}': a next node is missing.")
            elif target not in by_key:
                errors.append(f"Node '{key}': points to unknown node '{target}'.")

    # Reachability
    seen, stack = set(), [entry_key]
    while stack:
        k = stack.pop()
        if k in seen or k not in by_key:
            continue
        seen.add(k)
        n = by_key[k]
        stack.extend(t for t in node_edges(n.get("node_type"), n.get("config") or {}) if t)
    for k in by_key:
        if k not in seen:
            errors.append(f"Node '{k}' is unreachable from '{entry_key}'.")

    # Infinite loop guard: a cycle made only of auto-advancing nodes never waits
    # for the customer and would spin forever.
    def auto_cycle(start):
        path, visiting = [], set()

        def dfs(k):
            n = by_key.get(k)
            if not n or n.get("node_type") not in AUTO_ADVANCING:
                return False
            if k in visiting:
                return True
            visiting.add(k)
            for t in node_edges(n["node_type"], n.get("config") or {}):
                if t and dfs(t):
                    return True
            visiting.discard(k)
            return False

        return dfs(start)

    for k, n in by_key.items():
        if n.get("node_type") in AUTO_ADVANCING and auto_cycle(k):
            errors.append(f"Node '{k}' is in a loop that never waits for the customer.")
            break
    return list(dict.fromkeys(errors))


def match_reply(node_type: str, cfg: dict, reply_id: str | None, text: str | None) -> str | None:
    """Resolve a customer reply on a suspended buttons/list node to the next node key.

    Accepts the tapped reply_id, OR a typed answer equal to the option title,
    OR the option's 1-based number ("2") — customers often type instead of tap.
    """
    if node_type == "send_buttons":
        options = cfg.get("buttons", [])
    elif node_type == "send_list":
        options = [r for s in cfg.get("sections", []) for r in s.get("rows", [])]
    else:
        return None
    if reply_id:
        for o in options:
            if o.get("reply_id") == reply_id:
                return o.get("next_node_key")
    t = (text or "").strip().lower()
    if not t:
        return None
    for i, o in enumerate(options, start=1):
        if t == (o.get("title") or "").strip().lower() or t == str(i):
            return o.get("next_node_key")
    return None


def decide_fallback(action: str, reprompt_count: int, max_reprompts: int) -> str:
    """-> 'reprompt' | 'handoff' | 'end' | 'ignore'. reprompt_count already incremented."""
    if action == "Ignore":
        return "ignore"
    if action == "Handoff":
        return "handoff"
    if reprompt_count <= max(0, int(max_reprompts or 0)):
        return "reprompt"
    return "end" if action == "Reprompt Then End" else "handoff"


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_input(value: str, rule: str | None, pattern: str | None = None) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    rule = (rule or "any").lower()
    if rule == "number":
        try:
            float(v.replace(",", ""))
            return True
        except ValueError:
            return False
    if rule == "email":
        return bool(_EMAIL.match(v))
    if rule == "phone":
        return 8 <= len(re.sub(r"\D", "", v)) <= 15
    if rule == "regex" and pattern:
        try:
            return bool(re.fullmatch(pattern, v))
        except re.error:
            return False
    return True


# ============================================================== automation step tree


def scope_steps(steps: list[dict], parent_key: str | None, branch: str | None) -> list[dict]:
    """Ordered steps in one scope. Top level: parent blank. Branch: parent + yes/no."""
    out = []
    for s in steps:
        p = (s.get("parent_step_key") or "").strip() or None
        if parent_key is None:
            if p is None:
                out.append(s)
        elif p == parent_key and (s.get("branch") or "yes") == (branch or "yes"):
            out.append(s)
    return sorted(out, key=lambda s: s.get("idx") or 0)


# ============================================================== templates


def count_placeholders(text: str | None) -> int:
    nums = [int(n) for n in re.findall(r"\{\{\s*(\d+)\s*\}\}", text or "")]
    return max(nums) if nums else 0


def render_template_preview(body: str, params: list[str]) -> str:
    def repl(m):
        i = int(m.group(1)) - 1
        return params[i] if 0 <= i < len(params) else m.group(0)

    return re.sub(r"\{\{\s*(\d+)\s*\}\}", repl, body or "")


def build_template_components(header_type: str, body_params: list[str],
                              header_param: str | None = None, header_media: dict | None = None,
                              button_params: list[dict] | None = None) -> list[dict]:
    """Meta 'components' array for sending a template.

    header_media: {'type': 'image'|'video'|'document', 'link' or 'id', 'filename'?}
    button_params: [{'index': 0, 'sub_type': 'url', 'text': 'abc'}]
    """
    comps: list[dict] = []
    ht = (header_type or "NONE").upper()
    if ht == "TEXT" and header_param:
        comps.append({"type": "header", "parameters": [{"type": "text", "text": header_param}]})
    elif ht in ("IMAGE", "VIDEO", "DOCUMENT") and header_media:
        kind = ht.lower()
        media = {k: v for k, v in header_media.items() if k in ("link", "id", "filename") and v}
        if kind != "document":
            media.pop("filename", None)
        comps.append({"type": "header", "parameters": [{"type": kind, kind: media}]})
    if body_params:
        comps.append({"type": "body", "parameters": [
            {"type": "text", "text": (str(p) if p not in (None, "") else "-")[:1024]} for p in body_params
        ]})
    for b in button_params or []:
        comps.append({"type": "button", "sub_type": b.get("sub_type", "url"), "index": str(b.get("index", 0)),
                      "parameters": [{"type": "text", "text": str(b.get("text", ""))}]})
    return comps


# ============================================================== AI helpers

HANDOFF_SENTINEL = "[[HANDOFF]]"


def parse_ai_output(raw: str) -> tuple[str, bool]:
    raw = raw or ""
    handoff = HANDOFF_SENTINEL in raw
    return raw.replace(HANDOFF_SENTINEL, "").strip(), handoff


def to_chat_turns(history: list[dict]) -> list[dict]:
    """[{'direction','content'}] -> alternating user/assistant turns starting with user."""
    turns: list[dict] = []
    for m in history:
        role = "user" if m.get("direction") == "Inbound" else "assistant"
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Paragraph-aware chunking with overlap for the knowledge base."""
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:
            head = p[:size]
            cut = max(head.rfind(". "), head.rfind("\n"))
            cut = cut + 1 if cut > size // 2 else size
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut].strip())
            p = p[max(0, cut - overlap):].strip()
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            tail = cur[-overlap:] if cur and overlap else ""
            cur = f"{tail}\n\n{p}".strip() if tail else p
    if cur:
        chunks.append(cur)
    return chunks


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_WORD = re.compile(r"\w{3,}", re.UNICODE)


def lexical_score(query: str, text: str) -> float:
    q = {w.lower() for w in _WORD.findall(query or "")}
    if not q:
        return 0.0
    t = [w.lower() for w in _WORD.findall(text or "")]
    if not t:
        return 0.0
    tset = set(t)
    hits = sum(1 for w in q if w in tset)
    return hits / len(q)


def handoff_summary(history: list[dict], limit: int = 6) -> str:
    lines = []
    for m in history[-limit:]:
        who = "Customer" if m.get("direction") == "Inbound" else "Bot"
        txt = (m.get("content") or "").strip().replace("\n", " ")
        if txt:
            lines.append(f"{who}: {txt[:160]}")
    return "AI handed off. Recent conversation:\n" + "\n".join(lines)
