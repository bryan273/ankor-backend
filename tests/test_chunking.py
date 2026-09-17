"""`chunk_text` — the splitter that quietly inflated the index 31x.

The bug: the loop advanced `start` by `end - overlap`, but once `end` reached the end of
the text that expression fell BEHIND `start`, so the `max(..., start + 1)` guard took over
and moved forward one character at a time — emitting a near-identical chunk on every
iteration until the text ran out.

It never crashed, never logged, and produced plausible-looking chunks. A 3,494-character
article became 402 chunks. Across the corpus, 2,719 articles that should hold ~2,964
chunks held 92,533 — **97% duplicates**, all embedded, all stored, all competing for the
same top-k. `kb.MAX_PER_ARTICLE` exists because of this: it was a cap invented to stop one
article swamping every search, which was this bug wearing a disguise.

These tests are about the loop's termination, not its aesthetics.
"""
from __future__ import annotations

import pytest

from scripts.embed_corpus import chunk_text

SIZE = 3200
OVERLAP = 480


def sentences(n: int, words: int = 12) -> str:
    """Prose with real sentence boundaries, so the splitter has somewhere to cut."""
    return " ".join(f"This is sentence number {i} and it carries {words} words of text."
                    for i in range(n))


@pytest.mark.parametrize("length", [100, 500, 3_000, 3_199, 3_201, 3_494, 5_000, 20_000])
def test_chunk_count_is_proportional_to_length(length: int) -> None:
    """The count must track the text, not the overlap arithmetic.

    3,494 is the regression case: just past one chunk, with a tail shorter than the
    overlap. That is exactly the shape that used to degenerate.
    """
    text = sentences(length // 60 + 1)[:length]
    pieces = chunk_text(text)
    # Generous ceiling — the point is that it cannot be hundreds.
    ceiling = max(1, (len(text) // (SIZE - OVERLAP)) + 2)
    assert len(pieces) <= ceiling, (
        f"{len(text)} chars produced {len(pieces)} chunks (ceiling {ceiling})")


def test_short_tail_does_not_degenerate() -> None:
    """The exact failure: a tail shorter than `overlap` after the first chunk."""
    text = sentences(70)[:SIZE + OVERLAP // 2]
    pieces = chunk_text(text)
    assert len(pieces) <= 3, f"expected a couple of chunks, got {len(pieces)}"


def test_chunks_are_not_near_duplicates() -> None:
    """Consecutive chunks must not be the same text shifted by a character."""
    pieces = chunk_text(sentences(120))
    for a, b in zip(pieces, pieces[1:]):
        assert a != b
        # A one-character shift is the signature of the degenerate loop.
        assert not (len(a) == len(b) and a[1:] == b[:-1]), "chunks differ by one character"


def test_covers_the_whole_text() -> None:
    """Termination must not come at the cost of dropping the tail."""
    text = sentences(200)
    pieces = chunk_text(text)
    assert pieces, "no chunks produced"
    assert text.strip().endswith(pieces[-1].strip()[-40:]), "the end of the text was lost"


def test_text_shorter_than_one_chunk_is_one_chunk() -> None:
    assert len(chunk_text(sentences(3))) == 1


def test_empty_text_produces_nothing() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []
