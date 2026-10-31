"""Pure parsing for the ERP Assistant (no Frappe imports, unit-tested).

parse_command(text) -> {"intent", "doctype", "filters": [(key, value)], "ref", "updates": [(field, value)]}

Intents: help, confirm, decline, find, show, submit, cancel, amend, update, pdf, free (anything else:
handed to the AI when enabled).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

HELP_WORDS = {"help", "menu", "?", "commands", "erp", "start"}
YES_WORDS = {"yes", "y", "confirm", "confirmed", "ok", "okay", "haan", "ha", "proceed", "go ahead"}
NO_WORDS = {"no", "n", "abort", "nahi", "don't", "dont", "never mind", "nevermind"}
FIND_WORDS = {"find", "search", "list", "get", "fetch"}
SHOW_WORDS = {"show", "open", "view", "details", "detail", "display"}
ACTION_WORDS = {"submit": "submit", "cancel": "cancel", "amend": "amend", "pdf": "pdf", "print": "pdf"}
UPDATE_WORDS = {"set", "update", "change", "edit"}
PRONOUNS = {"", "it", "this", "that", "current", "the document", "this one", "that one", "same"}

_KEY = re.compile(r"(?:^|[,;]|\band\b)\s*([A-Za-z_][A-Za-z0-9_ ]{0,40}?)\s*[:=]", re.I)


def is_ref_like(token: str) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    if re.fullmatch(r"#?\d{1,3}", t):
        return True
    return bool(re.search(r"\d", t) and re.search(r"[-/.]", t) and " " not in t)


def _clean(v: str) -> str:
    v = (v or "").strip().strip(",;").strip()
    v = re.sub(r"\s+and\s*$", "", v, flags=re.I)
    return v.strip().strip("\"'").strip()


def split_pairs(text: str) -> tuple[str, list[tuple[str, str]]]:
    """'sales order customer: Harshit Dixit, date: 29-10-2026' ->
    ('sales order', [('customer', 'Harshit Dixit'), ('date', '29-10-2026')])"""
    text = text or ""
    matches = list(_KEY.finditer(text))
    if not matches:
        return text.strip(), []
    # The doctype phrase precedes the first key; the key regex may have swallowed it, so
    # treat the last word before ':' as the key when the "key" spans several words.
    first = matches[0]
    prefix = text[: first.start(1)]
    pairs = []
    for i, m in enumerate(matches):
        key = m.group(1).strip()
        if i == 0 and " " in key and not prefix.strip():
            words = key.split()
            # keep multi-word labels like "delivery date" only when the first word isn't a doctype word;
            # callers re-try with shorter keys, so split greedily: doctype = all but last 1-2 words.
            prefix, key = " ".join(words[:-1]), words[-1]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pairs.append((key.strip().lower(), _clean(text[m.end(): end])))
    return prefix.strip(), [(k, v) for k, v in pairs if v]


def _strip_pronoun(ref: str) -> str | None:
    r = _clean(ref)
    return None if r.lower() in PRONOUNS else r


def parse_command(text: str) -> dict:
    raw = (text or "").strip()
    low = raw.lower().strip(" .!")
    out = {"intent": "free", "doctype": None, "filters": [], "ref": None, "updates": [], "text": raw}
    if not low or low in HELP_WORDS:
        out["intent"] = "help"
        return out
    if low in YES_WORDS:
        out["intent"] = "confirm"
        return out
    if low in NO_WORDS:
        out["intent"] = "decline"
        return out
    words = raw.split()
    head = words[0].lower()
    rest = raw[len(words[0]):].strip()

    if head in FIND_WORDS and rest:
        dt, pairs = split_pairs(rest)
        out.update(intent="find", doctype=dt or None, filters=pairs)
        return out
    if head in SHOW_WORDS:
        out.update(intent="show", ref=_strip_pronoun(rest))
        return out
    if head in ACTION_WORDS:
        out.update(intent=ACTION_WORDS[head], ref=_strip_pronoun(rest))
        return out
    if head in UPDATE_WORDS and rest:
        m = re.match(r"^(?P<field>.+?)\s+of\s+(?P<ref>\S+)\s+(?:to|=)\s+(?P<val>.+)$", rest, re.I)
        if m:
            out.update(intent="update", ref=_strip_pronoun(m["ref"]), updates=[(m["field"].strip(), _clean(m["val"]))])
            return out
        if "=" in rest or re.search(r"\s+to\s+", rest, re.I):
            left, val = (rest.split("=", 1) if "=" in rest else re.split(r"\s+to\s+", rest, maxsplit=1, flags=re.I))
            lw = left.split()
            ref = None
            if lw and is_ref_like(lw[0]):
                ref, field = lw[0], " ".join(lw[1:])
            else:
                field = left.strip()
            if field.strip():
                out.update(intent="update", ref=_strip_pronoun(ref or ""), updates=[(field.strip(), _clean(val))])
                return out
    if len(words) == 1 and is_ref_like(raw):
        out.update(intent="show", ref=raw.lstrip("#"))
        return out
    return out


# ------------------------------------------------------------------ dates

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_date(value: str, today: date | None = None) -> str | None:
    """Day-first date parsing -> 'YYYY-MM-DD'. Accepts 29-10-2026, 29/10/26, 2026-10-29,
    29 Oct 2026, Oct 29 2026, today / yesterday / tomorrow."""
    today = today or date.today()
    v = (value or "").strip().lower().rstrip(".")
    if not v:
        return None
    rel = {"today": 0, "yesterday": -1, "tomorrow": 1}
    if v in rel:
        return (today + timedelta(days=rel[v])).isoformat()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", v)
    if m:
        y, mo, d = map(int, m.groups())
        return _safe_date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", v)
    if m:
        d, mo, y = map(int, m.groups())
        return _safe_date(y + 2000 if y < 100 else y, mo, d)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[\s-]+([a-z]{3,9})[\s,-]+(\d{2,4})", v)
    if m and m[2][:3] in _MONTHS:
        y = int(m[3])
        return _safe_date(y + 2000 if y < 100 else y, _MONTHS[m[2][:3]], int(m[1]))
    m = re.fullmatch(r"([a-z]{3,9})[\s-]+(\d{1,2})(?:st|nd|rd|th)?[\s,-]+(\d{2,4})", v)
    if m and m[1][:3] in _MONTHS:
        y = int(m[3])
        return _safe_date(y + 2000 if y < 100 else y, _MONTHS[m[1][:3]], int(m[2]))
    return None


def _safe_date(y, mo, d):
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return None


def extract_json(raw: str) -> dict | None:
    """First JSON object in an LLM reply (tolerates ``` fences and chatter)."""
    import json

    s = raw or ""
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start: end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def from_ai(data: dict, text: str = "") -> dict:
    """Normalise the AI's JSON intent into parse_command's shape."""
    intent = str(data.get("intent") or "unknown").lower()
    if intent not in ("find", "show", "submit", "cancel", "amend", "update", "pdf", "help"):
        intent = "unknown"

    def pairs(obj):
        if isinstance(obj, dict):
            return [(str(k).strip().lower(), str(v).strip()) for k, v in obj.items()
                    if v not in (None, "", [], {}) and str(k).strip()]
        return []

    ref = data.get("ref")
    ref = None if ref in (None, "", "null") else str(ref).strip()
    if ref and ref.lower() in PRONOUNS:
        ref = None
    return {"intent": intent, "doctype": (data.get("doctype") or None), "filters": pairs(data.get("filters")),
            "ref": ref.lstrip("#") if ref else None, "updates": pairs(data.get("updates")),
            "reply": data.get("reply"), "text": text}
