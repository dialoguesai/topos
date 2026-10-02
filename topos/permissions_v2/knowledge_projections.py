"""Evidence-preserving projections of stored facts, goals and graph relationships.

These adapters do not grant permission. They recover unambiguous legacy source
identities, independently qualify every source, require a common permit clause,
and check the projected assertion. Unsupported inference forms stay withheld.

A source is a message or, with the journal family on (IF-5), a journal entry. A journal
entry is qualified exactly as a message is (`qualify_automatic_message`: its owner proof,
posture, the NSFW hard withhold, owner-only, exclusions, Off-limits over every column, copies),
is inside the window only by every instant its stated day can denote, and is cited as a
record: an item grounded in one releases only under a grant that signs `journal_entry`
(IF-5 §2 citation scope), otherwise `journal_citation_needs_record_option`. A goal citing a
journal entry is also grounded when it is the entry's structured goal field, verbatim, and the
field clears `journal_goal_field.refusal` (Lane H1; `TOPOS_PERMISSIONS_V2_JOURNAL_GOAL_FIELD`,
default off); every other check above still applies to it. Under a grant that releases that entry
itself, whole (`journal_entry_released`, decided per grant where the rule is asked), the field is a
paragraph the grant already releases, and the rule's guards on the text's form are set aside; under
any other grant they all apply. A fact citing exactly one journal entry that
the entry does not state releases as `assertion: "inferred"` when its value clears every guard of
`inferred_facts.refusal` (IF-6 v1; `TOPOS_PERMISSIONS_V2_DERIVED_FACTS`, default off, inert without the
journal family); with the flag off it is `fact_not_grounded`, exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3

from .canonical import PolicyError, digest
from .evidence import _json, _key
from .entity_boundary import rows_revision
from .fact_eligibility import canonical_utc_microseconds
from .identity import ATTESTED_CONTRACT, permit_subjects
from .message_evidence import qualify_automatic_message
from .native_claim_grounding import explicitly_states_claim
from .opaque_ids import opaque_record_id
from .permitted_derivation import check_lineage
from .predicate_classes import CLASSES, WIDENED, scalar
from .release import source_message_decision

MAX_SUPPORT = 20
MAX_FACTS = 5000
PREDICATE_TEXT = {'works_at':'works at','worked_at':'worked at','works_on':'works on','role_is':'has the role',
    'certified_in':'is certified in','studied_at':'studied at','skilled_in':'is skilled in',
    'prefers':'prefers','member_of':'is a member of','lives_in':'lives in','practices':'practices','training_for':'is training for'}
# OD-46: the predicates measured on permitted messages, each with its class in predicate_classes.
PREDICATE_TEXT.update({predicate: klass.text for predicate, klass in WIDENED.items()})
JOURNAL = 'journal_entries'
# The tables a goal's (record_id, source_id) may name; the journal joins only while its family exists.
GOAL_TABLES = (('conversation_messages','message_id'),('ai_chat_messages','message_id'))

# What a relationship's release and eligibility read of each column of its two graph rows, so that its revision
# (`relationship_revision`) pins exactly that.
#   pinned    the value is read: the row's identity and the checks keyed by it (exclusions, owner-only marks,
#             opt-outs, protected ids and contacts), the subject, the endpoint, the type, the validity, the
#             endpoint's names, aliases, handles, contact and self flag. A change stales the member.
#   goal_link only the JSON's `source_object_id` is read: the goal the edge releases with.
#   volatile  a graph rebuild rewrites it without changing what is released (counters, timestamps, the edge's
#             display statement, the goal node's variant list), and nothing reads it but the Off-limits scan,
#             which reads every text column. It is not pinned: the currency check runs that scan again on the
#             current rows (`current_revision` with a boundary), so its verdict, not the value, is what counts.
# A column missing here is pinned. tests/permissions_v2/test_relationship_revision.py fails until a new column of
# either table is classified.
EDGE_COLUMNS = {
    'edge_id': 'pinned', 'src_entity_id': 'pinned', 'dst_entity_id': 'pinned', 'edge_type': 'pinned',
    'valid_from': 'pinned', 'valid_to': 'pinned', 'metadata_json': 'goal_link',
    'weight': 'volatile', 'evidence_count': 'volatile', 'last_event_at': 'volatile',
    'created_at': 'volatile', 'updated_at': 'volatile'}
ENTITY_COLUMNS = {
    'entity_id': 'pinned', 'entity_type': 'pinned', 'canonical_name': 'pinned', 'normalized_name': 'pinned',
    'aliases_json': 'pinned', 'identifiers_json': 'pinned', 'contact_id': 'pinned', 'is_self': 'pinned',
    'embedding_blob': 'volatile', 'first_seen': 'volatile', 'last_seen': 'volatile', 'mention_count': 'volatile',
    'metadata_json': 'volatile', 'created_at': 'volatile', 'updated_at': 'volatile'}
RELATIONSHIP_REVISION = 'topos-relationship-revision/v2'


def _journal_enabled() -> bool:
    from .evidence_families import family
    return family(JOURNAL).enabled()


def _source_released(identity, row, precision):
    """The event time one source may release at the grant's precision, by its family's rule (IF-5 §2)."""
    if precision not in ('second','day'):
        return None
    if identity.table==JOURNAL:
        from .evidence_families import released
        return released(identity.table,row,precision)   # its stated day at `day`; at `second` only a recorded instant
    stamp=canonical_utc_microseconds(row.get('event_at'))
    if stamp is None:
        return None
    return stamp//1000000 if precision=='second' else stamp//86400000000*86400


def _source_rank_us(identity, row):
    """The time an index ranks one source by: a message's instant, a journal entry's stated day (never finer)."""
    if identity.table==JOURNAL:
        from .evidence_families import rank_time_us
        return rank_time_us(identity.table,row)
    return canonical_utc_microseconds(row.get('event_at'))


@dataclass
class Projection:
    table: str
    record_id: str
    kind: str
    content: str
    fields: dict
    sources: list
    revision: str
    allow_clause_id: str

    def output(self, *, key, grant_id, precision):
        citations, sources, times = [], set(), []
        for qualified, rows in self.sources:
            identity = qualified.snapshot.message.identity
            row = rows[_key(identity)]
            source_id = identity.source_id
            sources.add(source_id)
            # A journal entry is cited as a record (IF-5 §3): its own opaque id, its whole text, its one source.
            citations.append(dict(record_id=opaque_record_id(key,grant_id=grant_id,table=identity.table,
                source_id=source_id,dataset_id=identity.dataset_id,record_id=identity.record_id),
                source_id=source_id,content=row['content']))
            times.append(_source_released(identity,row,precision))
        # Old support cannot gain a fresh date through recent extraction: the earliest source dates the item. A
        # source that cannot be dated at this precision (a stated day at `second`) dates nothing, so neither does it.
        event = None if not times or None in times else min(times)
        return dict(kind=self.kind,record_id=opaque_record_id(key,grant_id=grant_id,table=self.table,
            source_id=None,dataset_id=None,record_id=self.record_id),content=self.content,
            source_ids=sorted(sources),citations=citations,event_at=event,**self.fields)

    def rank_time_us(self) -> int:
        """The index's rank time: the earliest source's, each by its family's rule (never later, never finer)."""
        times = [_source_rank_us(qualified.snapshot.message.identity,
                                 rows[_key(qualified.snapshot.message.identity)]) for qualified, rows in self.sources]
        if not times or None in times:
            raise PolicyError('evidence_outside_window')
        return min(times)


def _journal_entry_id(ref):
    """The entry a journal citation names (IF-5 W6): `record_id` in a fact's reference, `id` in a rule-extractor
    object's. Both present and different is ambiguous; neither, or not text, is incomplete."""
    named = [ref.get(field) for field in ('record_id','id') if ref.get(field) is not None]
    if not named or any(not isinstance(value,str) or not value for value in named):
        raise PolicyError('lineage_identity_incomplete')
    if len(set(named))>1:
        raise PolicyError('lineage_identity_ambiguous')
    return named[0]


def _resolve_journal(resolver, conn, ref):
    """A journal citation's member identity: its source fills from the one row with that id, never guessed.

    Rows of one source with identical text are one record (IF-5 §1.2): a citation of a twin resolves to the
    member, the row with the smallest (entry_at, entry_id), exactly the member `_journal_copies` keeps.
    """
    entry_id = _journal_entry_id(ref)
    if ref.get('dataset_id') is not None:
        raise PolicyError('lineage_identity_incomplete')   # a journal identity carries no dataset
    where,args = 'entry_id=?',[entry_id]
    if ref.get('source_id') is not None:
        where+=' AND source_id=?';args.append(ref['source_id'])
    try:
        matches = conn.execute(f'SELECT source_id,content FROM {JOURNAL} WHERE {where}',args).fetchmany(2)
        if len(matches)!=1:
            raise PolicyError('lineage_identity_ambiguous')
        source_id,content = matches[0]
        member = entry_id
        if isinstance(content,str):
            same = conn.execute(f'SELECT entry_at,entry_id FROM {JOURNAL} WHERE source_id=? AND content=?',
                                (source_id,content)).fetchall()
            member = min((at or '',entry) for at,entry in same)[1]
    except sqlite3.Error:
        raise PolicyError('evidence_storage_unavailable') from None
    return resolver._identity(JOURNAL,member,source_id)


