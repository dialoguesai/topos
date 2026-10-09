"""Review dependencies of a published knowledge-search snapshot (BL-155).

The whole-store digest remains the rollback/integrity check and the freshness
signal. Serving safety binds only the reviews an indexed member can consume,
including the absence of an owner correction. Bindings live inside the existing
AES-GCM sealed member, never in plaintext metadata or recipient output.
"""
from __future__ import annotations

from .canonical import digest
from .evidence import EvidenceIdentity

VERSION = "member-review-guard/v1"
TABLES = frozenset({"conversation_messages", "ai_chat_messages", "journal_entries"})


def keys_for(member: dict) -> set[str]:
    from .automatic_message_review import machine_key
    from .message_evidence import message_key

    identities = [member["message"]] if "message" in member else []
    identities.extend(context["identity"] for context in member.get("classification_contexts", []))
    identities.extend({"binding": member['message']['binding'],
                       "dataset_kind": ('row_dataset' if dependency['table'] == 'conversation_messages'
                                        else 'node_resource'), **{key: dependency[key]
                      for key in ("table", "record_id", "source_id", "dataset_id")}}
                      for dependency in member.get("entity_dependencies", []) if dependency["table"] in TABLES)
    keys = set()
    projection = member.get('projection')
    if projection and projection['table'] == 'signal_objects':
        # A fact's explicit owner review can veto the projection independently
        # of its supporting messages' classifications.
        keys.add(projection['record_id'])
    for raw in identities:
        identity = EvidenceIdentity.parse(raw)
        if identity.table in TABLES:
            keys.update((message_key(identity), machine_key(identity)))
    return keys


def bindings_for(member: dict, frozen) -> list[dict]:
    return [{"key": key, "revision": revision(frozen.reviews.get(key))} for key in sorted(keys_for(member))]


def revision(review) -> str | None:
    return digest(review.model_dump()) if review is not None else None


def current(member: dict, reviews, db, checked: dict) -> bool:
    bindings = member.get("review_bindings")
    if not isinstance(bindings, list):
        return False
    expected = keys_for(member)
    found = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {"key", "revision"}:
            return False
        key = binding["key"]
        if not isinstance(key, str) or key not in expected or key in found:
            return False
        found.add(key)
        if key not in checked:
            checked[key] = revision(reviews._current_in(db, key))
        if checked[key] != binding["revision"]:
            return False
    return found == expected


def opt_out_revision(opt_outs) -> str:
    # Any deselection change remains a hard invalidation: sibling/dependency
    # floors can reach farther than the ranked member itself.
    return digest(sorted(opt_outs))
