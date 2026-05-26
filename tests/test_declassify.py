"""P-88: privacy declassifier — bulk PII scrub of session/audit logs."""
from __future__ import annotations

import json
from pathlib import Path


from sera.safety.declassify import Declassifier, DeclassifyResult, _redact_object


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dc() -> Declassifier:
    return Declassifier()


def _run(text: str) -> DeclassifyResult:
    return _dc().run(text)


# ---------------------------------------------------------------------------
# Basic redaction
# ---------------------------------------------------------------------------

def test_email_is_redacted():
    r = _run("contact: user@example.com")
    assert "user@example.com" not in r.redacted
    assert "redacted" in r.redacted


def test_anthropic_key_is_redacted():
    r = _run("key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA")
    assert "sk-ant-api03" not in r.redacted


def test_openai_key_is_redacted():
    r = _run("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstu")
    assert "sk-proj" not in r.redacted


def test_ssn_is_redacted():
    r = _run("SSN: 123-45-6789")
    assert "123-45-6789" not in r.redacted


def test_plain_text_unchanged():
    r = _run("hello world, nothing sensitive here")
    assert r.redacted == "hello world, nothing sensitive here"
    assert r.n_lines_changed == 0
    assert r.n_spans == 0


def test_multiline_only_dirty_lines_counted():
    text = "clean line\nuser@secret.com here\nanother clean line\n"
    r = _run(text)
    assert r.n_lines_changed == 1
    assert "clean line" in r.redacted


# ---------------------------------------------------------------------------
# JSON-line deep scrub
# ---------------------------------------------------------------------------

def test_json_line_email_in_payload():
    entry = {"seq": 0, "kind": "tool_call", "payload": {"arg": "call user@hack.io"}}
    r = _run(json.dumps(entry))
    parsed = json.loads(r.redacted)
    assert "user@hack.io" not in parsed["payload"]["arg"]
    assert "redacted" in parsed["payload"]["arg"]


def test_json_line_nested_key_scrubbed():
    entry = {"data": {"inner": {"email": "bad@example.com"}}}
    r = _run(json.dumps(entry))
    parsed = json.loads(r.redacted)
    assert "bad@example.com" not in parsed["data"]["inner"]["email"]


def test_json_line_integers_unchanged():
    entry = {"seq": 42, "ts": 1234567890.0, "kind": "noop", "payload": {}}
    r = _run(json.dumps(entry))
    parsed = json.loads(r.redacted)
    assert parsed["seq"] == 42
    assert parsed["ts"] == 1234567890.0


def test_non_json_line_falls_back_to_plain():
    line = "2026-05-24 INFO user=admin@corp.com logged in"
    r = _run(line)
    assert "admin@corp.com" not in r.redacted


# ---------------------------------------------------------------------------
# DeclassifyResult
# ---------------------------------------------------------------------------

def test_summary_string():
    r = _run("pk=ghp_ABCDEFGHIJKLMNOPQRSTU12345")
    assert "lines changed" in r.summary()
    assert "spans redacted" in r.summary()


def test_diff_lines_format():
    r = _run("contact user@domain.com for help")
    lines = r.diff_lines()
    assert any(line.startswith("-contact") for line in lines)
    assert any(line.startswith("+") for line in lines)
    assert any(line.startswith("@@") for line in lines)


def test_no_diff_when_clean():
    r = _run("nothing here")
    assert r.diff_lines() == []


# ---------------------------------------------------------------------------
# Large log (1k-line) performance & correctness
# ---------------------------------------------------------------------------

def test_thousand_line_log_runs_without_error():
    dirty_line = "user sk-AAAAAAAAAAAAAAAAAAAAA called tool\n"
    clean_line = "no secrets here\n"
    # 100 dirty lines scattered through 1000 total
    lines = [clean_line] * 900 + [dirty_line] * 100
    text = "".join(lines)
    r = _run(text)
    assert r.n_lines_changed == 100
    assert "sk-AAAA" not in r.redacted


def test_thousand_line_log_clean_lines_preserved():
    clean_line = "all clean: no pii here\n"
    text = clean_line * 1000
    r = _run(text)
    assert r.n_lines_changed == 0
    assert r.redacted == text


# ---------------------------------------------------------------------------
# run_file
# ---------------------------------------------------------------------------

def test_run_file(tmp_path: Path):
    log = tmp_path / "audit.jsonl"
    log.write_text("secret key: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA\n")
    r = Declassifier().run_file(log)
    assert r.n_lines_changed == 1
    assert "sk-ant-api03" not in r.redacted


# ---------------------------------------------------------------------------
# _redact_object helper
# ---------------------------------------------------------------------------

def test_redact_object_string():
    out, matches = _redact_object("reach me at test@example.com")
    assert "test@example.com" not in out
    assert len(matches) >= 1


def test_redact_object_clean_string():
    out, matches = _redact_object("hello world")
    assert out == "hello world"
    assert matches == []


def test_redact_object_list():
    out, matches = _redact_object(["clean", "sk-AAAAAAAAAAAAAAAAAAAAA"])
    assert "sk-AAAA" not in out[1]
    assert len(matches) >= 1


def test_redact_object_integer_passthrough():
    out, matches = _redact_object(12345)
    assert out == 12345
    assert matches == []