def resolve_reference(resolver, conn, ref):
    """Fill absent source/dataset from exactly one canonical row, never guesses.

    This does not enroll or edit data. The subsequent qualifier must still prove
    native origin against that exact row and source. Conflicting supplied fields
    and cross-table/dataset ambiguity refuse. A journal reference resolves only
    while the journal family exists; with it off, it is unsupported as before.
    """
    if isinstance(ref,dict) and ref.get('table')==JOURNAL and _journal_enabled():
        return _resolve_journal(resolver,conn,ref)
    if not isinstance(ref,dict) or not isinstance(ref.get('record_id'),str):
        raise PolicyError('lineage_identity_incomplete')
    table = ref.get('table')
    if table not in {'conversation_messages','ai_chat_messages'}:
        raise PolicyError('lineage_unsupported')
    dataset = 'dataset_id' if table=='conversation_messages' else 'NULL'
    where,args='message_id=?',[ref['record_id']]
    if ref.get('source_id') is not None:
        where+=' AND source_id=?';args.append(ref['source_id'])
    if ref.get('dataset_id') is not None:
        if table!='conversation_messages': raise PolicyError('lineage_identity_incomplete')
        where+=' AND dataset_id=?';args.append(ref['dataset_id'])
    matches=conn.execute(f'SELECT source_id,{dataset} FROM {table} WHERE {where}',args).fetchmany(2)
    if len(matches)!=1:
        raise PolicyError('lineage_identity_ambiguous')
    return resolver._identity(table,ref['record_id'],matches[0][0],matches[0][1])


def _unrestricted(resolver,conn,reviews,review_db,table,record_id,row):
    from .exclusion_floor import exclusions
    vetoes=exclusions(conn)
    if record_id in vetoes['record'] or record_id in reviews._opt_outs_in(review_db):
        raise PolicyError('owner_opted_out')
    if table=='entities' and record_id in vetoes['entity']:
        raise PolicyError('intelligence_excluded')
    if conn.execute('SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=?',(table,record_id)).fetchone():
        raise PolicyError('owner_only')
    boundary=resolver.entity_boundary(conn)
    if boundary.legacy_veto(table,row):
        raise PolicyError('entity_protected')
    return boundary


def _journal_citation(resolver,conn,reviews,review_db,ref,identity,policy):
    """What a journal citation needs before its entry is qualified (IF-5 §2, §1.2).

    The entry is cited as a record, so the grant must sign the "Journal entries" option. When the item names a
    same-source twin rather than the member, the named row still vetoes: its own NSFW flag, deletion, owner-only
    mark, exclusion, the owner's opt-out and an Off-limits match over every column. The member is qualified next.
    """
    if 'journal_entry' not in policy.search.result_types:
        raise PolicyError('journal_citation_needs_record_option')
    cited=_journal_entry_id(ref)
    if cited==identity.record_id:
        return
    from topos.disclosure.content_policy import is_record_nsfw
    from .evidence import _deleted
    from .exclusion_floor import exclusions
    from .message_evidence import message_key
    try:
        rows=conn.execute(f'SELECT * FROM {JOURNAL} WHERE entry_id=? AND source_id=?',
                          (cited,identity.source_id)).fetchmany(2)
    except sqlite3.Error:
        raise PolicyError('evidence_storage_unavailable') from None
    if len(rows)!=1:
        raise PolicyError('lineage_identity_ambiguous')
    row=dict(rows[0])
    if is_record_nsfw(row):
        raise PolicyError('unsupported_message_content')
    if _deleted(row):
        raise PolicyError('evidence_deleted')
    if cited in exclusions(conn)['record']:
        raise PolicyError('intelligence_excluded')
    if message_key(resolver._identity(JOURNAL,cited,identity.source_id)) in reviews._opt_outs_in(review_db):
        raise PolicyError('owner_opted_out')
    if conn.execute('SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1',
                    (JOURNAL,cited)).fetchone():
        raise PolicyError('owner_only')
    resolver.entity_boundary(conn).check(table=JOURNAL,record_id=cited,source_id=identity.source_id,
                                         dataset_id=None,row=row)


