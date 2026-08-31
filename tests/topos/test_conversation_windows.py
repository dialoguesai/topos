"""Windowing chat into units worth embedding.

What these protect: a retrieval tier that can answer "what have I been working
on". Per-message embeddings cannot — the answer to that question is never
contained in one message, and the short ones ("sure, do that") embed to a vector
near every other agreeable noise in the corpus, taking a retrieval slot a real
answer could have had.

Measured on a real ChatGPT import: 1,231 message-level embeddings, 67 of them
containing the word "project", and the query "project" returned zero rows.
"""

from __future__ import annotations

from topos.features.signal.conversation_windows import (
    KIND_AI_CHAT,
    KIND_HUMAN_CHAT,
    MAX_WINDOW_CHARS,
    MIN_WINDOW_CHARS,
    build_windows,
)

LONG = "x" * 200  # comfortably past MIN_WINDOW_CHARS once rendered


def ai(role, content, mid, at="2026-07-01T10:00:00Z"):
    return {
        "sender_type": role,
        "content": content,
        "message_id": mid,
        "event_at": at,
        "conversation_id": "c1",
    }


def human(role, content, mid, at, name=None):
    msg = {
        "sender_type": role,
        "content": content,
        "message_id": mid,
        "event_at": at,
        "conversation_id": "h1",
    }
    if name:
        msg["sender_name"] = name
    return msg


# ---------------------------------------------------------------- AI chat ----


def test_a_question_and_its_answer_are_one_window():
    """The unit that motivated this. Splitting them leaves an answer with no
    question, which is exactly what made short assistant turns unretrievable."""
    windows = build_windows(
        [ai("user", "How do I stream a zip member?", "m1"), ai("assistant", LONG, "m2")],
        kind=KIND_AI_CHAT,
    )
    assert len(windows) == 1
    assert windows[0].message_ids == ["m1", "m2"]
    assert "How do I stream a zip member?" in windows[0].text
    assert windows[0].turn_count == 2


def test_a_new_question_starts_a_new_window():
    windows = build_windows(
        [
            ai("user", "First question about embeddings " + LONG, "m1"),
            ai("assistant", "First answer " + LONG, "m2"),
            ai("user", "Second, unrelated question " + LONG, "m3"),
            ai("assistant", "Second answer " + LONG, "m4"),
        ],
        kind=KIND_AI_CHAT,
    )
    assert [w.message_ids for w in windows] == [["m1", "m2"], ["m3", "m4"]]


def test_several_assistant_turns_stay_with_their_question():
    windows = build_windows(
        [
            ai("user", "Explain this " + LONG, "m1"),
            ai("assistant", "Part one " + LONG, "m2"),
            ai("assistant", "Part two " + LONG, "m3"),
        ],
        kind=KIND_AI_CHAT,
    )
    assert len(windows) == 1
    assert windows[0].message_ids == ["m1", "m2", "m3"]


def test_a_bare_acknowledgement_gets_carried_not_embedded_alone():
    """"sure, do that" is the case this whole module exists for. It must never
    become its own window; it rides with the exchange it belongs to."""
    windows = build_windows(
        [
            ai("user", "Should I stream it? " + LONG, "m1"),
            ai("assistant", "Yes " + LONG, "m2"),
            ai("user", "sure, do that", "m3"),
        ],
        kind=KIND_AI_CHAT,
    )
    # The trailing "sure, do that" is too thin to stand alone, so it is dropped
    # rather than embedded as a window of its own.
    assert all(w.message_ids != ["m3"] for w in windows)


def test_the_conversation_title_rides_along():
    """The cheapest topic label the corpus carries. Without it a window that
    says only "yes, that works" is unrecoverable."""
    windows = build_windows(
        [ai("user", "Q " + LONG, "m1"), ai("assistant", "A " + LONG, "m2")],
        kind=KIND_AI_CHAT,
        title="Streaming zip members",
    )
    assert windows[0].text.startswith("Conversation: Streaming zip members")


def test_speakers_are_labelled_so_attribution_survives_embedding():
    windows = build_windows(
        [ai("user", "My question " + LONG, "m1"), ai("assistant", "The answer " + LONG, "m2")],
        kind=KIND_AI_CHAT,
    )
    assert "Me: My question" in windows[0].text
    assert "Assistant: The answer" in windows[0].text


# ------------------------------------------------------------- human chat ----


