"""``permissions_v2_share_counts`` (A2A-3 §7.2; A2A-5 §4.2–§4.3; N4): what a share would cover now, as counts only.

For a compiled policy (a draft's, or a stored share's; never signed, stored or activated here), every item of the
policy's kinds, from its chosen sources, inside its window, is counted once, under the first of these that applies
(A2A-3 §7.2's order):

1. **not counted at all** (A2A-5 §4.4, "never shared"): not the owner's own words (a message someone else sent, an
   AI reply, quoted or forwarded text, a row a door wrote for anyone but the owner, a copy of text that also appears
   elsewhere), anything about an Off-limits person or thing, items the owner keeps to themselves (owner-only, a
   backing fact whose disclosure cannot be shared, an excluded record), private-window browsing, and what the NSFW
   rule withholds;
2. ``not_proven_yours``: no proof that the owner wrote it (native provenance, a capture proof, the journal's door or
   receipt);
3. ``could_not_check``: too long for the knowledge door (over 8,000 characters), empty or unreadable, or labels the
   checker could not settle;
4. ``you_held_back``: the owner opted it out, kept a fact it backs to themselves by deselecting it, closed a fact
   naming it, or reviewed it as not to share;
5. ``not_checked_yet``: no current owner or machine review;
6. ``highly_sensitive``: labelled highly sensitive, and no rule of the policy admits that level;
7. ``outside_your_choices``: the policy's own decision denies it (topics, levels, an excluded topic, a kind it does
   not sign, the open month under a grant that releases no dates);
8. ``can_share`` otherwise.

**How.** The node's own functions decide, so a count cannot drift from what a search would release:
``qualify_automatic_message`` (``qualify_message`` for an owner-reviewed p2c-v2 policy), ``release.
source_message_decision``, ``knowledge_projections`` for goals, relationships and facts, and ``interest_index`` and
``interest_family`` for interests, all on one read snapshot with the owner's decisions frozen as an index build
freezes them. Their own order differs from the contract's in one place: the node asks for proof before it asks
whose words a message is, so a message someone else sent fails proof first. The never-shared checks therefore run
first and on their own: the row's own fields (NSFW, its door, its sender or role, quote and forward metadata), the
owner's floors (``message_evidence._floors``: owner-only, exclusions, Off-limits over the row and every fact naming
it, backing facts kept private) and the copy rule. After that the node's first refusal decides, mapped by
``REFUSALS``; the knowledge door's 8,000-character limit applies to anything proven.

A goal, relationship or fact counts under its own kind: discovered as an index build discovers it, from the rows in
the window, and ``can_share`` exactly when ``qualify_projection`` admits it under the policy. A refused one counts
under the first class of the rows it cites when one of them is not shareable, else under its own refusal.

Counts only: nothing in the reply names an item, a source row, a label, a person or a reason text.
"""
from __future__ import annotations

from collections import Counter

from .canonical import PolicyError, digest

VERSION = "topos-share-counts/v1"
NEVER = "never"
CAN_SHARE = "can_share"
NOT_PROVEN = "not_proven_yours"
COULD_NOT_CHECK = "could_not_check"
HELD_BACK = "you_held_back"
NOT_CHECKED = "not_checked_yet"
HIGHLY_SENSITIVE = "highly_sensitive"
OUTSIDE = "outside_your_choices"
#: The reply's held-back reasons, in A2A-3 §7.2's order.
REASONS = (HIGHLY_SENSITIVE, OUTSIDE, NOT_CHECKED, NOT_PROVEN, HELD_BACK, COULD_NOT_CHECK)
#: First applies first.
ORDER = (NEVER, NOT_PROVEN, COULD_NOT_CHECK, HELD_BACK, NOT_CHECKED, HIGHLY_SENSITIVE, OUTSIDE, CAN_SHARE)
#: Classes that mean the row was proven the owner's; the size limit and labels only matter for these.
AFTER_PROOF = frozenset({HELD_BACK, NOT_CHECKED, HIGHLY_SENSITIVE, OUTSIDE, CAN_SHARE})
#: The knowledge door's limit on one released message or journal entry (``search_release._accept``).
KNOWLEDGE_MAX_CHARS = 8000
MESSAGE_TABLES = ("conversation_messages", "ai_chat_messages", "journal_entries")
#: Metadata that makes a message row not the owner's original wording (``message_evidence._source_checks``).
QUOTE_FIELDS = ("is_forwarded", "forwarded_from", "quoted_message", "quoted_text", "quote", "quoted_message_id",
                "quoted_sender", "is_quoted", "quoteText", "quoteBody", "quoteAuthor", "quoteAuthorAci",
                "quoteAuthorUuid", "quoteId", "quotedMessageId", "storyReplyContext", "associated_message_guid",
                "associated_message_type")