def _inside(identity,row,lower_us,upper_us):
    """The window, by the source's family rule: a message's instant (and its native time), or every instant a
    journal entry's stated day can denote (IF-5 §1, `evidence_families.within`)."""
    if identity.table==JOURNAL:
        from .evidence_families import within
        if _source_rank_us(identity,row) is None:
            raise PolicyError('journal_time_unknown')
        if not within(identity.table,row,lower_us,upper_us):
            raise PolicyError('evidence_outside_window')
        return
    stamp=canonical_utc_microseconds(row.get('event_at'))
    from .reconciliation_provenance import native_time_within
    if stamp is None or not lower_us<=stamp<=upper_us or not native_time_within(row,lower_us,upper_us):
        raise PolicyError('evidence_outside_window')


def _support(resolver,conn,floor,reviews,review_db,refs,policy,lower_us,upper_us,*,extra_domains=(),extra_sensitivity='none'):
    if not isinstance(refs,list) or not 1<=len(refs)<=MAX_SUPPORT:
        raise PolicyError('lineage_identity_incomplete')
    result,seen,common=[],set(),None
    for ref in refs:
        identity=resolve_reference(resolver,conn,ref)
        if identity.table==JOURNAL:
            _journal_citation(resolver,conn,reviews,review_db,ref,identity,policy)
        if _key(identity) in seen: continue
        seen.add(_key(identity))
        qualified,rows=qualify_automatic_message(resolver,conn,floor,identity,reviews,review_db)
        row=rows[_key(identity)]
        _inside(identity,row,lower_us,upper_us)
        if identity.table==JOURNAL:
            from topos.disclosure.content_policy import is_record_nsfw
            if is_record_nsfw(row):   # the hard withhold, here too: a citation is the entry's whole text
                raise PolicyError('unsupported_message_content')
        if identity.table not in policy.search.tables or len(row['content'])>8000:
            raise PolicyError('evidence_outside_form')
        labels=qualified.classifications[0]
        ranks={'none':0,'personal':1,'special':2,'unknown':3}
        labels=labels.model_copy(update={'domains':sorted(set(labels.domains)|set(extra_domains)),
            'sensitivity':max((labels.sensitivity,extra_sensitivity),key=ranks.__getitem__)})
        checked=qualified.model_copy(update={'classifications':[labels]})
        decision=source_message_decision(policy,checked)
        if decision.verdict!='permit': raise PolicyError('evidence_not_permitted')
        clauses=set(decision.matched_allow_clause_ids)
        common=clauses if common is None else common&clauses
        result.append((qualified,rows))
    if not common: raise PolicyError('cross_rule_derivation')
    return result,sorted(common)[0]


def fact_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us):
    if row.get('object_type')!='fact' or row.get('valid_to') is not None:
        raise PolicyError('fact_not_current')
    payload=_json(row['payload_json'],dict)
    from .evidence import SHAREABLE_DISCLOSURES, implicit_labels
    if payload.get('disclosure') not in SHAREABLE_DISCLOSURES:
        raise PolicyError('fact_disclosure_unknown')
    subject,predicate=payload.get('subject_entity_id'),payload.get('predicate')
    # A structured pack value releases only through the one scalar field its class names.
    value=scalar(predicate,payload) if predicate in CLASSES else payload.get('object_value')
    if subject not in permit_subjects(conn,contract=ATTESTED_CONTRACT) or predicate not in PREDICATE_TEXT or not isinstance(value,str):
        raise PolicyError('fact_projection_unsupported')
    boundary=_unrestricted(resolver,conn,reviews,review_db,'signal_objects',row['object_id'],row)
    domains,sensitivity=implicit_labels(payload,row.get('signal_dimension'))
    prior=reviews._current_in(review_db,row['object_id'])
    if prior is not None:
        # Preserve explicit fact classifications and their exact revision checks.
        checked,_=resolver._qualified_bundle(conn,floor,row['object_id'],reviews,review_db,
                                             contract=ATTESTED_CONTRACT,discloses_sources=True)
        domains=tuple(set(domains)|{d for c in checked.classifications for d in c.domains})
        ranks={'none':0,'personal':1,'special':2,'unknown':3}
        sensitivity=max([sensitivity,*[c.sensitivity for c in checked.classifications]],key=ranks.__getitem__)
    sources,clause=_support(resolver,conn,floor,reviews,review_db,_json(row['source_refs_json'],list),policy,lower_us,upper_us,
                            extra_domains=domains,extra_sensitivity=sensitivity)
    check_lineage(payload,sources)
    assertion='owner_stated'
    if not any(explicitly_states_claim(rows[_key(q.snapshot.message.identity)]['content'],predicate,value) for q,rows in sources):
        # OD-38, flag default off: one cited message on its own entails the claim (guards + a stored verdict).
        from .entailment_grounding import author_of, entailed, fact_claim
        claim=fact_claim(predicate,value)
        attested=subject in permit_subjects(conn,contract=ATTESTED_CONTRACT)
        if not any(entailed(resolver,claim=claim,row=row,identity=q.snapshot.message.identity,
                            message=rows[_key(q.snapshot.message.identity)]['content'],author_is_owner=author_of(q),
                            subject_attested=attested,boundary=boundary) for q,rows in sources):
            # IF-6 v1 (§2 step 7), flag default off: the extractor's fact on one journal entry, as inferred.
            from . import inferred_facts
            if not inferred_facts.enabled():
                raise PolicyError('fact_not_grounded')
            _inferred(conn,reviews,review_db,policy,sources,predicate,value,boundary)
            assertion='inferred'
    return Projection('signal_objects',row['object_id'],'fact',f'Owner {PREDICATE_TEXT[predicate]} {value}.',
        {'assertion':assertion},sources,rows_revision([[row]]),clause)


