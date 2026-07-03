"""Extract and parse JSON objects from Perplexity / LLM text responses."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict


class ParseOk(TypedDict):
    ok: Literal[True]
    value: dict[str, Any]


class ParseFail(TypedDict, total=False):
    ok: Literal[False]
    reason: Literal["empty", "no_object", "truncated", "invalid"]
    detail: str


ParseResult = ParseOk | ParseFail


def _strip_model_preamble(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^[\s\S]*?</think>\s*", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^[\s\S]*?</redacted_reasoning>\s*", "", t, flags=re.IGNORECASE).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return t


def _match_brace_object(t: str, start: int) -> tuple[str, bool] | None:
    if start >= len(t) or t[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(t)):
        c = t[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return t[start : i + 1], False
    return t[start:], True


def extract_json_object(text: str) -> tuple[str, bool] | None:
    t = _strip_model_preamble(text)
    last_complete: tuple[str, bool] | None = None
    first_truncated: tuple[str, bool] | None = None
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        match = _match_brace_object(t, i)
        if not match:
            continue
        blob, truncated = match
        if not truncated:
            last_complete = (blob, False)
        elif first_truncated is None:
            first_truncated = (blob, True)
    if last_complete:
        return last_complete
    return first_truncated


def clean_json_text(raw: str) -> str:
    out = raw
    out = out.replace("\u201c", '"').replace("\u201d", '"')
    out = out.replace("\u2018", "'").replace("\u2019", "'")
    out = re.sub(r",\s*([}\]])", r"\1", out)
    out = re.sub(r"//[^\n]*", "", out)
    out = re.sub(r"/\*[\s\S]*?\*/", "", out)
    out = re.sub(r"\bNaN\b", "null", out)
    out = re.sub(r"\bInfinity\b", "null", out)
    out = re.sub(r"\bundefined\b", "null", out)
    out = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', out)
    return out


def _try_parse(raw: str) -> dict[str, Any] | None:
    try:
        v = json.loads(raw)
        if isinstance(v, dict) and not isinstance(v, list):
            return v
    except json.JSONDecodeError:
        pass
    return None


def parse_eod_report_json(text: str) -> ParseResult:
    if not text.strip():
        return {"ok": False, "reason": "empty"}
    extracted = extract_json_object(text)
    if not extracted:
        return {"ok": False, "reason": "no_object"}
    blob, truncated = extracted
    if truncated:
        return {
            "ok": False,
            "reason": "truncated",
            "detail": "Response ended before JSON object closed.",
        }
    candidates = [blob, clean_json_text(blob)]
    last_err = ""
    for candidate in candidates:
        direct = _try_parse(candidate)
        if direct:
            return {"ok": True, "value": direct}
        try:
            json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_err = str(exc)
    return {"ok": False, "reason": "invalid", "detail": last_err or None}
