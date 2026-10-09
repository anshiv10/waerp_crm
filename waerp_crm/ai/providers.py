"""Bring-your-own-key LLM adapters (Anthropic, OpenAI, Gemini, any OpenAI-compatible API) via plain HTTPS."""

from __future__ import annotations

import requests

TIMEOUT = 45


class AIError(Exception):
    pass


def chat(settings, system: str, turns: list[dict]) -> tuple[str, dict]:
    """-> (text, {'input_tokens', 'output_tokens'})"""
    turns = [t for t in turns if (t.get("content") or "").strip()]
    if not turns or turns[-1]["role"] != "user":
        # Providers expect the conversation to end with the user (Gemini rejects otherwise).
        turns = [*turns, {"role": "user", "content": "(Write the next message from us to the customer.)"}]
    key = settings.get_password("api_key", raise_exception=False)
    if not key and settings.provider != "OpenAI-Compatible":  # local servers (Ollama) need no key
        raise AIError("AI API key is not set in WA AI Settings.")
    key = key or "none"
    if settings.provider == "Anthropic":
        return _anthropic(key, settings, system, turns)
    return _openai(key, settings, system, turns)


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def gemini_base(url: str | None) -> str:
    """Only Google's OpenAI-compatible address works here; a native Gemini URL
    (.../models/x:generateContent, .../v1beta) is replaced by the right one."""
    url = (url or "").strip()
    if not url or ("googleapis.com" in url and "/openai" not in url):
        return GEMINI_BASE
    return url


def _base(s) -> str:
    if s.provider == "OpenAI":
        base = "https://api.openai.com/v1/"
    elif s.provider == "Gemini (Google)":
        base = gemini_base(s.get("base_url"))
    else:  # OpenAI-Compatible (Groq, OpenRouter, Ollama, ...)
        base = (s.get("base_url") or "").strip()
        if not base:
            raise AIError("Set the API Base URL for the OpenAI-compatible provider in WA AI Settings.")
    return base.rstrip("/") + "/"


def _chat_url(s) -> str:
    return _base(s) + "chat/completions"


def model_id(s) -> str:
    """Model as typed, minus stray spaces, quotes and Google's 'models/' prefix."""
    m = (s.model or "").strip().strip("'\"`").strip()
    if m.startswith("models/"):
        m = m[len("models/"):]
    if not m:
        raise AIError("Set the Model in WA AI Settings (use 'Test AI Connection' to see the models your key can use).")
    return m


def list_models(settings) -> list[str]:
    """Model IDs this API key can use, straight from the provider."""
    key = settings.get_password("api_key", raise_exception=False) or "none"
    if settings.provider == "Anthropic":
        r = requests.get("https://api.anthropic.com/v1/models?limit=100", timeout=TIMEOUT,
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        r = requests.get(_base(settings) + "models", timeout=TIMEOUT, headers={"Authorization": f"Bearer {key}"})
    ids = []
    for m in _json(r).get("data") or []:
        mid = (m.get("id") or "").removeprefix("models/")
        if mid:
            ids.append(mid)
    return sorted(set(ids))


def _anthropic(key, s, system, turns):
    body = {"model": model_id(s), "max_tokens": s.max_tokens or 500, "system": system, "messages": turns}
    if s.temperature is not None:
        body["temperature"] = float(s.temperature)
    r = _post("https://api.anthropic.com/v1/messages", json=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    data = _json(r)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return text, {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}


def _openai(key, s, system, turns):
    """OpenAI and every OpenAI-compatible API (Gemini, Groq, OpenRouter, Ollama...)."""
    url = _chat_url(s)
    # OpenAI's newer models want max_completion_tokens; compatible APIs generally accept max_tokens.
    limit_key = "max_completion_tokens" if s.provider == "OpenAI" else "max_tokens"
    body = {"model": model_id(s), "messages": [{"role": "system", "content": system}, *turns],
            limit_key: s.max_tokens or 500}
    if s.temperature not in (None, ""):
        body["temperature"] = float(s.temperature)
    headers = {"Authorization": f"Bearer {key}"}
    r = _post(url, json=body, headers=headers)
    if r.status_code == 400 and "temperature" in r.text:  # some reasoning models reject temperature
        body.pop("temperature", None)
        r = _post(url, json=body, headers=headers)
    if r.status_code == 400 and limit_key in r.text:  # provider wants the other token-limit name
        other = "max_tokens" if limit_key == "max_completion_tokens" else "max_completion_tokens"
        body[other] = body.pop(limit_key)
        r = _post(url, json=body, headers=headers)
    data = _json(r)
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    u = data.get("usage") or {}
    return text, {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}


def embed(settings, texts: list[str]) -> list[list[float]]:
    key = settings.get_password("embeddings_api_key", raise_exception=False)
    if not key:
        raise AIError("No embeddings key configured.")
    out = []
    for i in range(0, len(texts), 96):
        r = requests.post("https://api.openai.com/v1/embeddings", timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": settings.embeddings_model or "text-embedding-3-small", "input": texts[i:i + 96]})
        out.extend(d["embedding"] for d in sorted(_json(r).get("data", []), key=lambda d: d["index"]))
    return out


def _error_message(data) -> str:
    """Error text from any provider's error body: {"error": {...}}, {"error": "..."},
    or Gemini's list form [{"error": {...}}]."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    err = data.get("error", data.get("message"))
    if isinstance(err, dict):
        return str(err.get("message") or err.get("status") or "")
    return str(err or "")


def _json(r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        msg = _error_message(data) or r.text[:300] or r.reason or ""
        if r.status_code in (400, 404) and "model" in msg.lower():
            msg += (" — the Model name is not available for this key. Open WA AI Settings and click "
                    "'Test AI Connection' to pick one from the list.")
        elif r.status_code == 429:
            msg += " — rate limit or quota reached at the AI provider; wait a minute (free tiers allow only a few requests per minute)."
        raise AIError(f"AI provider error {r.status_code}: {msg}")
    return data if isinstance(data, dict) else {}


def _post(url: str, **kwargs) -> requests.Response:
    """POST with one retry after a short pause when the provider is momentarily busy."""
    import time

    r = requests.post(url, timeout=TIMEOUT, **kwargs)
    if r.status_code in (429, 500, 502, 503, 529):
        time.sleep(3)
        r = requests.post(url, timeout=TIMEOUT, **kwargs)
    return r