#: The node's refusal codes on these paths -> their class. A code not listed is ``could_not_check``: a refusal this
#: map does not know is still a refusal, never ``can_share``.
REFUSALS = {
    # whose words, Off-limits, owner-only, exclusions, copies (normally caught by the never-shared checks first)
    "not_original_message": NEVER, "independent_copy_lineage": NEVER, "journal_copy_alias": NEVER,
    "ai_chat_capture_writer_refused": NEVER, "owner_only": NEVER, "intelligence_excluded": NEVER,
    "entity_protected": NEVER, "protected_content_present": NEVER, "inferred_value_protected": NEVER,
    # proof
    "native_owner_provenance_unavailable": NOT_PROVEN, "journal_owner_unproven": NOT_PROVEN,
    "provenance_unlinked": NOT_PROVEN, "provenance_link_invalid": NOT_PROVEN, "source_posture_unknown": NOT_PROVEN,
    "ai_chat_capture_unattested": NOT_PROVEN, "not_owner_authored": NOT_PROVEN, "evidence_owner_binding": NOT_PROVEN,
    "relationship_subject_unknown": NOT_PROVEN, "fact_subject_unattested": NOT_PROVEN,
    "interest_source_unproven": NOT_PROVEN,
    # unreadable, too long, unsettled labels, engineering refusals
    "unsupported_message_content": COULD_NOT_CHECK, "evidence_content_unknown": COULD_NOT_CHECK,
    "classification_unknown_or_mixed": COULD_NOT_CHECK, "protected_content_unresolved": COULD_NOT_CHECK,
    "message_context_too_large": COULD_NOT_CHECK, "message_context_unavailable": COULD_NOT_CHECK,
    "message_protection_too_large": COULD_NOT_CHECK, "entity_exclusion_lineage_unavailable": COULD_NOT_CHECK,
    # the owner's own decisions
    "owner_opted_out": HELD_BACK, "evidence_deleted": HELD_BACK,
    # no current review
    "machine_review_required": NOT_CHECKED, "message_review_required": NOT_CHECKED, "review_stale": NOT_CHECKED,
    # the policy's own decision, a kind or a time it does not release
    "evidence_not_permitted": OUTSIDE, "journal_citation_needs_record_option": OUTSIDE,
    "result_type_excluded": OUTSIDE, "evidence_outside_window": NEVER, "outside_window": NEVER,
}
#: Label refusals the owner's own correction can cause: under an owner review they are the owner holding it back.
OWNER_LABEL_REFUSALS = frozenset({"not_original_message", "independent_copy_lineage", "protected_content_unresolved",
                                  "classification_unknown_or_mixed"})


def _zero() -> dict:
    return {CAN_SHARE: 0, "held_back": {reason: 0 for reason in REASONS}}


def chosen_sources(policy) -> set:
    """The sources the policy's permit rules may draw on: what "its chosen sources" means for a compiled draft."""
    from .contract import Only
    found = set()
    for rule in policy.rules:
        if rule.effect != "permit":
            continue
        selection = rule.evidence_use.sources
        found |= set(selection.values if isinstance(selection, Only) else policy.source_universe.source_ids)
    return found


