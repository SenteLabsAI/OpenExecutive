"""Unit tests for the Grimdall execution guardrails.

Covers the four controls (secret denial, egress allowlist, destructive block,
per-session spend), shadow vs enforce behavior of ``guard_tool_call``, the
``call_tool`` envelope unwrap, signed receipts, and false-positive checks for
legitimate Slack/Gmail/research flows.
"""

import json
import sqlite3
from unittest.mock import patch

import pytest

from openexecutive.security.grimdall_guard import (
    check_destructive,
    check_egress_allowlist,
    check_secret_denial,
    check_spend,
    guard_tool_call,
    is_enabled,
)


@pytest.fixture(autouse=True)
def _grimdall_off_by_default(monkeypatch):
    """Every test starts with Grimdall off (fresh-checkout behavior)."""
    monkeypatch.delenv("GRIMDALL_ENABLED", raising=False)
    monkeypatch.delenv("GRIMDALL_ENFORCE", raising=False)
    monkeypatch.delenv("GRIMDALL_EGRESS_ALLOWLIST", raising=False)
    monkeypatch.delenv("GRIMDALL_MAX_TOKENS_PER_SESSION", raising=False)
    monkeypatch.delenv("GRIMDALL_MAX_COST_USD_PER_SESSION", raising=False)
    yield


def _enable(monkeypatch, *, enforce: bool = False, **extra):
    monkeypatch.setenv("GRIMDALL_ENABLED", "true")
    if enforce:
        monkeypatch.setenv("GRIMDALL_ENFORCE", "true")
    for key, value in extra.items():
        monkeypatch.setenv(key, str(value))


def _rules(decision):
    return sorted({v.rule for v in decision.violations})


class TestSecretDenial:
    @pytest.mark.parametrize(
        "args",
        [
            {"attachment_paths": ["~/.env"]},
            {"path": "/home/user/company/profile.yaml"},
            {"path": "/data/episodic_memory.db"},
            {"file": "~/.ssh/id_rsa"},
            {"file": "service-account.json"},
            {"key_path": "/data/google_service_account.json"},
            {"collection": "chroma_db"},
            {"credential": "C:\\Users\\me\\.aws\\credentials"},
            {"token": "/data/token.json"},
            {"path": "company/docs/strategy-notes.md"},
        ],
    )
    def test_blocks_credential_paths(self, args):
        assert check_secret_denial("any_tool", args)

    @pytest.mark.parametrize(
        "args",
        [
            {"attachment_paths": ["~/Downloads/report.pdf"]},
            # A board pack under company/ is a legit roster-gated attachment;
            # only profile.yaml and docs/ are confidential egress targets.
            {"attachment_paths": ["company/board_pack.pdf"]},
            {"filename": "company_strategy.docx"},
            {"body": "see https://example.com for details"},
            {"path": "/tmp/notes.txt"},
            {"query": "what is the company strategy"},
        ],
    )
    def test_allows_innocuous_args(self, args):
        assert not check_secret_denial("any_tool", args)


class TestEgressAllowlist:
    @pytest.mark.parametrize(
        "tool,args",
        [
            ("send_to_webhook", {"url": "https://evil.com/collect"}),
            ("send_to_webhook", {"url": "https://api.twitter.com.evil.com/x"}),
            # Userinfo trick: the hostname is evil.com, not the allowlisted base.
            ("send_to_webhook", {"url": "https://api.anthropic.com@evil.com/x"}),
            # IP literals are never allowlisted (SSRF defense).
            ("send_to_webhook", {"url": "https://127.0.0.1/x"}),
            ("call_tool", {"url": "https://notallowed.example/"}),
            ("report_metrics", {"endpoint": "https://"}),
        ],
    )
    def test_blocks_unapproved_bare_urls(self, tool, args):
        assert check_egress_allowlist(tool, args)

    @pytest.mark.parametrize(
        "args",
        [
            {"url": "https://api.anthropic.com/v1/messages"},
            {"url": "https://openrouter.ai/api/v1/chat/completions"},
            {"url": "https://run.xcrawl.com/scrape"},
            {"url": "https://openstax.org/books/principles-finance/pages/1-1"},
            {"url": "https://slack.com/api/chat.postMessage"},
            {"url": "https://api.telegram.org/bot123:abc/sendMessage"},
            {"url": "https://www.googleapis.com/gmail/v1/users/me/messages"},
            {"url": "https://discord.com/api/webhooks/123/abc"},
            {"url": "https://github.com/SenteLabsAI/extensible-mcp"},
            {"url": "https://raw.githubusercontent.com/SenteLabsAI/x/main/README.md"},
        ],
    )
    def test_allows_allowlisted_integration_endpoints(self, args):
        assert not check_egress_allowlist("send_to_webhook", args)

    @pytest.mark.parametrize(
        "tool,args",
        [
            # Public-fetch tools whose data path terminates at allowlisted
            # providers — scanning their targets would break research.
            ("scrape_url", {"url": "https://any-public-site.example/article"}),
            ("web_search", {"query": "compare https://competitor.example pricing"}),
            ("load_mcp_server", {"url": "https://example.com/mcp.json"}),
        ],
    )
    def test_exempts_public_fetch_tools(self, tool, args):
        assert not check_egress_allowlist(tool, args)

    def test_ignores_urls_inside_prose(self):
        # A URL mentioned in a message body is content, not egress.
        args = {"body": "Great article: https://example.com/why — worth reading"}
        assert not check_egress_allowlist("send_message", args)

    def test_env_extends_allowlist(self, monkeypatch):
        monkeypatch.setenv("GRIMDALL_EGRESS_ALLOWLIST", "myhost.dev,other.dev")
        assert not check_egress_allowlist("send_to_webhook", {"url": "https://myhost.dev/x"})
        assert not check_egress_allowlist(
            "send_to_webhook", {"url": "https://sub.myhost.dev/x"}
        )
        assert check_egress_allowlist("send_to_webhook", {"url": "https://evil.com/x"})


