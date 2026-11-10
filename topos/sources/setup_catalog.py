"""The sources a person can turn on from a terminal, by the id the registry and the app use.

``topos-node sources list|preview|enable`` and the owner-socket doors behind them read this one table.
It answers two questions and nothing else: *which word means which source* (the registry id, the name
the app shows, and the names people actually type, so a setup prompt can carry a line like
``- [x] iMessage Sync`` or ``- [x] imessage`` and both land on the same source), and *how is that
source turned on on this computer*:

``coding_history``
    The coding tool's own transcripts (``ingestion/ai_tool_sync.py``). The owner's own files read into
    the owner's own node: no confirmation beyond the tool's own approval.
``messages``
    iMessage and Signal Desktop, read on this computer (``ingestion/messenger_setup.py``). Other
    people's words, so turning one on needs a yes from the person on a terminal, or the Topos app.
``extension``
    A browser extension that writes into this node as a local pusher (``local_pushers.py``). The node
    cannot install an extension; turning the source on means the person adds the extension and runs
    ``topos-node pushers add`` once. The catalog names the app and the sources it writes so that
    command is the same one every time.

Data and string matching only, so the CLI can resolve a word before it talks to the node.
``tests/sources/test_setup_catalog.py`` pins every id here to the registry and every registry
display name to its id.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

KIND_CODING_HISTORY = "coding_history"
KIND_MESSAGES = "messages"
KIND_EXTENSION = "extension"

MESSENGER_SOURCE_IDS: Tuple[str, ...] = ("imessage", "signal")


@dataclass(frozen=True)
class SetupSource:
    """One source as the setup commands see it."""

    source_id: str
    name: str
    kind: str
    #: What turning it on reads, in one plain sentence the consent question and ``sources list`` show.
    reads: str
    #: Other words for it: the registry display name, the app's name, what a prompt's checkbox says.
    aliases: Tuple[str, ...] = ()
    #: ``extension`` only: the pusher app (``local_pushers.PUSHER_DISPLAY_NAMES``), the name the
    #: extension goes by in the browser, every source that app writes (enrolled together, one key),
    #: and where the person gets the extension.
    app: Optional[str] = None
    app_name: Optional[str] = None
    app_sources: Tuple[str, ...] = ()
    install_url: Optional[str] = None

    def public(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "source_id": self.source_id, "name": self.name, "kind": self.kind, "reads": self.reads,
            "aliases": list(self.aliases),
        }
        if self.kind == KIND_EXTENSION:
            out.update({"app": self.app, "app_name": self.app_name, "app_sources": list(self.app_sources),
                        "install_url": self.install_url})
        return out


_BROWSER_READS = "The pages you visit and what you do on them, sent by the Dialogues Browser History extension as you browse."

CATALOG: Tuple[SetupSource, ...] = (
    SetupSource(
        "claude_code_sessions", "Claude Code", KIND_CODING_HISTORY,
        "What you typed in Claude Code on this computer and its replies.",
        aliases=("Claude Code history", "Claude Code sessions", "claude-code"),
    ),
    SetupSource(
        "codex_sessions", "Codex", KIND_CODING_HISTORY,
        "What you typed in Codex on this computer and its replies.",
        aliases=("Codex history", "Codex sessions"),
    ),
    SetupSource(
        "cursor_sessions", "Cursor", KIND_CODING_HISTORY,
        "What you typed in Cursor on this computer and its replies.",
        aliases=("Cursor history", "Cursor sessions"),
    ),
    SetupSource(
        "imessage", "iMessage", KIND_MESSAGES,
        "Your Messages history on this Mac: what you sent and what other people sent you.",
        aliases=("iMessage Sync", "Messages", "Apple Messages", "iMessages"),
    ),
    SetupSource(
        "signal", "Signal Desktop", KIND_MESSAGES,
        "Your Signal Desktop history on this computer: what you sent and what other people sent you.",
        aliases=("Signal", "Signal Sync", "Signal messages"),
    ),
    SetupSource(
        "browser_visits", "Browser Visits", KIND_EXTENSION, _BROWSER_READS,
        aliases=("Browser History", "Browser History Plugin", "Dialogues Browser History",
                 "browser-history-plugin", "Browsing history"),
        app="browser-history-plugin", app_name="Dialogues Browser History",
        app_sources=("browser_visits", "browser_events"),
        install_url="https://github.com/dialoguesai/browser-history-plugin",
    ),
    SetupSource(
        "browser_events", "Browser Events", KIND_EXTENSION, _BROWSER_READS,
        app="browser-history-plugin", app_name="Dialogues Browser History",
        app_sources=("browser_visits", "browser_events"),
        install_url="https://github.com/dialoguesai/browser-history-plugin",
    ),
    SetupSource(
        "chatgpt_ui_conversation", "ChatGPT UI Conversation", KIND_EXTENSION,
        "Your ChatGPT web chats, copied by the ChatGPT Shadow extension as they happen.",
        aliases=("ChatGPT Shadow", "ChatGPT capture", "chatgpt-shadow-extension", "ChatGPT live capture",
                 "ChatGPT"),
        app="chatgpt-shadow-extension", app_name="ChatGPT Shadow", app_sources=("chatgpt_ui_conversation",),
        install_url="https://github.com/dialoguesai/chatgpt-shadow-extension",
    ),
)

BY_ID: Dict[str, SetupSource] = {entry.source_id: entry for entry in CATALOG}

#: Words a checkbox label adds that are not part of the name: "iMessage Sync", "Browser History Plugin".
_FILLER = ("sync", "plugin", "extension", "source", "history", "sessions")


def _key(word: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(word or "").lower())


def _index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for entry in CATALOG:
        for word in (entry.source_id, entry.name, *entry.aliases):
            index.setdefault(_key(word), entry.source_id)
    return index


_INDEX = _index()


def resolve(word: str) -> Optional[SetupSource]:
    """The source a word names: a registry id, a display name or a common name, whatever its case,
    spacing or punctuation ("iMessage Sync", "imessage", "Signal-Desktop"). None when nothing matches;
    a word is never guessed at."""
    key = _key(word)
    if not key:
        return None
    found = _INDEX.get(key)
    if found is None:
        for filler in _FILLER:
            if key.endswith(filler) and len(key) > len(filler):
                found = _INDEX.get(key[: -len(filler)])
                if found is not None:
                    break
    return BY_ID.get(found) if found else None


def kind_of(source_id: str) -> Optional[str]:
    entry = BY_ID.get(str(source_id or "").strip())
    return entry.kind if entry else None