def _inferred(conn,reviews,review_db,policy,sources,predicate,value,boundary):
    """IF-6 v1, §2 step 7: what a fact the stated floor and OD-38 did not ground needs to release as inferred.

    Reached only with `TOPOS_PERMISSIONS_V2_DERIVED_FACTS` on, after every check a stated fact runs (steps 1-5),
    so the fact's implicit labels have already met the grant's decision beside the entry's (`_support`). Each
    requirement is checked here, where it is relied on, even when an earlier step guarantees it today:
      - the support is exactly one source, a journal entry (`inferred_fact_scope` otherwise). A same-source twin
        resolves to its member, so a fact citing an entry and its twin is one source. Every cited record already
        passed `_support` above, which reads at most MAX_SUPPORT of them and refuses a fact citing more;
      - the grant signs the "Journal entries" option (`_journal_citation` refused otherwise; v1 asks nothing more
        of the grant);
      - the predicate has a releasable class (`predicate_classes.CLASSES`: never special, never health, never a
        third party). PREDICATE_TEXT also names `practices` and `training_for`, whose implicit labels the grant
        decision refuses today; this does not rest on that;
      - the node's own people can be read (else no third party can be ruled out);
      - every value guard of `inferred_facts.refusal`, over the entry's own labels before the merge, and (v1b) the
        protected_content its review gave before any floor (`_unfloored_protected_content`).
    Raises the first that fails; returns None when the fact releases as inferred."""
    from . import inferred_facts
    if len(sources)!=1 or sources[0][0].snapshot.message.identity.table!=JOURNAL:
        raise PolicyError('inferred_fact_scope')
    if 'journal_entry' not in policy.search.result_types:
        raise PolicyError('journal_citation_needs_record_option')
    if predicate not in CLASSES:
        raise PolicyError('fact_projection_unsupported')
    qualified,rows=sources[0]
    try:
        people=inferred_facts.snapshot_people(conn,boundary)
    except sqlite3.Error:
        raise PolicyError('evidence_storage_unavailable') from None
    code=inferred_facts.refusal(value,predicate,rows[_key(qualified.snapshot.message.identity)],
                                qualified.classifications[0],boundary=boundary,people=people,
                                model_protected_content=_unfloored_protected_content(reviews,review_db,qualified))
    if code is not None:
        raise PolicyError(code)


def _unfloored_protected_content(reviews,review_db,qualified):
    """IF-6 v1b: the protected_content of the very review that qualified the entry, before any floor.

    An owner correction is the owner's own word, so its label stands. A machine review's stored label has been
    through the journal family's floor (OD-58: the model's `unknown` becomes `none`, so the entry releases); the
    model's own label is recorded beside it (`MachineMessageReview.model_protected_content`). None when the review
    cannot be matched to the one qualification used, or recorded no model label (published before v1b): the
    inferred fact then withholds. With the flag on, such a journal review is not current
    (`automatic_message_review.lacks_model_label`), so its entry withholds too until it is assessed again."""
    from .automatic_message_review import MachineMessageReview, machine_key
    from .message_evidence import OwnerMessageReview, message_key
    identity=qualified.snapshot.message.identity
    for key,kind in ((message_key(identity),OwnerMessageReview),(machine_key(identity),MachineMessageReview)):
        review=reviews._current_in(review_db,key)
        if (isinstance(review,kind) and review.review_id==qualified.review_id
                and digest(review.model_dump())==qualified.review_revision):
            return (review.classifications[0].protected_content if kind is OwnerMessageReview
                    else review.model_protected_content)
    return None


