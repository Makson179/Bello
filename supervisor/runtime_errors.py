"""Bounded, best-effort credential-safe diagnostics for provider errors.

These helpers only prepare logs. They do not decide whether to retry or end a
turn, and never change a missing/invalid ``willRetry`` into a terminal error.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import Any


MAX_PROVIDER_ERROR_BYTES = 16_384
_MAX_SCAN_CHARS = 65_536
_TRUNCATED = "...[truncated]"
_REDACTED = "[REDACTED]"
_SECRET_KEY = (
    r"(?:(?:[a-z][a-z0-9]*[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|auth[_-]?token|session[_-]?token|client[_-]?secret|"
    r"secret(?:[_-]access[_-]key)?|password|passwd|token|authorization|"
    r"proxy[_-]?authorization|cookie|set[_-]?cookie)|"
    r"apiKey|accessToken|refreshToken|clientSecret|secretKey)"
)
_SECRET_VALUE = r'''(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;{}]+)'''
_ASSIGNMENT = re.compile(
    rf'''(?P<prefix>(?<![\w.-])(?:--)?["']?{_SECRET_KEY}["']?\s*[:=]\s*){_SECRET_VALUE}''',
    re.IGNORECASE,
)
_CLI_SECRET = re.compile(rf"(?P<prefix>--{_SECRET_KEY}\s+){_SECRET_VALUE}", re.IGNORECASE)
_AUTH = re.compile(r"\b(?:Bearer|Basic)\s+[^\s,;\"'<>]+", re.IGNORECASE)
_COOKIE = re.compile(r"(?im)^(\s*(?:cookie|set-cookie)\s*:\s*)[^\r\n]+")
_URL = re.compile(r'''\b[a-z][a-z0-9+.-]{0,31}://[^\s<>"'`]+''', re.IGNORECASE)
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|hf_[A-Za-z0-9]{8,}|"
    r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|$)",
    re.DOTALL,
)
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED):
        return _TRUNCATED[:limit]
    return text[:limit - len(_TRUNCATED)] + _TRUNCATED


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    base, fragment, _ = url.partition("#")
    base, query, _ = base.partition("?")
    scheme, address = base.split("://", 1)
    authority, slash, path = address.partition("/")
    if "@" in authority:
        authority = _REDACTED + "@" + authority.rsplit("@", 1)[1]
    return (scheme + "://" + authority + slash + path
            + ("?" + _REDACTED if query else "")
            + ("#" + _REDACTED if fragment else ""))


def sanitize_error_text(text: str, *, max_chars: int = 4096) -> str:
    """Redact common credentials before clipping a bounded diagnostic string.

    URL paths/status messages remain readable; URL userinfo, query and fragment
    never do. This is not a claim to recognize arbitrary unlabeled secrets.
    """
    if not isinstance(max_chars, int) or max_chars < 0:
        raise ValueError("max_chars must be a nonnegative integer")
    limit = min(max_chars, _MAX_SCAN_CHARS)
    if not text or limit == 0:
        return ""
    clipped_input = len(text) > _MAX_SCAN_CHARS
    text = text[:_MAX_SCAN_CHARS]
    if clipped_input:
        # Do not expose a partial credential at the scan boundary after earlier
        # redactions shrink the prefix. Its last unfinished word is unavailable.
        text = re.sub(r"\S+$", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROLS.sub("", _ANSI.sub("", text))
    text = _PRIVATE_KEY.sub(_REDACTED, text)
    text = _URL.sub(_redact_url, text)
    text = _COOKIE.sub(lambda m: m[1] + _REDACTED, text)
    text = _AUTH.sub(_REDACTED, text)
    text = _ASSIGNMENT.sub(lambda m: m["prefix"] + _REDACTED, text)
    text = _CLI_SECRET.sub(lambda m: m["prefix"] + _REDACTED, text)
    text = _TOKEN.sub(_REDACTED, text)
    if clipped_input:
        text += _TRUNCATED
    return _clip(text, limit)


def _json(value: Any) -> str:
    # ASCII gives a predictable upper bound even for control/unicode text.
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _json_text(text: str, max_bytes: int) -> str:
    if len(_json(text)) <= max_bytes:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(_json(text[:mid] + _TRUNCATED)) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return text[:low] + _TRUNCATED


def _bounded_info(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> Any:
    nodes = [64] if nodes is None else nodes
    if nodes[0] <= 0 or depth >= 4:
        return _TRUNCATED
    nodes[0] -= 1
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= 64 else "[integer out of range]"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return sanitize_error_text(value, max_chars=512)
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 16 or nodes[0] <= 0:
                result["__truncated__"] = True
                break
            if not isinstance(key, str):
                continue
            safe_key = sanitize_error_text(key, max_chars=80)
            result[safe_key] = (_REDACTED if re.fullmatch(_SECRET_KEY, key, re.IGNORECASE)
                                else _bounded_info(item, depth=depth + 1, nodes=nodes))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for index, item in enumerate(value):
            if index >= 16 or nodes[0] <= 0:
                result.append(_TRUNCATED)
                break
            result.append(_bounded_info(item, depth=depth + 1, nodes=nodes))
        return result
    return "[unsupported value]"


def bounded_provider_error(params: Mapping[str, Any]) -> dict[str, Any]:
    """Keep diagnostic error fields and tri-state retry information, <=16 KiB.

    An absent ``willRetry`` stays absent; a present non-boolean becomes null.
    Only the literal booleans True and False carry provider retry semantics.
    """
    result: dict[str, Any] = {}
    if "willRetry" in params:
        retry = params["willRetry"]
        result["willRetry"] = retry if type(retry) is bool else None
    error = params.get("error")
    source = error if isinstance(error, Mapping) else {}
    details: dict[str, Any] = {}
    message = source.get("message")
    if not isinstance(message, str):
        message = error if isinstance(error, str) else params.get("message")
    if isinstance(message, str):
        details["message"] = _json_text(sanitize_error_text(message), 4096)
    additional = source.get("additionalDetails")
    if isinstance(additional, str):
        details["additionalDetails"] = _json_text(sanitize_error_text(additional), 4096)
    if "codexErrorInfo" in source:
        info = _bounded_info(source["codexErrorInfo"])
        rendered = _json(info)
        if len(rendered) > 6144:
            info = {"truncated": True, "preview": _json_text(rendered, 5800)}
        details["codexErrorInfo"] = info
    if details:
        result["error"] = details
    return result
