"""The kinds a share can carry, in the one order every reader uses (A2A-3 §5.4), and the kinds this node releases.

A share's kinds are read off its signed policy (``kinds_of``), never off a stored row. Page words map one to one
(A2A-5 §4.2): ``messages`` is a ``message`` from ``conversation_messages``, ``ai_chats`` a ``message`` from
``ai_chat_messages``, ``journal_entries`` a ``journal_entry``, ``interests`` an ``interest``, ``goals`` a ``goal``,
``relationships`` a ``relationship`` and ``facts`` a ``fact`` (older shares only: BL-10).

``released_kinds`` is what this node would release once bound, from its switches (``switches``, decision D5): the
switch value a bound node reads, whether or not this node is bound yet, so the catalog an owner sees before setup
names the kinds setup will offer. Never ``facts``: the stated fact kind has no switch of its own (BL-10, N1 Q4), so a
node can never say it is off, and 1.5.0 offers none.
"""
from __future__ import annotations

from . import switches

KINDS = ("messages", "ai_chats", "journal_entries", "interests", "goals", "relationships", "facts")

#: kind -> (the result type a knowledge policy signs, the canonical table its items come from, or None)
RESULT_TYPES = {"messages": "message", "ai_chats": "message", "journal_entries": "journal_entry",
                "interests": "interest", "goals": "goal", "relationships": "relationship", "facts": "fact"}
TABLES = {"messages": "conversation_messages", "ai_chats": "ai_chat_messages", "journal_entries": "journal_entries",
          "interests": "activity_events"}


def kinds_of(policy) -> list[str]:
    """The kinds a signed (or compiled) policy carries, in ``KINDS`` order (A2A-3 §5.4)."""
    from .search_contract import CAPABILITY_KNOWLEDGE_SEARCH
    search = getattr(policy, "search", None)
    if search is None:
        return []
    tables = set(getattr(search, "tables", None) or ())
    knowledge = policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH
    signed = set(getattr(search, "result_types", None) or ()) if knowledge else set()
    found = []
    for kind in KINDS:
        if kind in ("messages", "ai_chats"):
            if TABLES[kind] in tables and (not knowledge or "message" in signed):
                found.append(kind)
        elif knowledge and RESULT_TYPES[kind] in signed:
            found.append(kind)
    return found


def _when_bound(switch, env=None) -> bool:
    found = switches.explicit(switch, env)
    return bool(switch.bound if found is None else found)


def released_kinds(env=None) -> list[str]:
    """The kinds this node releases when bound, in ``KINDS`` order. Empty when sharing or search is switched off.

    Messages and AI chats are on whenever search is (N1); journal entries and browsing interests follow their kind
    switches; goals come with search; relationships need the owner's "this is me" (identity confirmations), which
    is what makes a relationship's subject the owner. Facts never (module docstring)."""
    if not _when_bound(switches.ENABLED, env) or not _when_bound(switches.MESSAGE_SEARCH, env):
        return []
    found = ["messages", "ai_chats"]
    if _when_bound(switches.JOURNAL_SOURCES, env):
        found.append("journal_entries")
    if _when_bound(switches.INTEREST_SOURCES, env):
        found.append("interests")
    found.append("goals")
    if _when_bound(switches.IDENTITY_ATTESTATIONS, env):
        found.append("relationships")
    return found