def _goal_stated(content, goal):
    if not isinstance(goal,str) or not 6<=len(goal)<=8000: return False
    if re.search(r'[?\n"“”]|\b(?:not|never|if|unless|maybe|perhaps)\b',content,re.I): return False
    forms=r"(?:My goal is to |I want to |I plan to |I intend to |I aim to )"
    return bool(re.fullmatch(forms+re.escape(goal.rstrip('.!'))+r'[.!]?',content,re.I)
                or (content==goal and re.match(forms,content,re.I)))


def journal_entry_released(policy, qualified, rows, lower_us, upper_us) -> bool:
    """Whether this grant releases the cited journal entry itself, whole, as a `journal_entry` record (IF-5 §2-§3).

    What the index build asks of a raw journal member (`SearchIndexService._rebuild_once`) and the release asks of
    its record (`MessageSearchRelease._journal_member`), decided on the entry's own qualified labels, before any
    item's domains are merged into them: a knowledge grant that signs `journal_entry` and lists the journal table;
    its decision permits the entry; the entry is not NSFW-flagged, is text of at most 8,000 characters, and every
    instant its stated day can denote lies inside the window. `qualified` is the entry as `qualify_automatic_message`
    returned it for this grant's read. Decided per grant, where it is relied on; what cannot be decided is False.
    """
    try:
        from topos.disclosure.content_policy import is_record_nsfw
        from .evidence_families import within
        from .search_contract import CAPABILITY_KNOWLEDGE_SEARCH
        identity = qualified.snapshot.message.identity
        if identity.table != JOURNAL or not _journal_enabled():
            return False
        if (policy.versions.capability != CAPABILITY_KNOWLEDGE_SEARCH
                or 'journal_entry' not in policy.search.result_types or identity.table not in policy.search.tables):
            return False
        row = rows[_key(identity)]
        content = row.get('content')
        if is_record_nsfw(row) or not isinstance(content, str) or len(content) > 8000:
            return False
        if not within(identity.table, row, lower_us, upper_us):
            return False
        return source_message_decision(policy, qualified).verdict == 'permit'
    except Exception:  # noqa: BLE001 -- an entry whose release cannot be decided is not released
        return False


def _goal_field(conn, qualified, rows, goal_row, boundary, *, policy=None, lower_us=None, upper_us=None) -> bool:
    """IF-5 Lane H1, the journal family only (flag default off): the goal IS the cited entry's structured goal
    field, verbatim, and the field clears every guard of `journal_goal_field.refusal`, which reads the entry's own
    qualified labels, the attested self and the node's own people at this point of use.

    With `policy` and the window, whether that grant releases the entry whole is decided here
    (`journal_entry_released`) and handed to the rule: only then are the guards on the text's form set aside.
    Without them, or under a grant that does not release the entry, every guard applies.

    The goal must cite the entry itself. A goal the node's extraction stored for a same-text copy of the entry
    resolves to the same member (IF-5 §1.2) and would release beside the member's own goal, word for word the
    same: the field is one goal, the one stored for the member (the lane stores it when none is)."""
    identity = qualified.snapshot.message.identity
    if identity.table != JOURNAL or goal_row.get('record_id') != identity.record_id:
        return False
    from . import journal_goal_field
    if not journal_goal_field.enabled():
        return False
    entry = rows[_key(identity)]
    field = journal_goal_field.structured_field(entry)
    if field is None or field != goal_row.get('goal_text'):
        return False   # not the entry's goal field (most journal goals): the node's people are not even read
    from .entailment_grounding import author_of
    from .identity import attested_self
    released = policy is not None and journal_entry_released(policy, qualified, rows, lower_us, upper_us)
    people = frozenset()
    if not released:
        try:
            people = journal_goal_field.known_people(conn)
        except sqlite3.Error:
            return False   # the node's people cannot be read, so no third party can be ruled out
    labels = qualified.classifications[0]
    return journal_goal_field.refusal(goal_row.get('goal_text'), entry, boundary=boundary,
                                      author_is_owner=author_of(qualified),
                                      subject_attested=attested_self(conn) is not None,
                                      sensitivity=labels.sensitivity, people=people,
                                      entry_released=released) is None