def admits_special(policy) -> bool:
    """Whether any permit rule could release an item labelled highly sensitive ("the policy includes that level").

    Each rule's predicates are evaluated with the sensitivity known and every other attribute unknown. Kleene logic
    makes a rule ``False`` there only when no topic, role or subject could make it admit ``special``."""
    from .contract import evaluate_predicate
    attributes = {"domain": None, "actor_role": None, "subject": None, "sensitivity": ["special"]}
    for rule in policy.rules:
        if rule.effect != "permit":
            continue
        values = (evaluate_predicate(rule.evidence_use.predicate, attributes),
                  evaluate_predicate(rule.release.predicate, attributes))
        if False not in values:
            return True
    return False


def freeze(resolver, reviews):
    """The owner's decisions, read once under the gate, as an index build freezes them (``SearchIndexService._freeze``)."""
    from topos.storage.db.write_gate import with_db_write
    with with_db_write():
        if reviews.binding != resolver.binding or reviews.canonical_file_revision != resolver._file_revision():
            raise PolicyError("review_database_binding")
        with resolver._read() as (conn, _floor):
            reviews._observe_clock(conn)
            with reviews._db() as review_db:
                return reviews.freeze(review_db)


class _Walk:
    """One count: the snapshot, the frozen decisions, and every row's class once."""

    def __init__(self, resolver, conn, floor, frozen, policy, *, now: int):
        from .search_contract import CAPABILITY_KNOWLEDGE_SEARCH
        self.resolver, self.conn, self.floor, self.frozen, self.policy = resolver, conn, floor, frozen, policy
        self.now = now
        self.knowledge = policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH
        self.lower_us = (now - policy.search.window.max_age_seconds) * 1_000_000
        self.upper_us = now * 1_000_000
        self.sources = chosen_sources(policy)
        self.tables = set(policy.search.tables)
        self.special = admits_special(policy)
        self.boundary = resolver.entity_boundary(conn)
        self.file_revision = resolver._file_revision()
        self.classes: dict = {}          # (table, record_id, source_id) -> class, computed on first ask
        self.raw: dict = {}              # (table, record_id, source_id) -> the row as stored, for every row in scope

    # -- which rows ----------------------------------------------------------------------------------------------

    def rows(self, table: str) -> list:
        """(identity, row) of every row of ``table`` from a chosen source inside the window, as a build reads time."""
        from .evidence_families import family, within
        from .fact_eligibility import canonical_utc_microseconds
        from .reconciliation_provenance import native_time_within
        if table not in self.tables or not self.sources:
            return []
        if table == "journal_entries" and not family(table).enabled():
            return []
        found = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if found is None:
            return []
        id_column = "entry_id" if table == "journal_entries" else "message_id"
        marks = ",".join("?" for _ in self.sources)
        out = []
        for raw in self.conn.execute(f"SELECT * FROM {table} WHERE source_id IN ({marks}) ORDER BY {id_column}",
                                     sorted(self.sources)).fetchall():
            row = dict(raw)
            if table == "journal_entries":
                if not within(table, row, self.lower_us, self.upper_us):
                    continue
            else:
                event_us = canonical_utc_microseconds(row.get("event_at"))
                if (event_us is None or not self.lower_us <= event_us <= self.upper_us
                        or not native_time_within(row, self.lower_us, self.upper_us)):
                    continue
            try:
                identity = self.resolver._identity(table, row.get(id_column), row.get("source_id"),
                                                   row.get("dataset_id") if table == "conversation_messages" else None)
            except PolicyError:
                identity = None
            else:
                self.raw[(table, identity.record_id, identity.source_id)] = row
            out.append((identity, row))
        return out

    # -- one row -------------------------------------------------------------------------------------------------

    def row_class(self, identity) -> str:
        """The class of one row in scope (``rows`` found it), decided once."""
        if identity is None:
            return COULD_NOT_CHECK
        key = (identity.table, identity.record_id, identity.source_id)
        if key not in self.classes:
            self.classes[key] = self._row_class(identity, self.raw[key])
        return self.classes[key]

    def _row_class(self, identity, row) -> str:
        """The contract's first class for one row, from the row as stored (the evidence loader itself refuses a row
        it cannot bind to the owner, so whose words a row is must be read before it is asked).

        1. The row's own fields that make it never shared (no proof can change them).
        2. The node's qualification, in the node's own order, mapped to a class.
        3. Only when that class could hide a never-shared row (the node asks for proof and content before the
           owner's floors, and for opt-outs before Off-limits): the floors and the copy rule on their own. A row the
           node qualified, or found unreviewed or outside the policy, passed them already.
        4. The knowledge door's size limit for anything proven.
        """
        if self._never_by_fields(identity, row):
            return NEVER
        found = self._qualified_class(identity)
        if found in (NOT_PROVEN, COULD_NOT_CHECK, HELD_BACK) and self._never_by_floors(identity, row):
            return NEVER
        content = row.get("content")
        if found in AFTER_PROOF and self.knowledge and isinstance(content, str) and len(content) > KNOWLEDGE_MAX_CHARS:
            return COULD_NOT_CHECK
        return found

    def _never_by_fields(self, identity, row) -> bool:
        """Never shared by the row's own fields: NSFW, a door other than the owner's, someone else's message, an AI
        reply, quoted or forwarded text."""
        from topos.disclosure.content_policy import is_record_nsfw
        from topos.features.provenance.writer_class import OWNER_WRITER_CLASSES, normalize_writer_class
        from .ai_chat_capture import USER_ROLES
        from .evidence import _json
        table = identity.table
        if is_record_nsfw(row):
            return True
        writer = normalize_writer_class(row.get("writer_class"))
        if writer is not None and writer not in OWNER_WRITER_CLASSES:
            return True
        if table == "conversation_messages" and row.get("is_from_self") == 0:
            return True
        sender = row.get("sender_type")
        if table == "ai_chat_messages" and isinstance(sender, str) and sender.strip() and sender not in USER_ROLES:
            return True
        if table != "journal_entries" and row.get("metadata_json") not in (None, ""):
            try:
                metadata = _json(row["metadata_json"], dict)
            except PolicyError:
                metadata = {}
            if any(metadata.get(field) not in (None, False, 0, "", [], {}) for field in QUOTE_FIELDS):
                return True
        return False

    def _never_by_floors(self, identity, row) -> bool:
        """Never shared by the owner's floors (owner-only, exclusions, Off-limits over the row and every fact naming
        it, a backing fact whose disclosure cannot be shared) or the copy rule, run exactly as qualification runs
        them but on their own, with no opt-out (an opt-out is the owner holding it back, a later class)."""
        from .evidence import EvidenceRevision, _key, _row_revision
        from .message_evidence import _floors
        from .message_review_contract import MessageSnapshot
        snapshot = MessageSnapshot(binding=self.resolver.binding, canonical_file_revision=self.file_revision,
                                   message=EvidenceRevision(identity=identity,
                                                            revision=_row_revision(row, table=identity.table)),
                                   protection_revision=digest({"share_counts": "floors only"}))
        try:
            _floors(self.resolver, self.conn, snapshot, {_key(identity): row}, frozenset())
        except PolicyError as exc:
            if REFUSALS.get(exc.code) == NEVER:
                return True
        try:
            return bool(self.resolver._known_copies(self.conn, identity, row))
        except PolicyError as exc:
            return REFUSALS.get(exc.code) == NEVER

    def _qualified_class(self, identity) -> str:
        from .message_evidence import OwnerMessageReview, message_key, qualify_automatic_message, qualify_message
        from .release import source_message_decision
        owner_reviewed = isinstance(self.frozen._current_in(None, message_key(identity)), OwnerMessageReview)
        qualify = qualify_automatic_message if self.knowledge else qualify_message
        try:
            qualified, _rows = qualify(self.resolver, self.conn, self.floor, identity, self.frozen, None)
            decision = source_message_decision(self.policy, qualified)
        except PolicyError as exc:
            if owner_reviewed and exc.code in OWNER_LABEL_REFUSALS:
                return HELD_BACK
            if exc.code == "protected_content_unresolved" and self._machine_says_present(identity):
                return NEVER
            return REFUSALS.get(exc.code, COULD_NOT_CHECK)
        return self._decided(decision.verdict, qualified.classifications[0].sensitivity)

    def _machine_says_present(self, identity) -> bool:
        from .automatic_message_review import MachineMessageReview, machine_key
        review = self.frozen._current_in(None, machine_key(identity))
        return isinstance(review, MachineMessageReview) and review.classifications[0].protected_content == "present"

    def _decided(self, verdict: str, sensitivity: str) -> str:
        if verdict == "permit":
            return CAN_SHARE
        if sensitivity == "special" and not self.special:
            return HIGHLY_SENSITIVE
        if verdict == "deny":
            return OUTSIDE
        return COULD_NOT_CHECK              # indeterminate: a label the rules could not read

    # -- goals, relationships, facts ------------------------------------------------------------------------------

    def projection_classes(self, identities, result_types) -> Counter:
        """kind -> Counter of classes for every goal, relationship and fact the rows in scope name."""
        from .knowledge_projections import candidates, qualify_projection
        kinds = {"user_goals": "goal", "entity_edges": "relationship", "signal_objects": "fact"}
        found = {kind: Counter() for kind in ("goal", "relationship", "fact")}
        wanted = [kind for kind in ("fact", "goal", "relationship") if kind in result_types]
        if not wanted or not identities:
            return found
        for table, record_id in sorted(set(candidates(self.conn, identities, wanted))):
            kind = kinds[table]
            try:
                projected = qualify_projection(self.resolver, self.conn, self.floor, self.frozen, None, table,
                                               record_id, self.policy, self.lower_us, self.upper_us)
            except PolicyError as exc:
                found[kind][self._projection_refusal(table, record_id, exc.code)] += 1
                continue
            found[kind][CAN_SHARE if projected.kind in result_types else OUTSIDE] += 1
        return found

    def _projection_refusal(self, table: str, record_id: str, code: str) -> str:
        cited = [self.row_class(identity) for identity in self._cited(table, record_id)]
        blocking = [found for found in cited if found != CAN_SHARE]
        if blocking:
            return min(blocking, key=ORDER.index)
        return REFUSALS.get(code, COULD_NOT_CHECK)

    def _cited(self, table: str, record_id: str) -> list:
        """The evidence identities a goal, relationship or fact cites, resolved as its projection resolves them; the
        ones outside the window or the chosen sources are left out (they decide nothing here)."""
        from .evidence import _json
        from .knowledge_projections import GOAL_TABLES, JOURNAL, _journal_enabled, load_projection_row, \
            resolve_reference
        try:
            row = load_projection_row(self.conn, table, record_id)
            if table == "entity_edges":
                return self._cited("user_goals", _json(row["metadata_json"], dict).get("source_object_id"))
            if table == "user_goals":
                refs = []
                for goal_table, id_column in GOAL_TABLES + (((JOURNAL, "entry_id"),) if _journal_enabled() else ()):
                    columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({goal_table})")}
                    if {id_column, "source_id"} <= columns and self.conn.execute(
                            f"SELECT 1 FROM {goal_table} WHERE {id_column}=? AND source_id=?",
                            (row.get("record_id"), row.get("source_id"))).fetchone():
                        refs.append(dict(table=goal_table, record_id=row["record_id"], source_id=row["source_id"]))
            else:
                refs = _json(row["source_refs_json"], list)
            identities = [resolve_reference(self.resolver, self.conn, ref) for ref in refs]
        except (PolicyError, KeyError, TypeError):
            return []
        return [identity for identity in identities
                if (identity.table, identity.record_id, identity.source_id) in self.raw]

    # -- interests -------------------------------------------------------------------------------------------------

    def interest_classes(self) -> Counter:
        """One per (topic cluster, month) wholly inside the window with enough visits to be an interest at all."""
        from . import interest_family as fam
        from . import interest_index, interest_review as ir
        found = Counter()
        if not interest_index.enabled() or not interest_index.admits(self.policy):
            return found
        owner_id = self.resolver.binding.owner_id
        result = fam.build(self.conn, owner_id=owner_id, now_us=self.upper_us, boundary=self.boundary,
                           opt_outs=self.frozen.opt_outs)
        objects = {obj.interest_id: obj for obj in result.objects}
        try:
            context_revision, _terms = ir.context(self.boundary)
        except PolicyError:
            context_revision = None
        labels_withheld = {"browsing": NEVER, "label_form": COULD_NOT_CHECK, "label_host": COULD_NOT_CHECK,
                           "label_title": COULD_NOT_CHECK, "label_person": NEVER, "excluded_label": NEVER,
                           "opted_out": HELD_BACK, "offlimits": NEVER}
        for candidate in result.candidates:
            if not fam.period_inside(period_start_us=candidate.period_start_us,
                                     period_end_us=candidate.period_end_us, now_us=self.upper_us,
                                     max_age_seconds=self.policy.search.window.max_age_seconds):
                continue
            if not candidate.qualifies("all"):
                continue                      # below the threshold: not an interest at all
            stage = next((stage for stage in fam.VISIT_CHECKS if not candidate.qualifies(stage)), None)
            if stage is not None:
                found[NOT_PROVEN if stage == "provenance" else NEVER] += 1
                continue
            if candidate.label_withheld is not None:
                found[labels_withheld.get(candidate.label_withheld, COULD_NOT_CHECK)] += 1
                continue
            obj = objects.get(fam.interest_id(candidate.cluster_id, candidate.month))
            if obj is None:
                found[COULD_NOT_CHECK] += 1
                continue
            if not obj.complete and not interest_index.open_month_allowed(self.policy):
                found[OUTSIDE] += 1           # the month is still open, and the share releases no dates
                continue
            if context_revision is None:
                found[COULD_NOT_CHECK] += 1
                continue
            assessment = ir.current(self.conn, owner_id=owner_id, obj=obj, context_revision=context_revision)
            if assessment is None:
                found[NOT_CHECKED] += 1
                continue
            labels = assessment.classification
            if labels.protected_content == "present":
                found[NEVER] += 1
            elif labels.protected_content != "none" or labels.sensitivity == "unknown":
                found[COULD_NOT_CHECK] += 1
            elif labels.sensitivity == "special":
                found[HIGHLY_SENSITIVE] += 1
            else:
                verdict, _rule = interest_index.decide(self.policy, labels)
                found[CAN_SHARE if verdict == "permit" else OUTSIDE] += 1
        return found


