"""Bounded local-LLM topic-cluster labels with deterministic fallback.

TF-IDF term labels degrade with corpus noise ("https / good / here" on the
live node). A single short local-LLM call per cluster per full recompute is
cheap (recomputes are debounced to ~daily) and makes every surface that shows
cluster labels legible.

Naming a cluster takes two things the first version of this prompt had
neither of:

  * **contrast** — what this cluster is NOT. Each cluster was named alone,
    from its own raw within-cluster term frequency, so the model could not
    know a sibling had already taken the name. For a personal corpus the MOST
    frequent terms are exactly the words every cluster shares, which drives
    labels toward the generic by construction: one live node carried 151
    clusters under ~132 distinct labels, and among the 114 active in the last
    30 days only 78 distinct names — "personal projects" on seventeen of them,
    "private notes" on seven. Fixed by class-based TF-IDF terms (weight here
    against the rest of the set, not raw frequency) plus the labels of the
    nearest siblings in the prompt, with clusters named in a deterministic
    order so those sibling labels exist when they are needed.
  * **a lens** — clusters are built per signal dimension
    (``cluster_records_by_facet``), but every dimension got the same generic
    instruction, so "personal projects" spanned five of them. Each prompt now
    opens with the question that dimension exists to answer, plus one line
    saying what kind of name answers it.

Those two dimension lines come from deliberately different places:

  * ``core_question`` is what the dimension MEANS, so it is read from
    ``definitions/<dimension>.json`` and never restated here;
  * the directive line ("Name the project, deliverable or work stream") is
    labeling-specific instruction text — it constrains the part of speech a
    namer produces, not the dimension's semantics — so it is a dict in this
    module keyed by ``dimension_id``. It could equally have been a
    ``label_instruction`` field in the definition JSONs; those files were
    under concurrent edit when this landed, and a labeler-only concern is not
    worth a write into them.

The banned-vocabulary rule is load-bearing rather than stylistic: every
duplicate label measured on the live node is built out of the eight words it
names. It is stated in the prompt and re-checked after parsing, because a
model that ignores it gets one retry and is then counted honestly.

Contract (same shape as the disclosure minimizer's EngineSelector):
  * ``complete`` is injectable for tests;
  * any timeout / error / unparseable output keeps the deterministic label;
  * the first hard failure aborts remaining calls (a down model must cost one
    timeout, not k of them);
  * prompts are a pure function of the cluster set — same input, same bytes.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger("topos.features.signal.cluster_labels")

_LABEL_POOL = ThreadPoolExecutor(max_workers=2)
_MAX_LABEL_CHARS = 60
# The prompt's own first rule, enforced after parsing too — see
# label_is_wrong_length for why the rule alone was not enough.
_MIN_LABEL_WORDS = 2
_MAX_LABEL_WORDS = 5
_SAMPLE_PREVIEWS = 12
_PREVIEW_CHARS = 140

# Contrastive labeling.
_SIBLING_LABELS = 6
_DISTINGUISHING_TERMS = 8
# A term most clusters carry cannot distinguish one of them. The contrast
# score already sends those toward zero; the share cut removes them outright
# (live: "time", carried by 60% of clusters, outranked every specific term).
# Only meaningful once there are enough clusters for a share to mean anything.
_UBIQUITY_SHARE = 0.4
_MIN_CLUSTERS_FOR_UBIQUITY_CUT = 10

_REJECT_MARKERS = ("label", "topic cluster", "i cannot", "i can't", "sorry", "as an ai")
_WORD_TOKEN = re.compile(r"[a-z]+")
_LABEL_PREFIX = re.compile(r"^\s*(?:label|name|answer)\s*[:\-]\s*", re.IGNORECASE)
# A published label must never be a link. "&" stays legal (AT&T, R&D).
_URL_LIKE = re.compile(
    r"https?://|www\.|[/@?=]|\.(?:com|org|net|io|app|dev|ai|co|gl|gov|edu)\b",
    re.IGNORECASE,
)

# Every duplicate label observed on the live node is built out of these words
# ("personal projects" x17, "private notes" x7, "personal productivity" x6,
# "personal reflections" x5). They are banned in the prompt and checked again
# after parsing, because a rule the model ignores is not a rule.
_BANNED_LABEL_WORDS = (
    "personal",
    "private",
    "general",
    "misc",
    "various",
    "notes",
    "stuff",
    "activities",
)
_BANNED_LABEL_WORD_SET = frozenset(_BANNED_LABEL_WORDS)
# Not content: these carry no subject, so they neither trigger nor excuse a
# banned label ("my personal notes" is as generic as "personal notes").
_LABEL_FILLER = frozenset(
    {"a", "an", "and", "the", "of", "for", "to", "in", "on", "my", "our", "their", "other"}
)

_GENERIC_CORE_QUESTION = "What does this group of the person's records show about them?"
_GENERIC_LABEL_INSTRUCTION = "Name the subject these items are actually about."

# Labeling directives, NOT dimension semantics — see the module docstring for
# why these live here and ``core_question`` does not. A dimension_id absent
# from this map (network_bridge, profile, anything added later) takes the
# generic instruction; an unmapped dimension must never be an error.
_DIMENSION_LABEL_INSTRUCTIONS: Dict[str, str] = {
    "interests": "Name the subject of attention - a topic, product, field or source.",
    "relationships": "Name the tie or shared context - who, or what binds these people.",
    "wellbeing": "Name the state, rhythm or condition. Descriptive only, never clinical.",
    "memory": "Name the recurring thread - the subject that keeps coming back.",
    "work": "Name the project, deliverable or work stream.",
    "places": "Name the place or the rhythm of being there.",
    "time": "Name the recurring commitment or availability pattern.",
    "resources": "Name the flow or standing commitment.",
    "intentions": "Name the objective being pursued.",
}


def cluster_label_model() -> str:
    explicit = os.environ.get("TOPOS_CLUSTER_LABEL_MODEL", "").strip()
    if explicit:
        return explicit
    try:
        from ...config.settings import settings

        return str(settings.ollama_query_model)
    except Exception:
        return "llama3.2:latest"


def cluster_label_contrast_enabled() -> bool:
    """Contrastive prompts on by default; ``off`` restores the isolated prompt."""
    raw = os.environ.get("TOPOS_CLUSTER_LABEL_CONTRAST", "").strip().lower()
    return raw not in ("off", "0", "false", "no")


def dimension_core_question(dimension: Optional[str]) -> str:
    """The question this signal dimension exists to answer.

    Read from ``definitions/<dimension>.json`` — never restated here — so the
    labeler cannot drift from what a dimension means. Facets with no
    definition (``network_bridge``) and clusters with no dimension get a
    generic question rather than an empty line, which keeps the prompt one
    fixed shape for every cluster.
    """
    dim = str(dimension or "").strip().lower()
    if not dim:
        return _GENERIC_CORE_QUESTION
    try:
        from .dimension_definition_loader import get_definition_or_none

        defn = get_definition_or_none(dim)
    except Exception:  # noqa: BLE001 — a missing definition must not stop labeling
        defn = None
    if not defn:
        return _GENERIC_CORE_QUESTION
    return str(defn.get("core_question") or "").strip() or _GENERIC_CORE_QUESTION


def dimension_label_instruction(dimension: Optional[str]) -> str:
    """What kind of name this dimension wants — one directive line.

    One source, deliberately: the map above. This line is not read from the
    definition JSONs even though an optional ``label_framing`` field exists
    there, because two places that can each answer "how should this dimension
    be labelled" is two places that can disagree, and the directive is a
    labeler concern rather than dimension semantics. What the dimension MEANS
    still comes from the definition — see ``dimension_core_question``.
    """
    dim = str(dimension or "").strip().lower()
    return _DIMENSION_LABEL_INSTRUCTIONS.get(dim, _GENERIC_LABEL_INSTRUCTION)


def label_banned_words(label: str) -> List[str]:
    """Banned words carried by a label, in order — empty when it is specific."""
    tokens = re.findall(r"[a-z]+", str(label or "").lower())
    return [t for t in tokens if t in _BANNED_LABEL_WORD_SET]


def label_is_urlish(label: str) -> bool:
    """True for a URL, bare domain, path or @handle answer.

    Distinguishing terms are drawn from page titles and links, so a cluster
    whose most contrastive token is a link invites the model to answer with
    it: one measured relabel came back as a full maps.app.goo.gl URL, query
    string included. A cluster label is published — it reaches top_topics
    facts and every surface that lists topics — and the interests definition
    bans verbatim URLs there outright. This is a hard gate, not a preference:
    a url-ish answer that survives its retry keeps the term label.
    """
    return bool(_URL_LIKE.search(str(label or "")))


def label_mentions_protected(label: str, protected_terms: Optional[Any] = None) -> bool:
    """True when a label names an entity the owner put off limits.

    A cluster label is stored once and served to every caller, so there is no
    per-caller variant to filter: the name must not be minted in the first
    place. This is the same name scan the egress surfaces use
    (``blackholed_name_terms``), applied at the producer instead — the labeler
    only started needing it when labels stopped being term soup and started
    naming the subject.
    """
    if not protected_terms:
        return False
    try:
        from ..lifecycle.blackhole import normalize_entity_name
    except Exception:  # noqa: BLE001 — no blackhole feature means nothing to protect
        return False
    blob = normalize_entity_name(str(label or ""))
    if not blob:
        return False
    return any(term and term in blob for term in protected_terms)


def label_is_generic(label: str) -> bool:
    """True when a label leans on the banned vocabulary for its meaning.

    Any banned word counts, not only an all-banned name: the observed
    duplicates are "personal projects" / "personal productivity" /
    "personal reflections", where the banned word is doing the work and the
    remaining word is a category. Filler is ignored so "my personal notes"
    reads the same as "personal notes".
    """
    tokens = [
        t
        for t in re.findall(r"[a-z]+", str(label or "").lower())
        if t not in _LABEL_FILLER
    ]
    if not tokens:
        return False
    return any(t in _BANNED_LABEL_WORD_SET for t in tokens)


def _fallback_terms(cluster: Dict[str, Any]) -> Counter:
    """Terms for a cluster whose members carry no text (DB rows without previews)."""
    counter: Counter = Counter()
    label = str(cluster.get("label") or "")
    for term in list(cluster.get("label_terms") or []) + label.split(" / "):
        token = str(term).strip().lower()
        if token:
            counter[token] += 1
    return counter


def member_bigrams(text: str, stopwords: Any) -> List[str]:
    """Adjacent content-word pairs, in reading order.

    The unigram extractor is a ``[a-z]{4,}`` findall, which destroys every
    phrase before the model sees it: "google search" reaches the prompt as
    "google", "search", and "commit messages" as "commit", "messages". The
    prompt then asks for a NAME from a list of bare tokens, and gets one bare
    token back — which is most of what the 2-5 word rule was fighting.

    Adjacent means nothing but whitespace between the two words, so a comma or
    a full stop ends the phrase rather than joining across it. Both halves
    face the same length and stopword filter as unigrams, so "the search"
    never forms.
    """
    pairs: List[str] = []
    previous: Optional[str] = None
    previous_end = -1
    for match in _WORD_TOKEN.finditer(text):
        token = match.group(0)
        joined_by_space = previous_end >= 0 and not text[previous_end : match.start()].strip()
        if (
            joined_by_space
            and previous
            and len(previous) >= 4
            and len(token) >= 4
            and previous not in stopwords
            and token not in stopwords
            # A banned word is not distinguishing on its own, but pairing it
            # with one that is ("personal kayaking") makes the pair rare enough
            # to top the contrast ranking — which would feed the exact generic
            # vocabulary the prompt bans straight back into the prompt.
            and previous not in _BANNED_LABEL_WORD_SET
            and token not in _BANNED_LABEL_WORD_SET
        ):
            pairs.append(f"{previous} {token}")
        previous, previous_end = token, match.end()
    return pairs


def _cluster_term_counts(clusters: Sequence[Dict[str, Any]]) -> List[Counter]:
    # Lazy: topic_clustering imports this module from inside its functions, and
    # a module-level import here would make that a cycle the day it moves up.
    from .topic_clustering import _STOPWORDS, _member_term_counts

    counts: List[Counter] = []
    for cluster in clusters:
        members = list(cluster.get("members") or [])
        counter = _member_term_counts(members)
        if counter:
            for member in members:
                text = str(member.get("text_preview") or "").lower()
                counter.update(member_bigrams(text, _STOPWORDS))
        counts.append(counter if counter else _fallback_terms(cluster))
    return counts


def _drop_subsumed(scored: Sequence[tuple[str, float]], top_n: int) -> List[str]:
    """Take the best ``top_n``, letting a phrase stand in for its own words.

    "google search" ranking above "google" makes "google" redundant: spending
    two of eight slots on the halves of a phrase already listed tells the model
    less, not more. Order is preserved, so the strongest term still leads.
    """
    chosen: List[str] = []
    covered: set[str] = set()
    for term, _weight in scored:
        parts = term.split(" ")
        if len(parts) == 1:
            if term in covered:
                continue
        else:
            covered.update(parts)
            # A phrase arriving after one of its own words replaces it.
            chosen = [c for c in chosen if c not in parts]
        chosen.append(term)
        if len(chosen) >= top_n:
            break
    return chosen[:top_n]


def compute_distinguishing_terms(
    clusters: Sequence[Dict[str, Any]],
    *,
    top_n: int = _DISTINGUISHING_TERMS,
) -> List[List[str]]:
    """Per-cluster terms ranked by contrast, index-aligned with ``clusters``.

    Class-based TF-IDF in the BERTopic sense — each cluster is one document —
    scored as this cluster's share of a term against the rest of the set:
    ``p(t|cluster) · log(p(t|cluster) / p(t|rest))``. A term no more frequent
    here than elsewhere scores zero and is dropped. Raw within-cluster
    frequency, what the first labeler fed the model, ranks whatever the whole
    corpus talks about — the same list for every cluster, which is how
    fourteen clusters ended up called "personal projects".
    """
    counts = _cluster_term_counts(clusters)
    n_clusters = max(len(clusters), 1)

    corpus_freq: Counter = Counter()
    doc_freq: Counter = Counter()
    for counter in counts:
        corpus_freq.update(counter)
        for term in counter:
            doc_freq[term] += 1
    corpus_tokens = sum(corpus_freq.values())
    ubiquity_cut = (
        _UBIQUITY_SHARE * n_clusters
        if n_clusters >= _MIN_CLUSTERS_FOR_UBIQUITY_CUT
        else float(n_clusters) + 1.0
    )

    def _score(counter: Counter, *, contrast: bool) -> List[str]:
        here_tokens = sum(counter.values()) or 1
        rest_tokens = max(corpus_tokens - here_tokens, 1)
        scored: List[tuple[str, float]] = []
        for term, tf in counter.items():
            p_here = tf / here_tokens
            if not contrast:
                scored.append((term, p_here))
                continue
            if doc_freq[term] >= ubiquity_cut:
                continue  # carried by most clusters: cannot distinguish this one
            p_rest = max(corpus_freq[term] - tf, 0) / rest_tokens
            weight = p_here * math.log((p_here + 1e-9) / (p_rest + 1e-9))
            if weight <= 0:
                continue  # no more frequent here than anywhere else
            scored.append((term, weight))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return _drop_subsumed(scored, top_n)

    out: List[List[str]] = []
    for counter in counts:
        terms = _score(counter, contrast=True)
        if not terms:  # nothing stands out — a shared term beats no term at all
            terms = _score(counter, contrast=False)
        out.append(terms)
    return out


def _dimension_of(cluster: Dict[str, Any]) -> str:
    return str(cluster.get("primary_dimension") or cluster.get("dimension") or "").strip().lower()


def _centroid(cluster: Dict[str, Any]) -> List[float]:
    vector = cluster.get("centroid_vector")
    if not isinstance(vector, list):
        metadata = cluster.get("metadata")
        if isinstance(metadata, dict):
            vector = metadata.get("centroid_vector")
    if not isinstance(vector, list):
        return []
    try:
        return [float(x) for x in vector]
    except (TypeError, ValueError):
        return []


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _term_overlap(a: Sequence[str], b: Sequence[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def labeling_order(clusters: Sequence[Dict[str, Any]]) -> List[int]:
    """Indices in labeling order: siblings adjacent, biggest cluster first.

    Deterministic on purpose — a cluster's prompt contains the labels its
    already-named siblings took, so the order IS part of the output.
    """
    return sorted(
        range(len(clusters)),
        key=lambda i: (
            _dimension_of(clusters[i]),
            -int(clusters[i].get("member_count") or len(clusters[i].get("members") or [])),
            str(clusters[i].get("cluster_id") or ""),
            i,
        ),
    )


def sibling_labels(
    index: int,
    clusters: Sequence[Dict[str, Any]],
    distinguishing_terms: Sequence[Sequence[str]],
    assigned: Dict[int, str],
    *,
    limit: int = _SIBLING_LABELS,
) -> List[str]:
    """Labels of the nearest clusters — same dimension first, then anywhere.

    Similarity is centroid cosine when both centroids are known and term
    overlap otherwise, so this works on clusters loaded back from SQLite
    without their member vectors. A sibling contributes the label it was just
    given this pass when it has one, else the term label it is carrying.
    """
    target_dim = _dimension_of(clusters[index])
    target_centroid = _centroid(clusters[index])
    target_terms = distinguishing_terms[index] if index < len(distinguishing_terms) else []

    scored: List[tuple[int, float, float, int]] = []
    for other in range(len(clusters)):
        if other == index:
            continue
        centroid = _centroid(clusters[other])
        if target_centroid and centroid:
            similarity = _cosine(target_centroid, centroid)
        else:
            other_terms = (
                distinguishing_terms[other] if other < len(distinguishing_terms) else []
            )
            similarity = _term_overlap(target_terms, other_terms)
        same_dim = 1.0 if _dimension_of(clusters[other]) == target_dim else 0.0
        scored.append((other, same_dim, similarity, other))
    scored.sort(key=lambda item: (-item[1], -item[2], item[3]))

    out: List[str] = []
    seen: set[str] = set()
    for other, _same_dim, _similarity, _tie in scored:
        label = str(assigned.get(other) or clusters[other].get("label") or "").strip()
        if not label:
            continue
        key = _normalized_label(label)
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= limit:
            break
    return out


def _previews(cluster: Dict[str, Any]) -> List[str]:
    previews: List[str] = []
    for member in (cluster.get("members") or [])[: _SAMPLE_PREVIEWS * 3]:
        text = str(member.get("text_preview") or "").strip()
        if text:
            previews.append(text[:_PREVIEW_CHARS])
        if len(previews) >= _SAMPLE_PREVIEWS:
            break
    return previews


def build_label_prompt(cluster: Dict[str, Any]) -> str:
    """The original single-cluster prompt: no lens, no siblings.

    Kept as-is so the relabel evaluation can run this arm against the same
    clusters and the same model (``scripts/eval_cluster_labels.py``).
    """
    terms = [str(t) for t in (cluster.get("label_terms") or []) if t]
    label = str(cluster.get("label") or "")
    if label:
        terms = [t for t in label.split(" / ") if t] + terms
    lines = [
        "You name topic clusters built from one person's private notes,",
        "messages, and web activity. Reply with ONLY a short descriptive",
        "label of 2-5 words. No quotes, no trailing punctuation, no",
        "explanation.",
        "",
        f"Frequent terms: {', '.join(terms[:8]) or '(none)'}",
        "Sample items:",
    ]
    lines.extend(f"- {p}" for p in _previews(cluster))
    lines.append("Label:")
    return "\n".join(lines)


def build_contrastive_label_prompt(
    cluster: Dict[str, Any],
    *,
    distinguishing_terms: Optional[Sequence[str]] = None,
    siblings: Optional[Sequence[str]] = None,
    rejected_label: Optional[str] = None,
    rejected_reason: str = "taken",
) -> str:
    """Prompt carrying the dimension's question, its contrast, and its neighbours.

    ``rejected_label`` builds the retry prompt: ``url`` when the answer was a
    link, ``taken`` when a sibling already answered with that name, ``generic``
    when the answer leaned on the banned vocabulary.
    """
    dimension = _dimension_of(cluster)
    terms = [str(t) for t in (distinguishing_terms or []) if t][:_DISTINGUISHING_TERMS]
    sibling_list = [str(s) for s in (siblings or []) if str(s).strip()]
    lines = [
        "You are naming one cluster of a person's records so the name answers a",
        "specific question about them.",
        "",
        f"QUESTION THIS DIMENSION ANSWERS: {dimension_core_question(dimension)}",
        dimension_label_instruction(dimension),
        "",
        "What makes THIS cluster different from its neighbours: "
        f"{', '.join(terms) or '(nothing stands out)'}",
        "Sample items:",
    ]
    lines.extend(f"- {p}" for p in _previews(cluster))
    if sibling_list:
        lines.extend(
            [
                "",
                "Names already used by neighbouring clusters - yours must be clearly"
                " different:",
            ]
        )
        lines.extend(f"- {s}" for s in sibling_list)
    lines.extend(
        [
            "",
            "Rules:",
            # "2-5 words" alone measured 46% compliant; naming the failure mode
            # is what the other rules here already do for links and generics.
            "- 2-5 words, never one word on its own. No quotes, no punctuation,"
            " no explanation.",
            "- Name the actual subject, activity, person, place, product or project.",
            "- A bare name is not enough: say what about it - what they do with it,"
            " or what happens there.",
            "- Never a URL, domain, file path or @handle - write what it IS.",
            "- Never use: " + ", ".join(_BANNED_LABEL_WORDS) + ".",
        ]
    )
    if rejected_reason == "protected":
        # Deliberately does NOT quote the rejected answer: it contains the name
        # the owner put off limits, and echoing it back into the next prompt
        # would put it right back in front of the model.
        lines.append(
            "- The previous answer named a person or place that must not appear."
            " Name the activity or subject instead."
        )
    elif rejected_label and rejected_reason == "url":
        lines.append(
            f'- "{rejected_label}" was rejected: it is a link, not a name. Say what it is about.'
        )
    elif rejected_label and rejected_reason == "generic":
        lines.append(
            f'- "{rejected_label}" was rejected as too generic. Name the subject itself.'
        )
    elif rejected_label and rejected_reason == "length":
        count = label_word_count(rejected_label)
        if count < _MIN_LABEL_WORDS:
            lines.append(
                f'- "{rejected_label}" is one word. Keep it and add what about it,'
                f" for {_MIN_LABEL_WORDS}-{_MAX_LABEL_WORDS} words in total."
            )
        else:
            lines.append(
                f'- "{rejected_label}" is {count} words. Answer with'
                f" {_MIN_LABEL_WORDS}-{_MAX_LABEL_WORDS}."
            )
    elif rejected_label:
        lines.append(
            f'- "{rejected_label}" is already taken by another cluster. Pick a different name.'
        )
    lines.append("Label:")
    return "\n".join(lines)


def parse_label(text: str) -> Optional[str]:
    """First line of model output, sanitized; None keeps the fallback label."""
    raw = str(text or "").strip()
    if not raw:
        return None
    line = raw.splitlines()[0].strip()
    line = _LABEL_PREFIX.sub("", line, count=1)
    line = line.strip("\"'`“”‘’ .:;")
    line = re.sub(r"\s+", " ", line)
    if not line or len(line) > _MAX_LABEL_CHARS:
        return None
    lowered = line.lower()
    if any(marker in lowered for marker in _REJECT_MARKERS):
        return None
    if len(line.split()) > 7:
        return None
    return line


def label_word_count(label: str) -> int:
    return len(str(label or "").split())


def label_is_wrong_length(label: str) -> bool:
    """True when an answer ignores the prompt's own first rule.

    Measured on the first full relabel of the live node: the contrastive prompt
    fixed duplication but obeyed "2-5 words" LESS than the prompt it replaced —
    46% compliance against 90%, with single-word answers going 15 → 81
    ("Met", "America", "Friend", "Internet"). Every directive in the prompt
    asks the model to *name a thing*, and distinguishing terms arrive as bare
    tokens, so a lone proper noun is the path of least resistance. Those names
    are unique, which is exactly why the duplication metrics looked healthy
    while the labels got less informative — one bare noun says who or where,
    never what about them.
    """
    count = label_word_count(label)
    return count < _MIN_LABEL_WORDS or count > _MAX_LABEL_WORDS


def _normalized_label(label: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(label or "").lower()).strip()


def _rejection_reason(
    label: str,
    used: Dict[str, int],
    protected_terms: Optional[Any] = None,
) -> Optional[str]:
    """Why this answer earns the one retry, or None to accept it as given."""
    if label_mentions_protected(label, protected_terms):
        return "protected"  # first: unpublishable at any distinctness
    if label_is_urlish(label):
        return "url"
    if _normalized_label(label) in used:
        return "taken"
    if label_is_generic(label):
        return "generic"
    if label_is_wrong_length(label):
        return "length"  # last: a terse name is weak, not unpublishable
    return None


def _label_rank(
    label: str,
    used: Dict[str, int],
    protected_terms: Optional[Any] = None,
) -> tuple[int, int, int, int, int]:
    """Higher is better: publishable beats protected, then link, then duplicate,
    then generic, then well-formed length.

    Length ranks last on purpose. "Austin" is a worse label than "Austin Trips",
    but it is a better one than a duplicate or "personal projects" — so a retry
    only wins on length when it costs nothing above it.
    """
    return (
        0 if label_mentions_protected(label, protected_terms) else 1,
        0 if label_is_urlish(label) else 1,
        0 if _normalized_label(label) in used else 1,
        0 if label_is_generic(label) else 1,
        0 if label_is_wrong_length(label) else 1,
    )


def _presentable(label: str) -> str:
    """Title-case an all-lowercase answer; leave any deliberate casing alone.

    The model answers in prose case about two thirds of the time ("rami malek",
    "friends"), and these are read side by side with term labels and entity
    names. Deterministic, so it costs no call and cannot vary between runs.
    """
    text = str(label or "")
    return text.title() if text and not any(ch.isupper() for ch in text) else text


def _better_label(
    first: str,
    retried: Optional[str],
    used: Dict[str, int],
    protected_terms: Optional[Any] = None,
) -> str:
    """Keep the retry only when it is actually an improvement.

    A model that answers "personal projects" twice still beats falling back to
    the term label it was replacing ("https / good / here"), so a stubbornly
    generic answer ships and is counted, not discarded. A protected name never
    ships either way — the caller drops it after this.
    """
    if not retried:
        return first
    return (
        retried
        if _label_rank(retried, used, protected_terms)
        > _label_rank(first, used, protected_terms)
        else first
    )


def _disambiguated(
    label: str,
    terms: Sequence[str],
    used: Dict[str, int],
    cluster: Dict[str, Any],
) -> Optional[str]:
    """Deterministic last resort when the model repeats a name anyway.

    Same parenthetical shape the term labeler uses (``_disambiguate_labels``),
    so a duplicate never reaches a surface that lists clusters side by side.
    """
    lowered = str(label).lower()
    candidates = [str(t) for t in terms if str(t).lower() not in lowered]
    candidates.append(str(cluster.get("member_count") or len(cluster.get("members") or [])))
    for candidate in candidates:
        suffixed = f"{label} ({candidate})"
        if len(suffixed) > _MAX_LABEL_CHARS:
            continue
        if _normalized_label(suffixed) not in used:
            return suffixed
    return None


class LabelerUnavailable(Exception):
    pass


def _complete_via_engine(prompt: str) -> str:
    from ...engine.client import get_engine_client_or_local
    from ...engine.tasks import ModelRequest, ProcessingTask, RequestedBy

    client = get_engine_client_or_local(None)
    task = ProcessingTask(
        id="cluster_label",
        type="enrichment",
        subtype="query_inference",
        source_id="cluster_labeler",
        record_ids=[],
        input={"query": prompt, "context": ""},
        model_request=ModelRequest(provider="ollama", model=cluster_label_model()),
        requested_by=RequestedBy(origin="ingestion_pipeline"),
    )
    result = client.run(task)
    if getattr(result, "status", None) != "completed":
        raise LabelerUnavailable(f"label model status={getattr(result, 'status', None)}")
    out = result.output or {}
    return str(out.get("answer") or out.get("output") or "")


def apply_llm_cluster_labels(
    clusters: List[Dict[str, Any]],
    *,
    complete: Optional[Callable[[str], str]] = None,
    timeout_sec: float = 10.0,
    mode: Optional[str] = None,
    contrastive: Optional[bool] = None,
    protected_terms: Optional[Any] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> int:
    """Replace deterministic labels with LLM labels where possible.

    The term label is preserved in metadata["term_label"] so nothing is lost
    and drift between labelers stays inspectable. Returns count relabeled.

    Contrastive by default: clusters are named in a deterministic order, each
    prompt carries its dimension's lens plus the names its nearest siblings
    already took, and a name that still collides is suffixed rather than
    duplicated. ``contrastive=False`` (or TOPOS_CLUSTER_LABEL_CONTRAST=off)
    runs the original isolated prompt — the control arm of the relabel eval.

    ``protected_terms`` is the owner's off-limits name set
    (``blackholed_name_terms``), passed by the callers that hold a connection.
    A label is a caller-agnostic stored artifact, so a protected name is
    refused at production rather than filtered per reader.

    ``stats``, when given, is filled with the retry and drop counts by reason.
    Without it, whether a retry rule fires at all is invisible from outside,
    and a rule that never triggers looks exactly like one that does nothing —
    the difference a measured no-op turns on.
    """
    from .vector_settings import cluster_llm_labels_mode

    resolved_mode = mode or cluster_llm_labels_mode()
    if resolved_mode == "off" or not clusters:
        return 0
    use_contrast = cluster_label_contrast_enabled() if contrastive is None else bool(contrastive)
    runner = complete or _complete_via_engine

    distinguishing = (
        compute_distinguishing_terms(clusters) if use_contrast else [[] for _ in clusters]
    )
    order = labeling_order(clusters) if use_contrast else list(range(len(clusters)))
    assigned: Dict[int, str] = {}
    used: Dict[str, int] = {}
    relabeled = 0
    retries = Counter()
    dropped_urls = 0
    dropped_protected = 0

    def _run(prompt: str) -> str:
        future = _LABEL_POOL.submit(runner, prompt)
        return future.result(timeout=timeout_sec)

    for index in order:
        cluster = clusters[index]
        terms = distinguishing[index]
        siblings: List[str] = []
        if use_contrast:
            siblings = sibling_labels(index, clusters, distinguishing, assigned)
            prompt = build_contrastive_label_prompt(
                cluster, distinguishing_terms=terms, siblings=siblings
            )
        else:
            prompt = build_label_prompt(cluster)
        try:
            text = _run(prompt)
            label = parse_label(text)
            reason = (
                _rejection_reason(label, used, protected_terms)
                if (use_contrast and label)
                else None
            )
            if reason:
                # One bounded retry naming what was wrong with the answer — a
                # link, a name a sibling already took, one built from the
                # banned vocabulary, or one that ignores the 2-5 word rule.
                # Never a third call: a second retry aimed at bare common nouns
                # was built and measured over 152 live clusters — it fired 25
                # times and moved rule compliance not at all (77.0% either
                # way), because the residual one-word labels are mostly real
                # names. A duplicate that survives is suffixed below; a link
                # that survives is dropped.
                retries[reason] += 1
                label = _better_label(
                    label,
                    parse_label(
                        _run(
                            build_contrastive_label_prompt(
                                cluster,
                                distinguishing_terms=terms,
                                siblings=siblings,
                                rejected_label=None if reason == "protected" else label,
                                rejected_reason=reason,
                            )
                        )
                    ),
                    used,
                    protected_terms,
                )
        except (FuturesTimeoutError, LabelerUnavailable, Exception) as exc:  # noqa: BLE001
            logger.info("cluster LLM labeling stopped (%s); term labels kept", exc)
            break  # one failure means the model is down — don't pay k timeouts
        if not label:
            continue
        # Uniqueness and the link gate are part of the contrastive contract,
        # not of the isolated prompt — keeping them out of that arm is what
        # makes the eval's control the behaviour that actually shipped.
        if use_contrast:
            if label_mentions_protected(label, protected_terms):
                dropped_protected += 1
                continue  # off-limits names are never minted, only refused
            if label_is_urlish(label):
                dropped_urls += 1
                continue  # a link is never a published label
            # Names to stay clear of: the ones assigned in this pass AND the
            # term labels still carried by clusters the labeler has not reached
            # or has skipped. Both shapes end up side by side on the same
            # surface, and the deterministic suffix is the same "(n)" — a live
            # relabel put an assigned "Hello (7)" next to a retained
            # "hello (7)" because only assigned names were being checked.
            blocked = dict(used)
            for other, sibling in enumerate(clusters):
                if other == index or other in assigned:
                    continue
                carried = _normalized_label(str(sibling.get("label") or ""))
                if carried:
                    blocked.setdefault(carried, other)
            if _normalized_label(label) in blocked:
                distinct = _disambiguated(label, terms, blocked, cluster)
                if distinct is None:
                    continue  # keep the term label rather than ship a duplicate
                label = distinct
            label = _presentable(label)
        metadata = dict(cluster.get("metadata") or {})
        # setdefault, not assign: on a relabel pass over clusters that already
        # carry an LLM label, assigning would overwrite the deterministic term
        # label with the previous model output and lose it for good.
        metadata.setdefault("term_label", cluster.get("label"))
        metadata["label_model"] = cluster_label_model()
        metadata["label_style"] = "contrastive" if use_contrast else "isolated"
        cluster["metadata"] = metadata
        cluster["label"] = label
        assigned[index] = label
        used[_normalized_label(label)] = index
        relabeled += 1
    if stats is not None:
        stats.update(
            {
                "retries_by_reason": dict(retries),
                "retry_calls": sum(retries.values()),
                "dropped_urls": dropped_urls,
                "dropped_protected": dropped_protected,
            }
        )
    if retries or dropped_urls or dropped_protected:
        logger.info(
            "cluster labeling retried %d duplicate, %d generic, %d link and %d "
            "off-limits answer(s); %d link and %d off-limits answer(s) dropped "
            "to the term label",
            retries["taken"],
            retries["generic"],
            retries["url"],
            retries["protected"],
            dropped_urls,
            dropped_protected,
        )
    return relabeled
