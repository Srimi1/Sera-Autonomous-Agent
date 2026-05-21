"""P-13: embedders (stub + OpenAI shape) + image-caption-then-embed flow."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sera.memory.embedder import (
    IMAGE_PREFIX,
    OpenAIEmbedder,
    StubEmbedder,
    _bag_of_words_vector,
    _image_to_data_url,
    embed_chunks,
    embed_with_image,
)
from sera.memory.tree import MemoryTree, cosine_similarity


DIM = 64


def _run(coro):
    return asyncio.run(coro)


# ─── Stub ────────────────────────────────────────────────────────


def test_stub_dim_and_normalized():
    e = StubEmbedder(dim=DIM)
    v = _run(e.embed("the quick brown fox"))
    assert len(v) == DIM
    norm = math.sqrt(sum(x * x for x in v))
    assert norm == pytest.approx(1.0, rel=0.001)


def test_stub_is_deterministic():
    e = StubEmbedder(dim=DIM)
    v1 = _run(e.embed("sera autonomous agent"))
    v2 = _run(e.embed("sera autonomous agent"))
    assert v1 == v2


def test_stub_empty_string_is_zero_vector():
    e = StubEmbedder(dim=DIM)
    v = _run(e.embed(""))
    assert v == [0.0] * DIM


def test_stub_batch_matches_individual():
    e = StubEmbedder(dim=DIM)
    texts = ["hello", "world", ""]
    batch = _run(e.embed_batch(texts))
    individual = [_run(e.embed(t)) for t in texts]
    assert batch == individual


def test_similar_text_has_higher_cosine_than_unrelated():
    e = StubEmbedder(dim=DIM)
    a = _run(e.embed("a fluffy cat on a windowsill"))
    b = _run(e.embed("cat sitting at a windowsill"))
    c = _run(e.embed("compiler design and abstract syntax trees"))
    assert cosine_similarity(a, b) > cosine_similarity(a, c)


def test_bag_of_words_vector_bucket_repeatable():
    v1 = _bag_of_words_vector("alpha beta", 8)
    v2 = _bag_of_words_vector("alpha beta", 8)
    assert v1 == v2
    # Word order does not change the vector — bag of words.
    v3 = _bag_of_words_vector("beta alpha", 8)
    assert v1 == v3


# ─── embed_chunks helper ─────────────────────────────────────────


def test_embed_chunks_preserves_order():
    e = StubEmbedder(dim=DIM)
    vectors = _run(embed_chunks(e, ["alpha", "beta", "gamma"]))
    assert len(vectors) == 3
    assert vectors[0] == _run(e.embed("alpha"))


# ─── Image path ──────────────────────────────────────────────────


async def _fake_captioner(image):
    # Returns caption based on filename so the test is deterministic.
    if isinstance(image, (str, Path)):
        name = Path(image).stem
    else:
        name = "bytes"
    return f"a photo of a cat named {name}"


def test_embed_with_image_prefixes_caption(tmp_path: Path):
    img = tmp_path / "mittens.png"
    img.write_bytes(b"fake png")
    e = StubEmbedder(dim=DIM)
    caption, vec = _run(embed_with_image(e, img, captioner=_fake_captioner))
    assert caption.startswith(IMAGE_PREFIX)
    assert "mittens" in caption
    assert len(vec) == DIM


def test_image_query_retrieves_matching_text_chunk(tmp_path: Path):
    """Image caption + text content share keywords → high recall hit."""
    e = StubEmbedder(dim=DIM)
    tree = MemoryTree(db_path=tmp_path / "mem.db", embedding_dim=DIM)

    text_content = "a fluffy cat resting on a windowsill in soft afternoon light"
    text_vec = _run(e.embed(text_content))
    text_id = tree.add_chunk(source="notes", content=text_content, embedding=text_vec)

    unrelated_content = "compiler optimizations: dead code elimination and inlining"
    unrelated_vec = _run(e.embed(unrelated_content))
    tree.add_chunk(source="notes", content=unrelated_content, embedding=unrelated_vec)

    img = tmp_path / "cat-windowsill.png"
    img.write_bytes(b"fake png")

    async def caption(_image):
        return "fluffy cat sitting on a windowsill"

    _, img_vec = _run(embed_with_image(e, img, captioner=caption))
    hits = tree.search(img_vec, limit=2)
    assert hits[0].chunk_id == text_id, "image caption should retrieve text chunk first"
    assert hits[0].distance <= hits[1].distance


def test_image_to_data_url_includes_mime(tmp_path: Path):
    img = tmp_path / "x.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpg")
    url = _image_to_data_url(img)
    assert url.startswith("data:image/jpeg;base64,")
    assert "fake-jpg" not in url  # raw bytes must be base64-encoded


def test_image_to_data_url_from_bytes_defaults_to_png():
    url = _image_to_data_url(b"\x89PNG")
    assert url.startswith("data:image/png;base64,")


# ─── OpenAI shape (mocked) ───────────────────────────────────────


def test_openai_embedder_request_shape():
    """Mock the SDK to verify we send model / dimensions / input list."""
    e = OpenAIEmbedder(model="text-embedding-3-small", dim=8)

    fake_resp = MagicMock()
    fake_resp.data = [MagicMock(embedding=[0.0] * 8), MagicMock(embedding=[1.0] * 8)]

    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock(return_value=fake_resp)
    e._client = fake_client

    out = _run(e.embed_batch(["hello", "world"]))
    assert out == [[0.0] * 8, [1.0] * 8]

    args, kwargs = fake_client.embeddings.create.call_args
    assert kwargs["model"] == "text-embedding-3-small"
    assert kwargs["dimensions"] == 8
    assert kwargs["input"] == ["hello", "world"]


def test_openai_embedder_substitutes_empty_with_space():
    e = OpenAIEmbedder(dim=4)
    fake_resp = MagicMock()
    fake_resp.data = [MagicMock(embedding=[0.0] * 4)]
    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock(return_value=fake_resp)
    e._client = fake_client

    _run(e.embed(""))
    args, kwargs = fake_client.embeddings.create.call_args
    assert kwargs["input"] == [" "]


def test_openai_embedder_raises_without_key():
    e = OpenAIEmbedder(dim=4)
    with patch("sera.memory.embedder.get_key", return_value=None):
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            _run(e.embed("x"))


def test_openai_embedder_empty_batch_short_circuits():
    e = OpenAIEmbedder(dim=4)
    # No client init needed — empty input must not touch the network.
    out = _run(e.embed_batch([]))
    assert out == []
