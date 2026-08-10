from agent_content_pipeline.security import redact_sensitive_data


def test_redaction_covers_sensitive_keys_headers_and_known_secret_values():
    payload = {
        "accessToken": "server-token",
        "nested": {
            "message": "Authorization: Bearer header-secret",
            "response": "Set-Cookie: session=private-cookie; Path=/; HttpOnly",
            "detail": "request rejected for explicit-secret-value",
        },
    }

    redacted = redact_sensitive_data(payload, secret_values=("explicit-secret-value",))

    rendered = repr(redacted)
    assert "server-token" not in rendered
    assert "header-secret" not in rendered
    assert "private-cookie" not in rendered
    assert "explicit-secret-value" not in rendered
    assert redacted["accessToken"] == "[REDACTED]"