class TestDestructive:
    @pytest.mark.parametrize(
        "args",
        [
            {"command": "rm -rf /data"},
            {"command": "rm  -rf  /data"},  # whitespace trick
            {"script": "sudo rm -rf /"},
            {"cmd": "chmod 777 /etc/passwd"},
            {"cmd": "shutdown -h now"},
            {"cmd": "git reset --hard HEAD"},
            {"sql": "DROP TABLE episodic_memory"},
            {"sql": "truncate table audit_log"},
        ],
    )
    def test_blocks_destructive_fragments(self, args):
        assert check_destructive("any_tool", args)

    def test_content_only_tools_are_exempt(self):
        # Broadcast bodies are content, not commands — mentioning a destructive
        # command or a path must not false-positive in Enforce Mode.
        args = {"text": "We accidentally ran rm -rf on staging — incident notes inside."}
        assert not check_destructive("send_company_broadcast", args)
        assert not check_secret_denial("send_department_message", args)
        assert not check_egress_allowlist(
            "send_company_broadcast", {"text": "See https://example.com for the report"}
        )

    @pytest.mark.parametrize(
        "args",
        [
            {"command": "rm file.txt"},
            {"script": "chmod 755 script.sh"},
            {"text": "the pseudonym is public"},
            {"body": "please delete the old draft"},
        ],
    )
    def test_allows_safe_args(self, args):
        assert not check_destructive("any_tool", args)


