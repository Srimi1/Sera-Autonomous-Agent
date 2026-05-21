"""Embedders — text and image share one vector space.

The shape: `Embedder` is an async protocol returning a fixed-dim vector.
`StubEmbedder` is a deterministic bag-of-words hash for tests / offline
work. `OpenAIEmbedder` wraps `text-embedding-3-small` (1536-d default).

Image path: `caption_image_openai(path)` runs a small vision model to
produce a caption, then `embed_with_image(embedder, path)` prefixes
`[image] ` to that caption and embeds it through the same text embedder.
Text and image queries land in the same vector space — recall works
either direction.

Outclass: OpenHuman is text-only. Hermes does vision *via tools* (each
turn requires an extra round-trip). Sera caches an embedding once per
asset and recalls across modalities with no extra LLM call.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Protocol, Sequence

import numpy as np

from sera.llm.secrets import get_key

logger = logging.getLogger(__name__)

DEFAULT_TEXT_MODEL = "text-embedding-3-small"
DEFAULT_TEXT_DIM = 1536
DEFAULT_VISION_MODEL = "gpt-4o-mini"
IMAGE_PREFIX = "[image] "
"""Marker prepended to vision captions before embedding.

Lets the retrieval layer tell image-derived chunks from native text.
Also gives the embedder a small but consistent signal that this chunk
came from a non-text source — useful when you want to bias re-ranking.
"""

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


class Embedder(Protocol):
    """Async embedder. `dim` is the fixed output dimensionality."""

    dim: int

    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...


# ─── Stub ──────────────────────────────────────────────────────────


def _bag_of_words_vector(text: str, dim: int) -> list[float]:
    """Deterministic bag-of-words hash → unit vector.

    Each unique word maps to one of `dim` buckets via MD5 → uint32 modulo.
    The result is a sparse positive vector; same words → same bucket sums →
    identical vector. Overlapping vocabularies have higher cosine than
    disjoint vocabularies, which is enough realism for offline retrieval
    tests without pulling in a real model.
    """
    vec = np.zeros(dim, dtype=np.float32)
    for word in _WORD_RE.findall(text.lower()):
        h = hashlib.md5(word.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        vec[idx] += 1.0
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec.astype(np.float32).tolist()


@dataclass
class StubEmbedder:
    """Deterministic, dependency-free embedder for tests + offline runs."""

    dim: int = 64

    async def embed(self, text: str) -> list[float]:
        return _bag_of_words_vector(text or "", self.dim)

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [_bag_of_words_vector(t or "", self.dim) for t in texts]


# ─── OpenAI ────────────────────────────────────────────────────────


class OpenAIEmbedder:
    """OpenAI text embedder. Lazy AsyncOpenAI client, batched inputs.

    The default model + dim match `text-embedding-3-small`. Override
    `model` and `dim` together when switching to `-large` (3072) or
    a custom-truncated dim via the `dimensions` request param.
    """

    def __init__(
        self,
        model: str = DEFAULT_TEXT_MODEL,
        *,
        dim: int = DEFAULT_TEXT_DIM,
    ) -> None:
        self.model = model
        self.dim = dim
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            key = get_key("openai")
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY missing. Run `sera setup` or export the env var."
                )
            self._client = AsyncOpenAI(api_key=key)
        return self._client

    async def embed(self, text: str) -> list[float]:
        out = await self.embed_batch([text])
        return out[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._ensure_client()
        # OpenAI rejects empty strings; substitute a single space.
        cleaned = [t if t else " " for t in texts]
        resp = await client.embeddings.create(
            model=self.model,
            input=list(cleaned),
            dimensions=self.dim,
        )
        # API guarantees order matches input.
        return [list(item.embedding) for item in resp.data]


# ─── Vision caption ────────────────────────────────────────────────


ImageCaptioner = Callable[[Path | bytes], Awaitable[str]]
"""Async callable that turns an image into a short description.

Accepts either a filesystem path or raw bytes. Returns plain text.
"""


def _image_to_data_url(image: Path | bytes) -> str:
    """Build an inline `data:` URL the OpenAI vision endpoint accepts."""
    if isinstance(image, (str, Path)):
        path = Path(image)
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
    else:
        data = image
        mime = "image/png"  # bytes path: assume PNG, cheap default
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def caption_image_openai(
    image: Path | bytes,
    *,
    model: str = DEFAULT_VISION_MODEL,
    max_tokens: int = 80,
) -> str:
    """Describe an image with a cheap OpenAI vision model.

    The prompt is deliberately short and concrete — the goal is a caption
    suitable for embedding, not a creative description. Strip leading
    quotes / markdown markers the model occasionally emits.
    """
    from openai import AsyncOpenAI

    key = get_key("openai")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY missing. Run `sera setup` or export the env var."
        )
    client = AsyncOpenAI(api_key=key)
    data_url = _image_to_data_url(image)
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Describe this image in one factual sentence. "
                            "Mention salient objects, setting, and any text visible. "
                            "No commentary, no quotes."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    caption = (resp.choices[0].message.content or "").strip().strip('"').strip()
    return caption


async def embed_with_image(
    embedder: Embedder,
    image: Path | bytes,
    *,
    captioner: ImageCaptioner | None = None,
) -> tuple[str, list[float]]:
    """Caption then embed. Returns (prefixed_caption, vector).

    `captioner` defaults to `caption_image_openai`. Tests inject a stub
    captioner so the path can be exercised without network.
    """
    cap_fn = captioner or caption_image_openai
    caption = await cap_fn(image)
    annotated = IMAGE_PREFIX + caption
    vec = await embedder.embed(annotated)
    return annotated, vec


# ─── Helpers ───────────────────────────────────────────────────────


async def embed_chunks(
    embedder: Embedder, contents: Iterable[str]
) -> list[list[float]]:
    """Batch-embed an iterable of chunk contents.

    Materializes the iterable up front so the embedder can batch the
    whole request. Returns the vector list in the same order.
    """
    items = list(contents)
    return await embedder.embed_batch(items)