def count(resolver, reviews, policy, *, now: int) -> dict:
    """The reply for one compiled policy (already parsed and bound to this node by the caller)."""
    from .share_kinds import TABLES, kinds_of
    kinds = kinds_of(policy)
    result = {kind: _zero() for kind in kinds}
    frozen = freeze(resolver, reviews)
    with resolver._read(gated=False) as (conn, floor):
        walk = _Walk(resolver, conn, floor, frozen, policy, now=now)
        tallies = {kind: Counter() for kind in kinds}
        in_scope = []
        for table in ("conversation_messages", "ai_chat_messages", "journal_entries"):
            kind = next((k for k, t in TABLES.items() if t == table), None)
            rows = walk.rows(table)
            in_scope += [identity for identity, _raw in rows if identity is not None]
            if kind in tallies:
                for identity, _raw in rows:
                    tallies[kind][walk.row_class(identity)] += 1
        signed = set(getattr(policy.search, "result_types", None) or ()) if walk.knowledge else set()
        if walk.knowledge and {"goal", "relationship", "fact"} & signed:
            projected = walk.projection_classes(in_scope, signed)
            for kind, result_type in (("goals", "goal"), ("relationships", "relationship"), ("facts", "fact")):
                if kind in tallies:
                    tallies[kind] += projected[result_type]
        if "interests" in tallies:
            tallies["interests"] += walk.interest_classes()
    for kind, tally in tallies.items():
        result[kind][CAN_SHARE] = tally[CAN_SHARE]
        for reason in REASONS:
            result[kind]["held_back"][reason] = tally[reason]
    return {"version": VERSION, "as_of": int(now), "kinds": result}