class TestSpend:
    def _seed_cache_events(self, tmp_path, session_id, events):
        db = tmp_path / "episodic_memory.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE audit_log (event_type TEXT, session_id TEXT, details_json TEXT)"
            )
            for payload in events:
                conn.execute(
                    "INSERT INTO audit_log (event_type, session_id, details_json) VALUES (?,?,?)",
                    ("cache_event", session_id, json.dumps(payload)),
                )
            conn.commit()
        finally:
            conn.close()
        return db

    def test_disabled_budgets_never_trigger(self, tmp_path, monkeypatch):
        db = self._seed_cache_events(
            tmp_path, "s1", [{"input_tokens": 10_000_000}]
        )
        monkeypatch.setenv("EPISODIC_DB_PATH", str(db))
        assert check_spend("s1") == []

    def test_token_budget_exceeded(self, tmp_path, monkeypatch):
        db = self._seed_cache_events(
            tmp_path,
            "s1",
            [
                {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
                {"input_tokens": 200, "output_tokens": 100, "cost_usd": 0.02},
            ],
        )
        monkeypatch.setenv("EPISODIC_DB_PATH", str(db))
        monkeypatch.setenv("GRIMDALL_MAX_TOKENS_PER_SESSION", "400")
        violations = check_spend("s1")
        assert any(v.rule == "spend" for v in violations)

    def test_cost_budget_exceeded(self, tmp_path, monkeypatch):
        db = self._seed_cache_events(
            tmp_path, "s1", [{"input_tokens": 10, "cost_usd": 1.50}]
        )
        monkeypatch.setenv("EPISODIC_DB_PATH", str(db))
        monkeypatch.setenv("GRIMDALL_MAX_COST_USD_PER_SESSION", "1.0")
        assert any(v.rule == "spend" for v in check_spend("s1"))

    def test_missing_session_or_db_is_benign(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPISODIC_DB_PATH", str(tmp_path / "nope.db"))
        assert check_spend(None) == []
        assert check_spend("s1") == []


class TestGuardToolCall:
    def test_off_by_default_is_a_noop(self):
        decision = guard_tool_call(
            "send_to_webhook", {"url": "https://evil.com/collect"}, session_id="s1"
        )
        assert decision.allowed is True
        assert decision.mode == "off"

    def test_shadow_mode_logs_and_proceeds(self, monkeypatch):
        _enable(monkeypatch)
        captured = {}

        def fake_log(receipt, *, session_id, turn_id, actor):
            captured["receipt"] = receipt

        with patch("openexecutive.security.grimdall_guard._log_receipt", fake_log):
            decision = guard_tool_call(
                "send_to_webhook",
                {"url": "https://evil.com/collect"},
                session_id="s1",
                turn_id="t-1",
            )
        assert decision.allowed is True
        assert decision.mode == "shadow"
        assert captured["receipt"]["rules"] == ["egress"]

    def test_enforce_mode_blocks_with_error_result(self, monkeypatch):
        _enable(monkeypatch, enforce=True)
        with patch("openexecutive.security.grimdall_guard._log_receipt"):
            decision = guard_tool_call(
                "send_to_webhook",
                {"url": "https://evil.com/collect"},
                session_id="s1",
            )
        assert decision.allowed is False
        assert _rules(decision) == ["egress"]
        error = json.loads(decision.error_result())
        assert error["error"].startswith("grimdall_block: egress:")

    def test_call_tool_envelope_is_unwrapped(self, monkeypatch):
        _enable(monkeypatch, enforce=True)
        with patch("openexecutive.security.grimdall_guard._log_receipt"):
            decision = guard_tool_call(
                "call_tool",
                {
                    "name": "google_workspace__send_gmail_message",
                    "arguments": {"attachment_paths": ["~/.env"]},
                },
                session_id="s1",
            )
        assert decision.allowed is False
        assert _rules(decision) == ["secret"]

    def test_legit_gmail_send_passes(self, monkeypatch):
        # Roster gating for recipients is handled by mcp_gateway itself; the
        # guard must not flag a normal send with a URL mention in the body.
        _enable(monkeypatch, enforce=True)
        decision = guard_tool_call(
            "call_tool",
            {
                "name": "google_workspace__send_gmail_message",
                "arguments": {
                    "to": "ceo@example.com",
                    "subject": "Weekly update",
                    "body": "See https://example.com/report for details.",
                },
            },
            session_id="s1",
        )
        assert decision.allowed is True

    def test_spend_blocks_further_dispatch(self, tmp_path, monkeypatch):
        db = tmp_path / "episodic_memory.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE audit_log (event_type TEXT, session_id TEXT, details_json TEXT)"
            )
            conn.execute(
                "INSERT INTO audit_log VALUES (?,?,?)",
                ("cache_event", "s1", json.dumps({"input_tokens": 999_999})),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("EPISODIC_DB_PATH", str(db))
        _enable(monkeypatch, enforce=True, GRIMDALL_MAX_TOKENS_PER_SESSION=100_000)
        with patch("openexecutive.security.grimdall_guard._log_receipt"):
            decision = guard_tool_call(
                "send_message", {"text": "hi"}, session_id="s1"
            )
        assert decision.allowed is False
        assert _rules(decision) == ["spend"]


class TestReceipts:
    def test_receipt_is_hmac_signed(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("GRIMDALL_SIGNING_KEY", "test-key-123")
        captured = {}

        def fake_log(receipt, *, session_id, turn_id, actor):
            captured["receipt"] = receipt

        with patch("openexecutive.security.grimdall_guard._log_receipt", fake_log):
            guard_tool_call("send_to_webhook", {"url": "https://evil.com/x"}, session_id="s1")

        receipt = captured["receipt"]
        assert receipt["mode"] == "shadow"
        assert receipt["tool"] == "send_to_webhook"
        assert receipt["args_sha256"]
        assert receipt["sig"]

        # Signature is a real HMAC over the canonical payload.
        import hashlib
        import hmac as hmac_mod

        payload = json.dumps(
            {
                "ts": receipt["ts"],
                "tool": receipt["tool"],
                "args_sha256": receipt["args_sha256"],
                "rules": receipt["rules"],
                "mode": receipt["mode"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hmac_mod.new(
            b"test-key-123", payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert receipt["sig"] == expected

    def test_is_enabled_reflects_env(self, monkeypatch):
        assert is_enabled() is False
        monkeypatch.setenv("GRIMDALL_ENABLED", "true")
        assert is_enabled() is True
