from __future__ import annotations

import json

import pytest

from supervisor.runtime_errors import (
    MAX_PROVIDER_ERROR_BYTES,
    bounded_provider_error,
    sanitize_error_text,
)


def test_normal_error_preserves_diagnostics_without_copying_other_params():
    error = {
        "message": "stream disconnected before completion: HTTP 503",
        "additionalDetails": "upstream request req_123 timed out after 30s",
        "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}},
    }
    params = {"error": error, "willRetry": True, "threadId": "thread", "headers": {"secret": "private"}}
    result = bounded_provider_error(params)
    assert result == {"error": error, "willRetry": True}
    assert result["error"] is not error
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("retry", [True, False])
def test_retry_boolean_remains_exact(retry):
    assert bounded_provider_error({"willRetry": retry}) == {"willRetry": retry}


@pytest.mark.parametrize("retry", [None, 0, 1, "false", "true", "", [], {}])
def test_invalid_retry_is_null_not_false_or_missing(retry):
    result = bounded_provider_error({"willRetry": retry})
    assert "willRetry" in result and result["willRetry"] is None
    assert "willRetry" not in bounded_provider_error({})


@pytest.mark.parametrize("params", [
    {"error": "provider overloaded"},
    {"message": "provider overloaded"},
    {"error": {"message": None}, "message": "provider overloaded"},
])
def test_error_message_fallbacks(params):
    assert bounded_provider_error(params) == {"error": {"message": "provider overloaded"}}


@pytest.mark.parametrize(("text", "secrets"), [
    ("Authorization: Bearer abc123secret", ["abc123secret"]),
    ("Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA==", ["dXNlcjpwYXNzd29yZA=="]),
    ('{"api_key": "api secret with spaces", "status": 401}', ["api secret with spaces"]),
    ('{"password":"abc\\\"def secret", "status": 401}', ["abc", "def secret"]),
    ("OPENAI_API_KEY=private-key; status=401", ["private-key"]),
    ("AWS_SECRET_ACCESS_KEY=privateaws; status=401", ["privateaws"]),
    ("--access-token private-cli-token request failed", ["private-cli-token"]),
    ("refreshToken: refreshvalue", ["refreshvalue"]),
    ("Cookie: session=privatecookie; csrf=privatecsrf\nHTTP 403", ["privatecookie", "privatecsrf"]),
    ("sk-proj-Abcdef123456789 failed", ["sk-proj-Abcdef123456789"]),
    ("hf_Abcdef123456789 failed", ["hf_Abcdef123456789"]),
    ("ghp_Abcdef123456789 failed", ["ghp_Abcdef123456789"]),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature failed", ["eyJhbGciOiJIUzI1NiJ9"]),
    ("-----BEGIN PRIVATE KEY-----\nprivatematerial\n-----END PRIVATE KEY----- failed", ["privatematerial"]),
    ("-----BEGIN RSA PRIVATE KEY-----\nprivatematerial", ["privatematerial"]),
])
def test_common_credentials_are_redacted(text, secrets):
    sanitized = sanitize_error_text(text)
    assert "[REDACTED]" in sanitized
    for secret in secrets:
        assert secret not in sanitized


@pytest.mark.parametrize("scheme", ["https", "http", "wss", "postgresql"])
def test_urls_keep_endpoint_but_remove_userinfo_query_and_fragment(scheme):
    text = f"failed {scheme}://alice:privatepass@host.example:443/v1/responses?api_key=privatequery#privatefragment"
    sanitized = sanitize_error_text(text)
    assert "host.example:443/v1/responses" in sanitized
    assert all(secret not in sanitized for secret in ("alice", "privatepass", "privatequery", "privatefragment"))
    assert "?[REDACTED]#[REDACTED]" in sanitized


def test_ordinary_error_unicode_codes_and_safe_url_are_not_changed():
    text = "Ошибка соединения 🌍 HTTP 503 at https://api.example/v1/responses; req_123 retries=2 token_count=45"
    assert sanitize_error_text(text) == text


def test_control_escapes_are_removed_before_credential_redaction():
    text = "\x1b[31mBearer\x1b[0m private-token\x00\u202e\nHTTP 401"
    sanitized = sanitize_error_text(text)
    assert "private-token" not in sanitized
    assert "\x1b" not in sanitized and "\x00" not in sanitized and "\u202e" not in sanitized
    assert "HTTP 401" in sanitized


def test_carriage_returns_are_normalized_without_terminal_overwrite():
    assert sanitize_error_text("retrying\rHTTP 503\r\nnext attempt") == "retrying\nHTTP 503\nnext attempt"


def test_scan_boundary_does_not_reveal_partial_key_after_prefix_shrinks():
    prefix = "https://host/path?value=" + "x" * 65_503 + " "
    assert len(prefix) == 65_528
    text = prefix + "sk-secret-value-beyond-scan-limit"
    sanitized = sanitize_error_text(text)
    assert "sk-secret" not in sanitized
    assert "[truncated]" in sanitized


def test_structured_info_is_sanitized_recursively():
    params = {"error": {"codexErrorInfo": {
        "httpConnectionFailed": {"httpStatusCode": 401, "Authorization": "Bearer privatestructured"},
        "details": ["password=privatepassword", "https://host/x?privatequery"],
    }}}
    result = bounded_provider_error(params)
    info = result["error"]["codexErrorInfo"]
    assert info["httpConnectionFailed"]["httpStatusCode"] == 401
    assert "private" not in json.dumps(result)
    assert params["error"]["codexErrorInfo"]["httpConnectionFailed"]["Authorization"] == "Bearer privatestructured"


def test_large_nested_unicode_payload_has_json_size_bound_and_retry_survives():
    params = {"willRetry": False, "error": {
        "message": "🌍" * 100_000,
        "additionalDetails": "Ошибка" * 100_000,
        "codexErrorInfo": {f"field{n}": ["🌍" * 10_000] * 100 for n in range(100)},
    }}
    result = bounded_provider_error(params)
    assert result["willRetry"] is False
    assert len(json.dumps(result).encode("utf-8")) <= MAX_PROVIDER_ERROR_BYTES
    assert "truncated" in json.dumps(result)


def test_deep_and_cyclic_info_is_bounded_without_repr_or_nan():
    recursive = {"nan": float("nan"), "huge": 10 ** 100, "unknown": object()}
    recursive["self"] = recursive
    result = bounded_provider_error({"error": {"codexErrorInfo": recursive}})
    encoded = json.dumps(result, allow_nan=False)
    assert len(encoded.encode()) <= MAX_PROVIDER_ERROR_BYTES
    assert "truncated" in encoded
    assert "object at" not in encoded


@pytest.mark.parametrize("limit", [0, 1, 10, 40, 100])
def test_text_limit_is_exact_and_redaction_precedes_truncation(limit):
    result = sanitize_error_text("Bearer " + "private" * 20_000, max_chars=limit)
    assert len(result) <= limit
    assert "private" not in result


def test_large_json_escaped_text_stays_inside_payload_bound():
    result = bounded_provider_error({"error": {
        "message": '"\\' * 100_000,
        "additionalDetails": '"\\' * 100_000,
        "codexErrorInfo": {str(n): '\\"🌍' * 100_000 for n in range(100)},
    }})
    assert len(json.dumps(result).encode()) <= MAX_PROVIDER_ERROR_BYTES
