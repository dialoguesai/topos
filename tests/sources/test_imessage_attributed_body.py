"""iMessage `attributedBody` must be decoded, never scraped.

When an iMessage carries no plain `text`, its body lives in the `attributedBody`
column as an `NSAttributedString` archive -- either NSArchiver's "streamtyped"
format or an NSKeyedArchiver "bplist00". The reader used to pull the longest
printable byte run out of those archives and store that. On the owner's node on
2026-08-28 that had written, across 7,602 iMessage rows:

  * 1,283 rows whose entire body was the literal string `streamtyped`
  * 459 rows holding a class-table crumb (`Z$classnameX$classesWNSValue...`)
  * 1,980 rows of real text with the archive's length byte still glued to the
    front (`'++can you send me that link again'`) -- invisible to any blob-shaped search,
    and the reason the true damage was 49% and not the 23% the blobs showed

These tests pin the decode against blobs Apple's own archivers produced, so a
regression here fails on bytes rather than on our belief about the format.
"""

import pytest

from tests.fixtures.imessage.attributed_body_blobs import ATTRIBUTED_BODY_FIXTURES
from topos.ingestion.sources.imessage_reader import (
    _build_content_from_row,
    _extract_text_from_attributed_body,
)

TYPEDSTREAM_HEADER = b"\x04\x0bstreamtyped"


@pytest.mark.parametrize("name", sorted(ATTRIBUTED_BODY_FIXTURES))
def test_apple_archives_decode_to_their_original_text(name):
    """Every blob returns the exact string it was built from -- no more, no less."""
    blob, expected = ATTRIBUTED_BODY_FIXTURES[name]
    assert _extract_text_from_attributed_body(blob) == expected


@pytest.mark.parametrize(
    "name",
    ["typedstream_len126", "typedstream_len127", "typedstream_len128",
     "typedstream_len255", "typedstream_len256", "typedstream_len65535",
     "typedstream_len65536"],
)
def test_length_prefix_never_leaks_into_the_text(name):
    """The length byte is structure, not content.

    typedstream writes a body's length as a bare byte below 127 and as a tagged
    2- or 4-byte integer above it. A message of 32..126 bytes therefore carries a
    *printable* length byte, which the old scraper kept: 1,980 live rows began
    with `+` and a stray character. The boundaries on either side of each integer
    width are the cases that catch a decoder reading the wrong number of bytes.
    """
    blob, expected = ATTRIBUTED_BODY_FIXTURES[name]
    decoded = _extract_text_from_attributed_body(blob)
    assert decoded == expected
    assert set(decoded) == {"a"}, f"non-body bytes leaked in: {decoded[:20]!r}"


def test_archive_marker_words_in_a_real_message_survive():
    """A message that talks *about* the format is still that message."""
    for name in ("typedstream_selfref", "typedstream_classref"):
        blob, expected = ATTRIBUTED_BODY_FIXTURES[name]
        assert _extract_text_from_attributed_body(blob) == expected


def test_attribute_keys_are_never_mistaken_for_the_body():
    """`__kIM...` keys sit in the same archive and must not win.

    The backing string is the first byte array after the NSString class chain;
    every later one is an attribute key. A blob with three attribute runs is the
    case where "longest run" and "first run" disagree.
    """
    blob, expected = ATTRIBUTED_BODY_FIXTURES["typedstream_multirun"]
    decoded = _extract_text_from_attributed_body(blob)
    assert decoded == expected
    assert "__kIM" not in decoded


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        TYPEDSTREAM_HEADER,
        TYPEDSTREAM_HEADER + bytes(range(256)),
        # length that runs past the end of the buffer
        TYPEDSTREAM_HEADER + b"NSString\x01\x95\x84\x01\x2b\x83" + (10**9).to_bytes(8, "little"),
        TYPEDSTREAM_HEADER + b"NSString\x01\x95\x84\x01\x2b\xff",  # negative length
        b"bplist00",
        b"plain text that is not an archive at all",
    ],
)
def test_undecodable_input_yields_nothing_rather_than_bytes(blob):
    """A body we cannot read is absent, not garbage.

    Returning None lets `_build_content_from_row` fall through to `[attachment]`
    or `[reaction:N]`. Returning a best guess is what put binary into the
    embedding index and onto the person card.
    """
    assert _extract_text_from_attributed_body(blob) is None


def test_invalid_utf8_is_not_reinterpreted_as_another_encoding():
    """Archive bytes decoded as UTF-16 make plausible-looking CJK out of noise."""
    blob = TYPEDSTREAM_HEADER + b"NSString\x01\x95\x84\x01\x2b\x04\xff\xfe\xfd\xfc"
    assert _extract_text_from_attributed_body(blob) is None


def test_plain_text_column_still_wins_over_the_archive():
    blob, _ = ATTRIBUTED_BODY_FIXTURES["typedstream_plain"]
    row = {"text": "sent as plain text", "attributed_body": blob}
    assert _build_content_from_row(row) == "sent as plain text"


def test_attachment_only_body_becomes_the_attachment_marker():
    """U+FFFC alone is a placeholder for a photo, not a message."""
    blob, _ = ATTRIBUTED_BODY_FIXTURES["typedstream_attachment"]
    row = {"text": "", "attributed_body": blob, "cache_has_attachments": 1}
    assert _build_content_from_row(row) == "[attachment]"


def test_caption_beside_an_attachment_is_kept():
    """A photo with words attached is the words; the placeholder is dropped."""
    blob, _ = ATTRIBUTED_BODY_FIXTURES["typedstream_mixed"]
    row = {"text": "", "attributed_body": blob, "cache_has_attachments": 1}
    assert _build_content_from_row(row) == "look at this photo"


def test_multiline_bodies_keep_their_line_breaks():
    """The old normalizer collapsed whitespace, flattening every paragraph."""
    blob, expected = ATTRIBUTED_BODY_FIXTURES["typedstream_multiline"]
    assert _extract_text_from_attributed_body(blob) == expected
    assert "\n" in expected