def test_a_sitting_is_one_window():
    windows = build_windows(
        [
            human("user", "hey " + LONG, "m1", "2026-07-01T10:00:00Z"),
            human("other", "hey back " + LONG, "m2", "2026-07-01T10:02:00Z", name="Sam"),
            human("user", "one more thing " + LONG, "m3", "2026-07-01T10:05:00Z"),
        ],
        kind=KIND_HUMAN_CHAT,
    )
    assert len(windows) == 1
    assert windows[0].turn_count == 3


def test_a_long_gap_starts_a_new_window():
    """Human chat has no question/answer shape to lean on. Time is the seam."""
    windows = build_windows(
        [
            human("user", "morning thought " + LONG, "m1", "2026-07-01T09:00:00Z"),
            human("user", "evening thought " + LONG, "m2", "2026-07-01T21:00:00Z"),
        ],
        kind=KIND_HUMAN_CHAT,
    )
    assert [w.message_ids for w in windows] == [["m1"], ["m2"]]


def test_messages_close_together_are_not_split():
    windows = build_windows(
        [
            human("user", "a " + LONG, "m1", "2026-07-01T09:00:00Z"),
            human("user", "b " + LONG, "m2", "2026-07-01T09:20:00Z"),
        ],
        kind=KIND_HUMAN_CHAT,
    )
    assert len(windows) == 1


def test_the_other_party_is_named_when_we_know_them():
    windows = build_windows(
        [
            human("user", "question " + LONG, "m1", "2026-07-01T09:00:00Z"),
            human("other", "answer " + LONG, "m2", "2026-07-01T09:01:00Z", name="Sam"),
        ],
        kind=KIND_HUMAN_CHAT,
    )
    assert "Sam: answer" in windows[0].text


# ------------------------------------------------------------------ both ----


def test_a_window_stays_within_the_character_budget():
    """An unbounded window drifts across topics and embeds to their average,
    which resembles none of them."""
    huge = [ai("user", "y" * 5000, f"m{i}") for i in range(6)]
    windows = build_windows(huge, kind=KIND_AI_CHAT)
    assert windows, "expected at least one window"
    assert all(len(w.text) <= MAX_WINDOW_CHARS for w in windows)
    assert len(windows) > 1, "a 30k-char conversation must not be one window"


def test_windows_carry_their_time_span():
    windows = build_windows(
        [
            ai("user", "Q " + LONG, "m1", at="2026-07-01T10:00:00Z"),
            ai("assistant", "A " + LONG, "m2", at="2026-07-01T10:04:00Z"),
        ],
        kind=KIND_AI_CHAT,
    )
    assert windows[0].started_at.startswith("2026-07-01T10:00")
    assert windows[0].ended_at.startswith("2026-07-01T10:04")


def test_a_window_id_is_stable_so_a_rerun_overwrites():
    args = ([ai("user", "Q " + LONG, "m1"), ai("assistant", "A " + LONG, "m2")],)
    first = build_windows(*args, kind=KIND_AI_CHAT)[0].window_id
    second = build_windows(*args, kind=KIND_AI_CHAT)[0].window_id
    assert first == second == "c1:w:m1"


def test_empty_and_contentless_input_produce_nothing():
    assert build_windows([], kind=KIND_AI_CHAT) == []
    assert build_windows([ai("user", "", "m1"), ai("assistant", "   ", "m2")], kind=KIND_AI_CHAT) == []


def test_an_unknown_kind_falls_back_to_bursts():
    # Burst windowing needs no turn structure, so it cannot be wrong about one
    # that is not there.
    windows = build_windows(
        [human("user", "a " + LONG, "m1", "2026-07-01T09:00:00Z")], kind="something_else"
    )
    assert len(windows) == 1


def test_messages_without_timestamps_still_window():
    # iMessage backfills and some exports lose the stamp; losing the window too
    # would make those conversations silently unsearchable.
    windows = build_windows(
        [
            {"sender_type": "user", "content": "a " + LONG, "message_id": "m1", "conversation_id": "c9"},
            {"sender_type": "assistant", "content": "b " + LONG, "message_id": "m2", "conversation_id": "c9"},
        ],
        kind=KIND_AI_CHAT,
    )
    assert len(windows) == 1
    assert windows[0].conversation_id == "c9"
    assert windows[0].started_at is None


def test_a_thin_window_is_dropped_rather_than_embedded():
    windows = build_windows([ai("user", "ok", "m1")], kind=KIND_AI_CHAT)
    assert windows == []
    assert MIN_WINDOW_CHARS > 0
