"""Tests for sera.gateway.identity — unified cross-channel sessions.

P-60 verification: ask on Telegram, follow up on Slack, context preserved.
The headline test (TestCrossChannel) dispatches a Telegram event and a Slack
event from linked handles through a real Router and asserts ONE shared session.

Outclass verified:
- One session DB across every channel (identity → session, not platform → session)
- Privacy-first reply routing (native > cloud via PrivacyTier)
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import AsyncIterator

import pytest

from sera.gateway.identity import (
    DEFAULT_SESSION_TTL_S,
    PLATFORM_PRIVACY,
    IdentityStore,
    PrivacyTier,
)
from sera.gateway.router import InboundEvent, Router
from sera.llm.base import StreamChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path, *, clock=None) -> IdentityStore:
    return IdentityStore(
        db=tmp_path / "identity.db",
        ttl_s=DEFAULT_SESSION_TTL_S,
        clock=clock or time.time,
    )


class _StubLLM:
    name = "openai"
    context_budget = 32_000
    model = "stub"

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta_text="reply")
        yield StreamChunk(finish_reason="stop")


# ---------------------------------------------------------------------------
# Identity management
# ---------------------------------------------------------------------------

class TestIdentityManagement:
    def test_create_identity_returns_id(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        iid = store.create_identity(display_name="alice")
        assert iid
        assert store.identity_exists(iid)

    def test_link_and_lookup(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        iid = store.create_identity()
        store.link(iid, "telegram", "42")
        assert store.identity_for("telegram", "42") == iid

    def test_unknown_handle_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.identity_for("telegram", "999") is None

    def test_link_to_unknown_identity_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError):
            store.link("nonexistent", "telegram", "42")

    def test_link_all(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        iid = store.create_identity()
        store.link_all(iid, [("telegram", "42"), ("slack", "U1"), ("imessage", "+1415")])
        assert store.identity_for("telegram", "42") == iid
        assert store.identity_for("slack", "U1") == iid
        assert store.identity_for("imessage", "+1415") == iid

    def test_relink_reassigns_handle(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create_identity()
        b = store.create_identity()
        store.link(a, "telegram", "42")
        store.link(b, "telegram", "42")   # reassign
        assert store.identity_for("telegram", "42") == b

    def test_unlink(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        iid = store.create_identity()
        store.link(iid, "telegram", "42")
        store.unlink("telegram", "42")
        assert store.identity_for("telegram", "42") is None

    def test_links_for_returns_all(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        iid = store.create_identity()
        store.link_all(iid, [("telegram", "42"), ("slack", "U1")])
        links = store.links_for(iid)
        assert {(lk.platform, lk.channel_user_id) for lk in links} == {("telegram", "42"), ("slack", "U1")}

    def test_get_identity_includes_links(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        iid = store.create_identity(display_name="bob")
        store.link(iid, "discord", "D9")
        identity = store.get_identity(iid)
        assert identity is not None
        assert identity.display_name == "bob"
        assert len(identity.links) == 1


# ---------------------------------------------------------------------------
# Session unification
# ---------------------------------------------------------------------------

class TestSessionUnification:
    def test_linked_handles_share_session(self, tmp_path: Path) -> None:
        """The core claim: two linked handles resolve to the SAME session."""
        store = _store(tmp_path, clock=lambda: 100.0)
        iid = store.create_identity()
        store.link_all(iid, [("telegram", "42"), ("slack", "U1")])

        s_tg = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        s_sl = store.get_or_create_session("slack", "U1", workspace=str(tmp_path))
        assert s_tg.id == s_sl.id, "linked handles must share one session"

    def test_unlinked_handles_get_distinct_sessions(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: 100.0)
        s_a = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        s_b = store.get_or_create_session("slack", "U1", workspace=str(tmp_path))
        assert s_a.id != s_b.id, "unlinked handles must NOT share a session"

    def test_auto_create_mints_identity(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: 100.0)
        store.get_or_create_session("telegram", "new-user", workspace=str(tmp_path))
        assert store.identity_for("telegram", "new-user") is not None

    def test_auto_create_disabled_is_ephemeral(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: 100.0)
        store.get_or_create_session("telegram", "x", workspace=str(tmp_path), auto_create_identity=False)
        assert store.identity_for("telegram", "x") is None

    def test_same_handle_reuses_session_within_ttl(self, tmp_path: Path) -> None:
        t = [0.0]
        store = _store(tmp_path, clock=lambda: t[0])
        iid = store.create_identity()
        store.link(iid, "telegram", "42")
        t[0] = 1000.0
        s1 = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        t[0] = 1000.0 + 23 * 3600
        s2 = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        assert s1.id == s2.id

    def test_session_resets_past_ttl(self, tmp_path: Path) -> None:
        t = [0.0]
        store = _store(tmp_path, clock=lambda: t[0])
        iid = store.create_identity()
        store.link(iid, "telegram", "42")
        t[0] = 1000.0
        s1 = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        t[0] = 1000.0 + 25 * 3600
        s2 = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        assert s1.id != s2.id

    def test_session_id_for_identity(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: 100.0)
        iid = store.create_identity()
        store.link(iid, "telegram", "42")
        sess = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        assert store.session_id_for_identity(iid) == sess.id

    def test_session_id_for_channel_follows_link(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: 100.0)
        iid = store.create_identity()
        store.link_all(iid, [("telegram", "42"), ("slack", "U1")])
        sess = store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        # Slack handle resolves to the SAME session via the shared identity.
        assert store.session_id_for_channel("slack", "U1") == sess.id

    def test_active_identity_count(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: 100.0)
        a = store.create_identity()
        store.link(a, "telegram", "1")
        b = store.create_identity()
        store.link(b, "slack", "2")
        store.get_or_create_session("telegram", "1", workspace=str(tmp_path))
        store.get_or_create_session("slack", "2", workspace=str(tmp_path))
        assert store.active_identity_count() == 2


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class TestMerge:
    def test_merge_moves_links(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create_identity()
        b = store.create_identity()
        store.link(a, "telegram", "42")
        store.link(b, "slack", "U1")
        store.merge(a, b)
        assert store.identity_for("telegram", "42") == a
        assert store.identity_for("slack", "U1") == a
        assert not store.identity_exists(b)

    def test_merge_after_use_unifies_sessions(self, tmp_path: Path) -> None:
        """Two people-then-discovered-as-one: merge keeps the freshest session."""
        t = [100.0]
        store = _store(tmp_path, clock=lambda: t[0])
        a = store.create_identity()
        store.link(a, "telegram", "42")
        b = store.create_identity()
        store.link(b, "slack", "U1")
        store.get_or_create_session("telegram", "42", workspace=str(tmp_path))
        t[0] = 200.0
        s_b = store.get_or_create_session("slack", "U1", workspace=str(tmp_path))   # newer
        store.merge(a, b)
        # After merge, the slack handle (now under a) keeps the fresher session.
        assert store.session_id_for_channel("slack", "U1") == s_b.id
        assert store.session_id_for_channel("telegram", "42") == s_b.id

    def test_merge_self_is_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create_identity()
        store.merge(a, a)
        assert store.identity_exists(a)

    def test_merge_unknown_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create_identity()
        with pytest.raises(ValueError):
            store.merge(a, "ghost")


# ---------------------------------------------------------------------------
# Privacy-first reply routing — native beats cloud
# ---------------------------------------------------------------------------

class TestPrivacyRouting:
    def test_tier_mapping(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.tier_for("imessage") == PrivacyTier.NATIVE
        assert store.tier_for("email") == PrivacyTier.SELF_HOSTED
        assert store.tier_for("telegram") == PrivacyTier.CLOUD

    def test_unknown_platform_is_cloud(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.tier_for("myspace") == PrivacyTier.CLOUD

    def test_prefers_native_over_cloud(self, tmp_path: Path) -> None:
        """iMessage (NATIVE) beats Telegram (CLOUD) for outbound reply."""
        store = _store(tmp_path, clock=lambda: 100.0)
        iid = store.create_identity()
        store.link_all(iid, [("telegram", "42"), ("imessage", "+1415")])
        pref = store.preferred_channel(iid)
        assert pref is not None
        assert pref.platform == "imessage"

    def test_prefers_self_hosted_over_cloud(self, tmp_path: Path) -> None:
        store = _store(tmp_path, clock=lambda: 100.0)
        iid = store.create_identity()
        store.link_all(iid, [("slack", "U1"), ("email", "a@b.com")])
        pref = store.preferred_channel(iid)
        assert pref is not None
        assert pref.platform == "email"

    def test_tie_broken_by_recency(self, tmp_path: Path) -> None:
        """Two CLOUD channels: the most recently seen wins."""
        t = [100.0]
        store = _store(tmp_path, clock=lambda: t[0])
        iid = store.create_identity()
        store.link(iid, "telegram", "42")
        t[0] = 200.0
        store.link(iid, "discord", "D9")
        # touch discord more recently
        t[0] = 300.0
        store.get_or_create_session("discord", "D9", workspace=str(tmp_path))
        pref = store.preferred_channel(iid)
        assert pref is not None
        assert pref.platform == "discord"

    def test_no_links_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        iid = store.create_identity()
        assert store.preferred_channel(iid) is None

    def test_default_privacy_map_covers_all_channels(self) -> None:
        for platform in ["imessage", "email", "whatsapp", "telegram", "discord", "slack", "twilio"]:
            assert platform in PLATFORM_PRIVACY


# ---------------------------------------------------------------------------
# THE HEADLINE: cross-channel reference through a real Router
# ---------------------------------------------------------------------------

class TestCrossChannel:
    def test_ask_on_telegram_follow_up_on_slack_same_session(self, tmp_path: Path) -> None:
        """P-60 verification: ask on Telegram, follow up on Slack, one session.

        This is the outclass no rival ships: the Slack follow-up lands in the
        SAME Sera session the Telegram question created, so context carries.
        """
        store = _store(tmp_path, clock=lambda: 100.0)
        owner = store.create_identity(display_name="me")
        store.link_all(owner, [("telegram", "42"), ("slack", "U123")])

        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )

        # Ask on Telegram
        tg = InboundEvent(platform="telegram", user_id="42", channel_id="42",
                          text="my favorite color is blue")
        asyncio.run(router.dispatch(tg))
        session_after_telegram = store.session_id_for_channel("telegram", "42")

        # Follow up on Slack
        sl = InboundEvent(platform="slack", user_id="U123", channel_id="C9",
                          text="what's my favorite color?")
        asyncio.run(router.dispatch(sl))
        session_after_slack = store.session_id_for_channel("slack", "U123")

        assert session_after_telegram is not None
        assert session_after_telegram == session_after_slack, (
            "Telegram question and Slack follow-up must share one session — "
            "this is the cross-channel context-preservation outclass"
        )

    def test_unlinked_strangers_stay_separate(self, tmp_path: Path) -> None:
        """Two different people on two channels never collide into one session."""
        store = _store(tmp_path, clock=lambda: 100.0)
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )
        alice = InboundEvent(platform="telegram", user_id="alice", channel_id="alice", text="hi")
        bob = InboundEvent(platform="slack", user_id="bob", channel_id="bob", text="hi")
        asyncio.run(router.dispatch(alice))
        asyncio.run(router.dispatch(bob))
        s_alice = store.session_id_for_channel("telegram", "alice")
        s_bob = store.session_id_for_channel("slack", "bob")
        assert s_alice != s_bob

    def test_three_channels_one_session(self, tmp_path: Path) -> None:
        """Telegram + Slack + iMessage all collapse to one owner session."""
        store = _store(tmp_path, clock=lambda: 100.0)
        owner = store.create_identity(display_name="me")
        store.link_all(owner, [("telegram", "42"), ("slack", "U1"), ("imessage", "+1415")])
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )
        for platform, uid in [("telegram", "42"), ("slack", "U1"), ("imessage", "+1415")]:
            ev = InboundEvent(platform=platform, user_id=uid, channel_id=uid, text="hi")
            asyncio.run(router.dispatch(ev))

        sessions = {
            store.session_id_for_channel(p, u)
            for p, u in [("telegram", "42"), ("slack", "U1"), ("imessage", "+1415")]
        }
        assert len(sessions) == 1, "all three channels must share exactly one session"

        # And the preferred reply channel is the most-private one (iMessage).
        pref = store.preferred_channel(owner)
        assert pref is not None and pref.platform == "imessage"