def goal_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us):
    boundary=_unrestricted(resolver,conn,reviews,review_db,'user_goals',row['goal_id'],row)
    # Older goals name source+record but not canonical table. Resolve across the
    # source tables (the journal's while its family exists, IF-5 W6) only when
    # exactly one native candidate exists.
    refs=[]
    for table,id_column in GOAL_TABLES+(((JOURNAL,'entry_id'),) if _journal_enabled() else ()):
        cols={r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if not {id_column,'source_id'}<=cols: continue
        if conn.execute(f'SELECT 1 FROM {table} WHERE {id_column}=? AND source_id=?',
                        (row.get('record_id'),row.get('source_id'))).fetchone():
            refs.append(dict(table=table,record_id=row['record_id'],source_id=row['source_id']))
    if len(refs)!=1: raise PolicyError('lineage_identity_ambiguous')
    sources,clause=_support(resolver,conn,floor,reviews,review_db,refs,policy,lower_us,upper_us,extra_domains=('plans',))
    try: goal_payload=_json(row.get('payload_json') or '{}',dict)
    except PolicyError: goal_payload={}
    check_lineage(goal_payload,sources)
    q,rows=sources[0]
    content=rows[_key(q.snapshot.message.identity)]['content']
    if not _goal_stated(content,row.get('goal_text')) and not _goal_field(
            conn,q,rows,row,boundary,policy=policy,lower_us=lower_us,upper_us=upper_us):
        # OD-38, flag default off. A goal names no subject row; its subject is the message author, so the
        # owner must have exactly one attested self entity (#68, identity.attested_self) and the message must
        # be the owner's own original wording.
        from .entailment_grounding import author_of, entailed, goal_claim
        from .identity import attested_self
        if not entailed(resolver,claim=goal_claim(row.get('goal_text')),row=row,identity=q.snapshot.message.identity,
                        message=content,author_is_owner=author_of(q),
                        subject_attested=attested_self(conn) is not None,boundary=boundary):
            raise PolicyError('goal_not_grounded')
    return Projection('user_goals',row['goal_id'],'goal',row['goal_text'],{'status':'stated_intention'},
                      sources,rows_revision([[row]]),clause)


def relationship_projection(resolver,conn,reviews,review_db,row,source_projection):
    if row.get('valid_to') is not None: raise PolicyError('relationship_not_current')
    _unrestricted(resolver,conn,reviews,review_db,'entity_edges',row['edge_id'],row)
    metadata=_json(row['metadata_json'],dict)
    if metadata.get('source_object_id')!=source_projection.record_id:
        raise PolicyError('relationship_lineage_unknown')
    if row['src_entity_id'] not in permit_subjects(conn,contract=ATTESTED_CONTRACT):
        raise PolicyError('relationship_subject_unknown')
    endpoints=conn.execute('SELECT * FROM entities WHERE entity_id=?',(row['dst_entity_id'],)).fetchmany(2)
    if len(endpoints)!=1: raise PolicyError('relationship_endpoint_unknown')
    endpoint=dict(endpoints[0])
    _unrestricted(resolver,conn,reviews,review_db,'entities',row['dst_entity_id'],endpoint)
    target=endpoint.get('canonical_name')
    if source_projection.kind=='goal':
        if row['edge_type']!='pursues' or target!=source_projection.content:
            raise PolicyError('relationship_not_grounded')
    else:
        raise PolicyError('relationship_projection_unsupported')
    return Projection('entity_edges',row['edge_id'],'relationship',f'Owner intends to {target}',
        {'subject':'Owner','relation':'pursues','object':target},source_projection.sources,
        relationship_revision(row,endpoint,source_projection.revision),source_projection.allow_clause_id)


def _pinned(row, columns):
    """The columns of one graph row a relationship's revision pins (EDGE_COLUMNS, ENTITY_COLUMNS); NULL is absent."""
    pinned = {}
    for name, value in row.items():
        kind = columns.get(name, 'pinned')
        if kind == 'volatile' or value is None:
            continue
        if kind == 'goal_link':
            pinned[name + '.source_object_id'] = _json(value, dict).get('source_object_id')
        else:
            pinned[name] = value
    return pinned


def relationship_revision(edge, endpoint, source_revision):
    """What a relationship's release and eligibility read of its edge and its endpoint, and its goal's revision.

    A graph rebuild rewrites counters and timestamps on every `pursues` edge and goal node whether or not anything
    changed (1.4.3 pinned the whole rows, so every refresh staled every grant index holding a relationship); those
    columns are left out. The Off-limits scan reads them too, and its verdict is re-run on the current rows by the
    currency check (`current_revision`)."""
    return digest({'version': RELATIONSHIP_REVISION,
                   'rows': rows_revision([[_pinned(edge, EDGE_COLUMNS)], [_pinned(endpoint, ENTITY_COLUMNS)]]),
                   'source': source_revision})


def load_projection_row(conn,table,record_id):
    keys={'signal_objects':'object_id','user_goals':'goal_id','entity_edges':'edge_id'}
    if table not in keys: raise PolicyError('projection_table_unsupported')
    rows=conn.execute(f'SELECT * FROM {table} WHERE {keys[table]}=?',(record_id,)).fetchmany(2)
    if len(rows)!=1: raise PolicyError('projection_unavailable')
    return dict(rows[0])


def current_revision(conn,table,record_id,*,boundary=None):
    """The revision `qualify_projection` stamps on this projection, from its rows as they stand on `conn`.

    A fact or a goal is its whole row: that is what its eligibility reads (OD-38 keys its stored verdicts by that
    row's revision, `entailment_grounding.claim_revision`), and neither row is rewritten on a schedule (a fact when
    it is asserted again, a goal when its message is extracted again). A relationship is `relationship_revision`.
    With `boundary` (the currency check's own), a relationship whose edge or endpoint row the Off-limits scan now
    vetoes raises `entity_protected`: the scan reads the volatile columns the revision leaves out, so it is run
    again here, on the current rows."""
    row=load_projection_row(conn,table,record_id)
    if table!='entity_edges': return rows_revision([[row]])
    metadata=_json(row['metadata_json'],dict)
    goal=load_projection_row(conn,'user_goals',metadata.get('source_object_id'))
    endpoints=conn.execute('SELECT * FROM entities WHERE entity_id=?',(row['dst_entity_id'],)).fetchmany(2)
    if len(endpoints)!=1: raise PolicyError('relationship_endpoint_unknown')
    endpoint=dict(endpoints[0])
    if boundary is not None and (boundary.legacy_veto('entity_edges',row) or boundary.legacy_veto('entities',endpoint)):
        raise PolicyError('entity_protected')
    return relationship_revision(row,endpoint,rows_revision([[goal]]))


def qualify_projection(resolver,conn,floor,reviews,review_db,table,record_id,policy,lower_us,upper_us):
    row=load_projection_row(conn,table,record_id)
    if table=='signal_objects':
        return fact_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us)
    if table=='user_goals':
        return goal_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us)
    metadata=_json(row['metadata_json'],dict)
    source=qualify_projection(resolver,conn,floor,reviews,review_db,'user_goals',metadata.get('source_object_id'),policy,lower_us,upper_us)
    return relationship_projection(resolver,conn,reviews,review_db,row,source)


