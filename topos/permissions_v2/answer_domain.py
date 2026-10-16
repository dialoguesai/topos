"""Lane G1 (16 Oct 2026, narrow first): a question about a domain the grant denies gets no answer.

A share that denies health, family, finance, relationships and home is asked about the owner's health. Retrieval
returns only permitted items (the denied ones are withheld), the writer describes them, and the support judge keeps
each sentence because it is true of its item: nothing leaks, but the answer is padding. This module decides, before
the model, that such a question has no answer.

`asks_denied_domain` is the pure decision: no switch, no environment, no database; the offline replay calls it over
recorded runs. `policy_domains` reads the grant's denied and permitted domains from its signed rules, the way
`release.source_message_decision` reads them. The answer service (`answer_release.AnswerService._compute`) checks
`switches.ANSWER_DENIED_DOMAIN_REFUSAL` and calls both, beside `question_lacks_permitted_anchor`, and refuses with that
rule's reason, `question_not_supported`: the asker learns nothing about which domain is denied.

Words are matched as the anchor check reads presence (`answer_checks.question_only_anchors`): whole tokens, case
folded, at most one suffix apart ("finances" and "finance", "planning" and "plans"). The whole fold is not used: it
takes "outings" (the plans domain's phrase word) to "out", so "find out" would read as a plans question (BL-144's
reason, the other way round).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .answer_checks import _forms_within_one_suffix, _tokens, _within_one_suffix
from .answer_generation import CATEGORY_TOPICS
from .contract import Atom, evaluate_predicate
from .release import _rule_sources, _tables

#: The one processor whose rules the release evaluates (`release.source_message_decision`).
PROCESSOR = "owner-engine-local"


def _domain_forms(domains: Iterable[str], topics: Mapping[str, Iterable[str]]) -> set[str]:
    """Every word of the domains' names and catalog phrase words, as written and with one suffix off."""
    return _forms_within_one_suffix(word for domain in domains for name in (domain, *sorted(topics.get(domain, ())))
                                    for word in _tokens(name))


def prompt_item_texts(prompt) -> list[str]:
    """The permitted items' texts exactly as the anchor check reads them (`question_lacks_permitted_anchor`): each
    item's clipped body, kind and supporting text, then its domain evidence and source family."""
    return list(prompt.raw_texts + prompt.domain_texts + prompt.source_texts)


def asks_denied_domain(question: str, item_texts: list[str], denied: frozenset[str], permitted: frozenset[str], *,
                       topics: Mapping[str, Iterable[str]] = CATEGORY_TOPICS) -> bool:
    """True when the question asks about a domain the grant denies and nothing it may be answered from carries it.

    All four must hold:
      1. `denied` names at least one domain;
      2. a word of the question is a word of a denied domain: its name or a phrase word of `topics` (the node's
         `CATEGORY_TOPICS`), whole words, case folded, at most one suffix apart;
      3. at least one such word is no word of a domain in `permitted` (an ambiguous word never refuses);
      4. none of the question's denied-domain words (ambiguous ones included) is in any of `item_texts`: the
         permitted items' bodies, kinds, supporting text, domain evidence and source families, as the anchor check
         reads them (`Prompt.raw_texts + domain_texts + source_texts`).
    A question with no denied-domain word is never refused, whatever the items say."""
    if not denied:
        return False
    denied_forms = _domain_forms(denied, topics)
    asked = [word for word in _tokens(question) if _within_one_suffix(word, denied_forms)]
    if not asked:
        return False
    permitted_forms = _domain_forms(permitted, topics)
    if all(_within_one_suffix(word, permitted_forms) for word in asked):
        return False
    item_forms = _forms_within_one_suffix(word for text in item_texts for word in _tokens(text))
    return not any(_within_one_suffix(word, item_forms) for word in asked)


def _atom_domains(predicate) -> set[str]:
    """Every domain value a predicate names, at any depth."""
    if isinstance(predicate, Atom):
        return set(predicate.values) if predicate.attribute == "domain" else set()
    terms = getattr(predicate, "terms", None)
    if terms is not None:
        return set().union(*(_atom_domains(term) for term in terms))
    term = getattr(predicate, "term", None)
    return _atom_domains(term) if term is not None else set()


def policy_domains(policy, *, vocabulary: Iterable[str] = CATEGORY_TOPICS) -> tuple[frozenset[str], frozenset[str]]:
    """(denied, permitted): the domains this grant's signed rules deny and permit, read as the release reads them.

    Each domain the policy's atoms name, or the catalog names, is labelled alone (`{"domain": [d]}`) with every other
    attribute unknown, and each rule's two predicates are evaluated on it (Kleene logic, `contract.evaluate_predicate`),
    over the rules the release evaluates (processor `owner-engine-local`; `release.source_message_decision`):
      - denied: a deny rule says True for it, and that rule covers every source the permit rules draw on and every
        table the share searches, so no item of the domain is released from any of them;
      - permitted: a permit rule at the raw ceiling could release it (neither predicate says False), less the denied
        ones (a deny wins in the release).
    A policy with no deny rule denies nothing."""
    rules = [rule for rule in policy.rules if PROCESSOR in rule.evidence_use.processors.values]
    permits = [rule for rule in rules if rule.effect == "permit" and rule.release.ceiling == "raw"]
    denies = [rule for rule in rules if rule.effect == "deny"]
    sources = set().union(*(_rule_sources(rule, policy) for rule in permits))
    tables = set(policy.search.tables)
    covering = [rule for rule in denies if sources <= _rule_sources(rule, policy) and tables <= _tables(rule)]
    named = set(vocabulary).union(*(_atom_domains(predicate) for rule in rules
                                    for predicate in (rule.evidence_use.predicate, rule.release.predicate)))

    def label(domain):
        return {"domain": [domain], "actor_role": None, "subject": None, "sensitivity": None}

    denied = frozenset(domain for domain in named
                       if any(True in (evaluate_predicate(rule.evidence_use.predicate, label(domain)),
                                       evaluate_predicate(rule.release.predicate, label(domain)))
                              for rule in covering))
    permitted = frozenset(domain for domain in named
                          if any(False not in (evaluate_predicate(rule.evidence_use.predicate, label(domain)),
                                               evaluate_predicate(rule.release.predicate, label(domain)))
                                 for rule in permits)) - denied
    return denied, permitted
