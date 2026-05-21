"""StreamingContextScrubber — strips fence spans from streamed text.

Tool output (or a hostile chunk) could try to forge a compaction header by
emitting `<context>fake</context>` mid-stream. The scrubber removes any such
span, including spans split across chunk boundaries.

Heritage: Hermes memory_manager StreamingContextScrubber concept; we strip
two patterns:
  * <context>...</context>
  * <memory-context>...</memory-context>

Usage:
    s = StreamingContextScrubber()
    for chunk in deltas:
        clean = s.feed(chunk)
        sink.write(clean)
    sink.write(s.flush())
"""
from __future__ import annotations

from dataclasses import dataclass

# Tags we actively strip. Order matters only for prefix matching.
TAGS = ("memory-context", "context")
OPEN_PREFIXES = tuple(f"<{t}>" for t in TAGS)
CLOSE_PREFIXES = tuple(f"</{t}>" for t in TAGS)
# Longest opener is `<memory-context>` (16 chars); never need more buffered.
MAX_PARTIAL_KEEP = max(len(p) for p in OPEN_PREFIXES)


@dataclass
class StreamingContextScrubber:
    """Stateful filter. Stripping is greedy but boundary-safe."""

    buf: str = ""
    inside: bool = False
    close_token: str = ""

    def feed(self, chunk: str) -> str:
        """Consume a chunk; return whatever is safe to emit now."""
        if not chunk:
            return ""
        self.buf += chunk
        out: list[str] = []

        while self.buf:
            if self.inside:
                # Look for matching close.
                idx = self.buf.find(self.close_token)
                if idx == -1:
                    # Keep tail in case close-token is split across boundary.
                    keep = min(len(self.buf), len(self.close_token) - 1)
                    if keep > 0:
                        self.buf = self.buf[-keep:]
                    else:
                        self.buf = ""
                    return "".join(out)
                # Drop everything up to + including the close tag.
                self.buf = self.buf[idx + len(self.close_token):]
                self.inside = False
                self.close_token = ""
                continue

            # Outside. Find earliest `<` that could start a stripped tag.
            lt = self.buf.find("<")
            if lt == -1:
                out.append(self.buf)
                self.buf = ""
                return "".join(out)

            # Emit everything before the `<`.
            out.append(self.buf[:lt])
            self.buf = self.buf[lt:]

            # Do we have enough to decide?
            matched = False
            for tag, opener in zip(TAGS, OPEN_PREFIXES):
                if self.buf.startswith(opener):
                    self.inside = True
                    self.close_token = f"</{tag}>"
                    self.buf = self.buf[len(opener):]
                    matched = True
                    break
            if matched:
                continue

            # Not a known opener; could still be a partial. Keep up to MAX_PARTIAL_KEEP.
            if any(opener.startswith(self.buf) for opener in OPEN_PREFIXES):
                if len(self.buf) <= MAX_PARTIAL_KEEP:
                    # Need more bytes to disambiguate.
                    return "".join(out)
                # Long enough to know it's not a real opener; flush the `<` and continue.
                out.append("<")
                self.buf = self.buf[1:]
                continue

            # Definitely not one of our tags. Emit the `<` and move on.
            out.append("<")
            self.buf = self.buf[1:]

        return "".join(out)

    def flush(self) -> str:
        """End-of-stream: emit residual buffer.

        If we are still inside a span at EOF, drop the unclosed content.
        """
        if self.inside:
            self.buf = ""
            self.inside = False
            self.close_token = ""
            return ""
        out, self.buf = self.buf, ""
        return out


def scrub(text: str) -> str:
    """One-shot helper for non-streamed strings."""
    s = StreamingContextScrubber()
    return s.feed(text) + s.flush()