def _journal_citable(conn, identities):
    """Every entry id an item may cite for these journal members: each member and its same-source twins, which
    resolve to it (IF-5 §1.2). Empty while the journal family is off."""
    members=[identity for identity in identities if identity.table==JOURNAL]
    if not members or not _journal_enabled(): return set()
    found=set()
    for identity in members:
        found.add(identity.record_id)
        try:
            found.update(entry for (entry,) in conn.execute(
                f'SELECT twin.entry_id FROM {JOURNAL} member JOIN {JOURNAL} twin ON twin.source_id=member.source_id '
                'AND twin.content=member.content WHERE member.entry_id=? AND member.source_id=?',
                (identity.record_id,identity.source_id)))
        except sqlite3.Error:
            continue   # unreadable twins are not discovered: an item citing one is withheld, never guessed
    return found


def candidates(conn, permitted_messages, result_types):
    """Discovery only. Every returned identifier is independently qualified later.

    Iterate stored rows, without a hidden-universe top-k cutoff. Only rows naming
    an already qualified native source are considered for projection. A journal
    member is named by its entry id, or a twin's, under `record_id` or, in a
    rule-extractor object, `id` (IF-5 W6).
    """
    permitted_messages=list(permitted_messages)
    ids={identity.record_id for identity in permitted_messages}
    if not ids: return
    journal=_journal_citable(conn,permitted_messages)
    ids|=journal
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'fact' in result_types:
        for row in conn.execute("SELECT object_id,source_refs_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"):
            try: refs=_json(row[1],list)
            except PolicyError: continue
            if any(isinstance(ref,dict) and (ref.get('record_id') in ids or (ref.get('table')==JOURNAL
                   and isinstance(ref.get('id'),str) and ref['id'] in journal)) for ref in refs):
                yield 'signal_objects',row[0]
    goals=set()
    if {'goal','relationship'}&set(result_types) and 'user_goals' in tables:
        for record_id,message_id in conn.execute('SELECT goal_id,record_id FROM user_goals'):
            if message_id in ids:
                goals.add(record_id)
                if 'goal' in result_types: yield 'user_goals',record_id
    if 'relationship' in result_types and goals and 'entity_edges' in tables:
        for row in conn.execute("SELECT edge_id,metadata_json FROM entity_edges WHERE valid_to IS NULL AND edge_type='pursues'"):
            try: metadata=_json(row[1],dict)
            except PolicyError: continue
            if metadata.get('source_object_id') in goals: yield 'entity_edges',row[0]
