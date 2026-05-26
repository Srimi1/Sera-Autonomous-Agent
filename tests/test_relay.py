"""P-91 transport: real WebSocket CRDT relay convergence.

Proves the outclass: a chunk written on one node lands on another over a live
WebSocket connection, and concurrent edits converge deterministically — fast
(well under the 5s budget the phase promises).
"""
from __future__ import annotations

import asyncio

from sera.sync.crdt import CRDTDocument
from sera.sync.relay import (
    RelayClient,
    RelayServer,
    decode_doc,
    encode_doc,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# encode/decode
# ---------------------------------------------------------------------------

def test_encode_decode_roundtrip():
    doc = CRDTDocument()
    doc.chunks.add("c1", node_id="phone")
    doc.entities.set("loc", "gym", node_id="phone")
    back = decode_doc(encode_doc(doc))
    assert back.chunks.contains("c1")
    assert back.entities.get("loc") == "gym"


def test_decode_accepts_bytes():
    doc = CRDTDocument()
    doc.chunks.add("c1", node_id="n1")
    back = decode_doc(encode_doc(doc).encode("utf-8"))
    assert back.chunks.contains("c1")


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def test_server_starts_and_reports_port():
    async def _go():
        server = await RelayServer(port=0).start()
        try:
            assert server.port > 0
            assert server.url.startswith("ws://127.0.0.1:")
            assert server.n_clients == 0
        finally:
            await server.stop()

    _run(_go())


# ---------------------------------------------------------------------------
# THE OUTCLASS: phone write → laptop sees it over the wire
# ---------------------------------------------------------------------------

def test_phone_write_reaches_laptop():
    async def _go():
        server = await RelayServer(port=0).start()
        try:
            phone = await RelayClient(server.url).connect()
            laptop = await RelayClient(server.url).connect()
            try:
                # Both get the initial empty-state frame.
                await phone.wait_for_state(timeout=5.0)
                await laptop.wait_for_state(timeout=5.0)

                # Phone writes a chunk and pushes.
                phone.doc.chunks.add("note-1", node_id="phone")
                phone.doc.entities.set("location", "gym", node_id="phone", ts=1.0)
                laptop._first_state.clear()
                await phone.push()

                # Laptop converges via the relay broadcast.
                await laptop.wait_for_state(timeout=5.0)
                assert laptop.doc.chunks.contains("note-1")
                assert laptop.doc.entities.get("location") == "gym"
            finally:
                await phone.close()
                await laptop.close()
        finally:
            await server.stop()

    _run(_go())


def test_concurrent_edits_converge_deterministically():
    """Both nodes write; after both push, both hold the deterministic join."""
    async def _go():
        server = await RelayServer(port=0).start()
        try:
            phone = await RelayClient(server.url).connect()
            laptop = await RelayClient(server.url).connect()
            try:
                await phone.wait_for_state(timeout=5.0)
                await laptop.wait_for_state(timeout=5.0)

                # Concurrent edits on the same key — higher ts must win on both.
                phone.doc.chunks.add("c-phone", node_id="phone")
                phone.doc.entities.set("status", "away", node_id="phone", ts=1.0)

                laptop.doc.chunks.add("c-laptop", node_id="laptop")
                laptop.doc.entities.set("status", "online", node_id="laptop", ts=2.0)

                # Push both; let the relay merge + rebroadcast settle.
                await phone.push()
                await laptop.push()
                await asyncio.sleep(0.3)

                for node in (phone, laptop):
                    assert node.doc.chunks.contains("c-phone")
                    assert node.doc.chunks.contains("c-laptop")
                    # ts=2.0 ("online") wins deterministically on every node.
                    assert node.doc.entities.get("status") == "online"

                # Relay's authoritative merge agrees.
                assert server.doc.entities.get("status") == "online"
            finally:
                await phone.close()
                await laptop.close()
        finally:
            await server.stop()

    _run(_go())


def test_late_joiner_gets_full_history():
    """A node that connects AFTER edits receives merged state on connect."""
    async def _go():
        server = await RelayServer(port=0).start()
        try:
            phone = await RelayClient(server.url).connect()
            try:
                await phone.wait_for_state(timeout=5.0)
                phone.doc.chunks.add("early-note", node_id="phone")
                await phone.push()
                await asyncio.sleep(0.2)  # let the relay merge it

                # Laptop joins late — should be seeded with early-note.
                laptop = await RelayClient(server.url).connect()
                try:
                    await laptop.wait_for_state(timeout=5.0)
                    assert laptop.doc.chunks.contains("early-note")
                finally:
                    await laptop.close()
            finally:
                await phone.close()
        finally:
            await server.stop()

    _run(_go())


def test_sync_helper_pushes_and_returns_merged():
    async def _go():
        server = await RelayServer(port=0).start()
        try:
            a = await RelayClient(server.url).connect()
            b = await RelayClient(server.url).connect()
            try:
                await a.wait_for_state(timeout=5.0)
                await b.wait_for_state(timeout=5.0)

                b.doc.entities.set("k", "from-b", node_id="b", ts=5.0)
                await b.push()
                await asyncio.sleep(0.2)

                a.doc.entities.set("k2", "from-a", node_id="a", ts=1.0)
                merged = await a.sync(timeout=5.0)
                # a's sync round-trip reflects b's prior write too.
                assert merged.entities.get("k") == "from-b"
                assert merged.entities.get("k2") == "from-a"
            finally:
                await a.close()
                await b.close()
        finally:
            await server.stop()

    _run(_go())


def test_malformed_frame_does_not_crash_relay():
    """A garbage frame is dropped; the relay keeps serving good clients."""
    async def _go():
        from websockets.asyncio.client import connect

        server = await RelayServer(port=0).start()
        try:
            raw = await connect(server.url)
            try:
                # consume seed frame, then send junk
                await asyncio.wait_for(raw.recv(), timeout=5.0)
                await raw.send("this is not json {{{")
                await asyncio.sleep(0.1)

                # A legit client still works afterward.
                good = await RelayClient(server.url).connect()
                try:
                    await good.wait_for_state(timeout=5.0)
                    good.doc.chunks.add("still-works", node_id="g")
                    await good.push()
                    await asyncio.sleep(0.2)
                    assert server.doc.chunks.contains("still-works")
                finally:
                    await good.close()
            finally:
                await raw.close()
        finally:
            await server.stop()

    _run(_go())
