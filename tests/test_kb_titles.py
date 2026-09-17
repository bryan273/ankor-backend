"""What a retrieved passage is CALLED.

60% of the indexed corpus — 4,826 of 8,071 articles — carries a site-chrome title instead
of its own: `Anker` (1,527), `Soundcore` (1,261), and `eufy Support | Troubleshooting &amp;
Customer Service` (2,038). The crawler stored the page's `<title>`, which on these support
pages is the site name.

The bodies are correct and distinct. Only the label is wrong — and the label is what the
composer cites, so "how do I reset my earbuds" returned seven different, correct reset
guides all displayed as "Soundcore", and the customer saw seven identical sources.

These articles open by naming themselves ("This article will show you how to reset
soundcore Liberty 5"), so the opening sentence recovers the real title.
"""
from __future__ import annotations

import pytest

from app.services.kb import _from_slug, display_title

URL = "https://support.soundcore.com/s/article/how-to-reset-liberty-4-nc"


def test_a_real_title_is_left_alone():
    assert display_title("How to Reset soundcore VR P10", "body", URL) == \
        "How to Reset soundcore VR P10"


def test_html_entities_are_unescaped_in_a_real_title():
    """`AUSTRALIA &amp; NEW ZEALAND WARRANTY` is a real title stored raw."""
    assert display_title("AUSTRALIA &amp; NEW ZEALAND WARRANTY", "x", URL) == \
        "AUSTRALIA & NEW ZEALAND WARRANTY"


@pytest.mark.parametrize("chrome", [
    "Soundcore", "Anker", "eufy",
    "eufy Support | Troubleshooting &amp; Customer Service",
])
def test_chrome_titles_are_replaced_by_the_opening_sentence(chrome):
    title = display_title(
        chrome, "This article will show you how to reset soundcore Liberty 5. 1. Place", URL)
    assert title == "This article will show you how to reset soundcore Liberty 5"


def test_a_question_opening_is_kept_whole():
    assert display_title("Soundcore", "How do I reset soundcore V40i? If you are", URL) == \
        "How do I reset soundcore V40i?"


def test_chunk_zero_boilerplate_falls_back_to_the_slug():
    """"Method 1: 1" is three tokens but only one real word."""
    assert display_title("Soundcore", "Method 1: 1", URL) == "How to reset liberty 4 nc"


@pytest.mark.parametrize("ord_", [1, 2, 7])
def test_later_chunks_never_derive_from_their_body(ord_):
    """A chunk after the first opens wherever the splitter cut, so its opening line is
    not a title even when it is a fluent sentence: "To reset your earbuds: 1" and
    "1. Place the earbuds in the charging case" both read as titles at a glance, which
    makes them worse than an honest fallback."""
    assert display_title(
        "Soundcore", "1. Place the earbuds in the charging case and keep the lid open",
        URL, ord_) == "How to reset liberty 4 nc"


def test_chunk_zero_still_derives_from_its_body():
    assert display_title(
        "Soundcore", "This article will show you how to reset soundcore P40i. Step 1",
        URL, 0) == "This article will show you how to reset soundcore P40i"


def test_empty_body_falls_back_to_the_slug():
    assert display_title("Soundcore", "", "https://x/reset-space-a40") == "Reset space a40"


def test_opaque_numeric_slug_keeps_the_site_name():
    """An article id is no more informative than the brand, so do not pretend."""
    assert display_title("Soundcore", "", "https://x/12345") == "Soundcore"


def test_model_numbers_survive_the_slug_fallback():
    """`liberty-4-nc` must not become `liberty nc` — the number identifies the product."""
    assert _from_slug(URL) == "How to reset liberty 4 nc"


def test_slug_of_only_digits_is_rejected():
    assert _from_slug("https://x/98765") == ""


def test_title_is_bounded():
    long_body = "This is a very long opening sentence that just keeps going " * 5
    assert len(display_title("Anker", long_body, URL)) <= 90
