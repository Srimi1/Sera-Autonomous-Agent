"""P-95: Sera Mobile — shared core via the sidecar API + CRDT sync.

OUTCLASS: The phone is not a thin remote. It runs the SAME core: it talks to
the sidecar HTTP API (chat, ingest) and reconciles offline memory edits through
the P-91 CRDT layer, so a note written on the phone while offline merges
deterministically with the laptop the moment they reconnect — same session DB,
no last-writer-clobbers.
"""
from sera.mobile.sync_client import MobileSyncClient, TransportError

__all__ = ["MobileSyncClient", "TransportError"]
