"""Secret redaction patterns."""
from __future__ import annotations

from sera.safety.redact import has_secret, redact


def test_openai_keys_redacted():
    s = "key=sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    out = redact(s)
    assert "sk-proj-" not in out
    assert "<redacted:openai-key>" in out


def test_anthropic_keys_redacted():
    s = "ANTHROPIC_API_KEY=sk-ant-api03-AbCd-EfGhIjKlMnOpQrStUvWxYz0123456789"
    out = redact(s)
    assert "sk-ant-" not in out


def test_generic_sk_keys_redacted():
    s = "use sk-1234567890ABCDEFGHIJKLMNOP for the call"
    out = redact(s)
    assert "sk-12345" not in out


def test_github_pat_redacted():
    s = "GITHUB_TOKEN=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123"
    out = redact(s)
    assert "ghp_AbCd" not in out


def test_slack_token_redacted():
    s = "auth=xoxb-1234567890-AbCdEfGhIjKl"
    out = redact(s)
    assert "xoxb-" not in out


def test_aws_access_key_redacted():
    s = "AKIAIOSFODNN7EXAMPLE is the test key"
    out = redact(s)
    assert "AKIAIOSFODNN7" not in out


def test_bearer_token_redacted():
    s = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepartXYZ"
    out = redact(s)
    assert "Bearer eyJ" not in out


def test_env_assignment_redacted():
    s = "export OPENAI_API_KEY=sk-actual-secret-value123"
    out = redact(s)
    assert "sk-actual" not in out
    assert "OPENAI_API_KEY=<redacted>" in out


def test_private_key_block_redacted():
    s = (
        "before\n-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKjQwfGYBC2x...\n"
        "-----END RSA PRIVATE KEY-----\nafter"
    )
    out = redact(s)
    assert "MIIBO" not in out
    assert "<redacted:private-key>" in out


def test_innocuous_text_unchanged():
    s = "the quick brown fox jumps over the lazy dog"
    assert redact(s) == s


def test_has_secret_probe():
    assert has_secret("sk-1234567890ABCDEFGHIJ") is True
    assert has_secret("hello world") is False
    assert has_secret("") is False
