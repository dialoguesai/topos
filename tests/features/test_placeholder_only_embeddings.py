"""A document that is only redaction placeholders is not a document.

When a record's sole embeddable field is a disclosed one — a location event
whose whole document is its ``place_name`` — redaction collapses the document to
the placeholder token itself. The row still gets a vector, still occupies an ANN
slot, and matches everything and nothing.

Measured on the owner's node 2026-08-27: **134 such embeddings, 126 of them the
literal ``[ADDRESS]``, sharing 23 distinct vectors between them.** The junk
reaper could not see them because it only knew about binary-serialization
markers, so no sweep at any scope could remove them.

The discrimination that matters is between a document that IS a placeholder and
one that CONTAINS one. "met at [ADDRESS] today" is a real sentence with a
redacted span and must stay searchable; "[ADDRESS]" on its own is not.
"""

from __future__ import annotations

import pytest

from topos.features.signal.embed_context import is_derivable_content, is_placeholder_only


@pytest.mark.parametrize(
    "text",
    [
        "[ADDRESS]",
        "  [ADDRESS]  ",
        "[NAME][NAME]",
        "[URL][URL][URL][URL]",
        "[ADDRESS]-[ADDRESS]",
        "[ADDRESS], [ADDRESS]",
        "[PHONE] / [EMAIL]",
    ],
)
def test_a_placeholder_only_document_is_junk(text):
    assert is_placeholder_only(text)
    assert not is_derivable_content(text)


@pytest.mark.parametrize(
    "text",
    [
        "met at [ADDRESS] today",
        "[ADDRESS] and [NAME] talked about the move",
        "dinner, then [ADDRESS]",
        "Grow App",
        "a normal sentence",
    ],
)
def test_a_document_that_merely_contains_a_placeholder_survives(text):
    """The redaction is a span inside real content — the content is the point."""
    assert not is_placeholder_only(text)
    assert is_derivable_content(text)


def test_an_empty_document_is_not_a_placeholder(text=""):
    """Empty was already rejected for its own reason; keep the reasons distinct."""
    assert not is_placeholder_only("")
    assert not is_derivable_content("")


def test_lowercase_bracket_text_is_not_a_placeholder():
    """Placeholders are SHOUTED by the disclosure layer. `[note]` is prose."""
    assert not is_placeholder_only("[note]")


def test_binary_junk_is_still_rejected_for_its_own_reason():
    """Control: this added a rule, it did not replace one."""
    assert not is_derivable_content("streamtyped\x81\xe8NSAttributedString")


def test_the_reaper_can_now_see_them():
    """`purge_junk_embeddings` judges from the stored preview via this predicate,
    so teaching the predicate is what makes the rows reachable at all."""
    import inspect

    from topos.features.lifecycle import gc

    src = inspect.getsource(gc.purge_junk_embeddings)
    assert "is_derivable_content" in src


# ------------------------------------------------- the owner's own name

def test_the_guard_covers_the_owner_name_and_handle():
    """The one person whose name is both unambiguous and everywhere.

    Not every person: 1,249 contacts would collide with ordinary English
    ("Unknown", "Claude", "Porter") and a noisy hook gets deleted, which costs
    more than it catches. The owner is the exception — and the leak is real: the
    name was in ten tracked files and the login form in one, none of them
    reachable by the place/goal/black-hole scans.

    The floor keeps the FULL name and drops the bare first name, which is five
    characters and appears in synthetic fixtures that are not a leak.
    """
    import inspect

    from scripts import scan_repo_for_owner_data as scanner

    src = inspect.getsource(scanner._protected_names)
    assert "user_identity" in src, "the owner's own name is not covered"
    assert "owner handle" in src, "the login form of the name is not covered"
    assert 'len(full) < 8' in src, "a bare first name would be guarded and is too generic"
