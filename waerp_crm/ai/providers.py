"""Bring-your-own-key LLM adapters (Anthropic / OpenAI) via plain HTTPS."""

from __future__ import annotations

import requests

TIMEOUT = 45


class AIError(Exception):
    pass


def chat(settings, system: str, turns: list[dict]) -> tuple[str, dict]:
    """-> (text, {'input_tokens', 'output_tokens'})"""
    key = settings.get_password("api_key", raise_exception=False)
    if not key:
        raise AIError("AI API key is not set in WA AI Settings.")
    if settings.provider == "OpenAI":
        return _openai(key, settings, system, turns)
    return _anthropic(key, settings, system, turns)


def _anthropic(key, s, system, turns):
    body = {"model": s.model, "max_tokens": s.max_tokens or 500, "system": system, "messages": turns}
    if s.temperature is not None:
        body["temperature"] = float(s.temperature)
    r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=TIMEOUT, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    data = _json(r)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return text, {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}


def _openai(key, s, system, turns):
    body = {"model": s.model, "messages": [{"role": "system", "content": system}, *turns],
            "max_completion_tokens": s.max_tokens or 500}
    if s.temperature not in (None, ""):
        body["temperature"] = float(s.temperature)
    r = requests.post("https://api.openai.com/v1/chat/completions", json=body, timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {key}"})
    if r.status_code == 400 and "temperature" in r.text:  # reasoning models reject temperature
        body.pop("temperature", None)
        r = requests.post("https://api.openai.com/v1/chat/completions", json=body, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {key}"})
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


def _json(r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise AIError(f"AI provider error {r.status_code}: {msg or r.text[:300]}")
    return data
