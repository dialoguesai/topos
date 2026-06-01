We want to use local llms with Ollama to help decide which of the items in the source schema are going to be best used to help us derive belief-map items. 

Its sole job is to set the ideas for what items in the schema should be used for belief mapping. After it is selected, then every source can be used for it. The 3rd party can include which items are useful. The llm can create its own set. The user can override the default selections. The user can either include/or exclude the source from mapping to the belief map.


# Topos Belief Graph Architecture

## Purpose

The Topos Belief Graph Architecture defines how Topos models a person’s beliefs from heterogeneous source data while preserving provenance, uncertainty, temporality, and user control.

This architecture is designed to support:

* belief extraction from source data such as messages, AI conversations, social posts, notes, and imported records
* explainable links from beliefs back to raw evidence
* belief revision over time without mutating source truth
* user-authored corrections and additions
* selective sharing through Topos authorization and cognitive firewall layers
* future reasoning and agentic workflows built on top of the graph

The core recommendation is:

**Topos should use a temporal property graph as the operational model, governed by a knowledge-graph ontology, with provenance as a first-class requirement.**

---

## Design Principles

### 1. Source truth is immutable

Raw source data should never be overwritten by extracted beliefs, inferred claims, or user edits. Source records remain append-only.

### 2. Beliefs are modeled states, not raw facts

A belief node is not a transcript excerpt. It is the system’s current modeled representation of a person’s likely stance, preference, value, prediction, memory, goal, fear, or interpretation.

### 3. Provenance is mandatory

Every modeled belief must be traceable to one or more source items or source chunks, along with extraction method and confidence.

### 4. Beliefs evolve over time

Beliefs should strengthen, weaken, decay, split, merge, conflict, or become superseded as new evidence arrives.

### 5. User edits are additive

If a user corrects or adds a belief, that should create a new authored record and update the modeled state without changing the original source material or prior extraction history.

### 6. Graph semantics must stay disciplined

Topos should not allow arbitrary ad hoc node and edge types to accumulate. A clear ontology should govern allowed types and relationships.

### 7. Sharing is mediated

Beliefs are especially sensitive derived data. Access should be controlled separately from access to raw source data and should integrate with the Topos authorization and cognitive firewall systems.

---

## Recommended Structure

Topos Belief Graph uses a hybrid architecture:

### Core

* **Property graph** for operational storage and graph queries
* **Knowledge-graph ontology** for schema discipline and consistent semantics

### Required cross-cutting features

* temporal fields on nodes and edges
* provenance links to raw source data
* confidence and belief-strength scoring
* revision history
* user-authored overrides and annotations

### Optional future overlays

* evidence bundles for group-level support patterns
* probabilistic reasoning networks for advanced inference

---

## Layered Model

The architecture should be implemented as five conceptual layers.

### Layer 1: Source Layer

Stores raw imported or captured data in append-only form.

Examples:

* text messages
* email
* AI conversations
* social posts
* journal entries
* documents
* call transcripts
* imported structured records

### Layer 2: Semantic Extraction Layer

Transforms source content into chunks, entities, claims, topics, emotions, and other extraction artifacts.

### Layer 3: Belief Graph Layer

Stores propositions, beliefs, entities, topics, values, goals, and relationships among them.

### Layer 4: Belief State / Revision Layer

Tracks strengthening, weakening, contradiction, decay, supersession, merge, split, and user-authored changes.

### Layer 5: Access / Policy Layer

Applies authorization, cognitive firewall policies, and sharing rules to determine what can be exposed to internal agents, external agents, or other users.

---

## Core Object Distinctions

Topos should clearly separate the following concepts.

### Observed Statement

What appears in source material.

Example:

> “Sometimes I think I should leave this job.”

### Extracted Claim

What the system thinks that statement means.

Example:

> Speaker expresses dissatisfaction with current job and possible desire to exit.

### Modeled Belief

The current system-level belief state inferred from many observations over time.

Example:

> Moderate-strength belief that current career path feels unsustainable.

These must not be collapsed into one object.

---

## Graph Model Overview

The belief graph should contain typed nodes and typed edges.

### Primary node families

* source nodes
* semantic nodes
* belief nodes
* contextual nodes
* governance nodes

### Primary edge families

* provenance edges
* semantic relation edges
* belief relation edges
* temporal/revision edges
* access/policy edges

---

## Node Types

## 1. Source Nodes

### `SourceItem`

Represents a raw imported or captured item.

Fields:

* `id`
* `user_id`
* `source_type` — e.g. imessage, twitter, email, chatgpt, note, transcript
* `source_account_id`
* `thread_id`
* `author_id`
* `created_at`
* `captured_at`
* `raw_content`
* `content_hash`
* `metadata`
* `visibility_class`
* `schema_version`

### `SourceChunk`

Represents a chunked span of a source item used for extraction and evidence.

Fields:

* `id`
* `source_item_id`
* `chunk_index`
* `text`
* `start_offset`
* `end_offset`
* `embedding_ref`
* `token_count`
* `language`
* `created_at`

---

## 2. Semantic Nodes

### `Entity`

Represents a person, place, organization, product, concept, or named thing.

Fields:

* `id`
* `entity_type`
* `canonical_name`
* `aliases`
* `external_refs`
* `confidence`

### `Topic`

Represents a recurring domain of discussion.

Examples:

* career
* money
* trust
* health
* romance
* politics
* AI

Fields:

* `id`
* `canonical_name`
* `topic_family`
* `description`

### `EmotionState`

Represents an extracted affective signal or emotional pattern.

Fields:

* `id`
* `emotion_type`
* `polarity`
* `intensity`
* `confidence`
* `timespan_hint`

### `Proposition`

Represents a normalized claim extracted from one or more source statements.

Examples:

* user prefers autonomy over hierarchy
* user distrusts employer leadership
* user believes AI will reshape education

Fields:

* `id`
* `canonical_text`
* `subject_ref`
* `predicate`
* `object_ref`
* `qualifiers`
* `polarity`
* `modality`
* `first_seen_at`
* `last_seen_at`
* `confidence`

---

## 3. Belief Nodes

### `Belief`

Represents Topos’ current modeled belief state associated with a proposition.

Fields:

* `id`
* `user_id`
* `belief_type`
* `proposition_id`
* `status` — active, weak, conflicted, superseded, dormant, rejected
* `endorsement_strength` — how strongly the person appears to hold it
* `model_confidence` — confidence in the system’s belief model
* `explicitness` — explicit, implicit, inferred, user_authored
* `persistence_score`
* `salience_score`
* `contradiction_score`
* `source_diversity_score`
* `self_report_score`
* `decay_rate`
* `first_supported_at`
* `last_supported_at`
* `valid_from`
* `valid_to`
* `revision_number`
* `created_by` — pipeline, agent, user
* `schema_version`

### `BeliefFacet`

Optional node for representing a sub-aspect of a broader belief when needed.

Examples:

* broad belief: “I want to leave Austin”
* facets: cost pressure, social stagnation, career opportunity elsewhere

Fields:

* `id`
* `belief_id`
* `facet_type`
* `description`
* `confidence`

---

## 4. Contextual Nodes

### `Value`

Represents a core value inferred or declared by the user.

Examples:

* autonomy
* loyalty
* beauty
* truth
* stability
* adventure

Fields:

* `id`
* `canonical_name`
* `description`

### `Goal`

Represents a stated or inferred desired future state.

Fields:

* `id`
* `canonical_text`
* `goal_type`
* `time_horizon`
* `status`
* `confidence`

### `IdentityAspect`

Represents how the user sees themselves or wants to be seen.

Examples:

* writer
* founder
* father
* artist
* builder

Fields:

* `id`
* `canonical_name`
* `strength`
* `confidence`

### `InfluenceSource`

Represents a social or informational influence cluster.

Examples:

* close friends
* favorite author
* a particular online community
* recurring AI assistant patterns

Fields:

* `id`
* `influence_type`
* `label`
* `confidence`

---

## 5. Governance Nodes

### `EvidenceBundle`

Optional grouping node representing a collection of evidence items that jointly support or contradict a belief.

Fields:

* `id`
* `bundle_type`
* `description`
* `created_at`
* `confidence`

### `BeliefRevision`

Represents a logged update to a belief’s state.

Fields:

* `id`
* `belief_id`
* `timestamp`
* `change_type` — strengthen, weaken, contradict, supersede, merge, split, user_edit, decay
* `previous_snapshot`
* `new_snapshot`
* `cause_summary`
* `trigger_ref`
* `performed_by`

### `PolicyView`

Represents a policy-governed view of a belief or belief cluster for sharing.

Fields:

* `id`
* `policy_class`
* `audience_type`
* `transformation_type`
* `created_at`

---

## Edge Types

## 1. Provenance Edges

### `CHUNK_OF`

`SourceChunk -> SourceItem`

### `DERIVED_FROM`

`Proposition -> SourceChunk`
`Belief -> Proposition`

### `EVIDENCED_BY`

`Belief -> SourceChunk`

Edge fields:

* `support_type` — direct_support, indirect_support, contradiction, weak_signal, context_only
* `weight`
* `confidence`
* `extraction_method`
* `observed_at`

---

## 2. Semantic Relation Edges

### `MENTIONS`

`SourceChunk -> Entity | Topic`

### `ABOUT`

`Proposition | Belief -> Entity | Topic | Value | Goal | IdentityAspect`

### `EXPRESSES`

`SourceChunk -> EmotionState`

### `INFLUENCED_BY`

`Belief -> InfluenceSource`

---

## 3. Belief Relation Edges

### `SUPPORTS`

`Belief -> Belief`

### `CONTRADICTS`

`Belief -> Belief`

### `REFINES`

`Belief -> Belief`

### `IMPLIES`

`Belief -> Belief`

### `GENERALIZES`

`Belief -> Belief`

### `DEPENDS_ON`

`Belief -> Belief | Goal | Value | IdentityAspect`

Edge fields:

* `weight`
* `confidence`
* `active_from`
* `active_to`
* `evidence_count`

---

## 4. Temporal / Revision Edges

### `SUPERSEDES`

`Belief -> Belief`

### `SPLIT_INTO`

`Belief -> Belief`

### `MERGED_INTO`

`Belief -> Belief`

### `REVISED_BY`

`Belief -> BeliefRevision`

---

## 5. Access / Policy Edges

### `VISIBLE_TO_POLICY`

`Belief | Topic | Cluster -> PolicyView`

### `REDACTS`

`PolicyView -> Belief | EvidenceBundle | SourceChunk`

### `SUMMARIZES`

`PolicyView -> Belief | BeliefCluster`

These edges support Topos cognitive firewall behavior and permissioned sharing.

---

## Belief Types

Topos should begin with a constrained belief-type vocabulary.

Recommended initial belief types:

* `preference`
* `value`
* `goal`
* `prediction`
* `identity`
* `social_belief`
* `normative_belief`
* `memory_like_belief`
* `fear`
* `desire`
* `interpretation`
* `suspicion`

Examples:

* preference: “I prefer living in smaller cities.”
* value: “Autonomy matters more than prestige.”
* goal: “I want to start a company.”
* prediction: “AI will reshape my industry.”
* identity: “I am a writer.”
* social_belief: “My friend doesn’t really trust me.”
* normative_belief: “People should be more direct.”

---

## Required Scoring Model

Each belief should maintain a standard scoring profile.

### `endorsement_strength`

How strongly the person appears to hold the belief.

### `model_confidence`

How confident the system is that the belief exists.

### `persistence_score`

How stable the belief appears over time.

### `salience_score`

How central the belief appears in the person’s current life context.

### `contradiction_score`

How much counter-evidence exists.

### `source_diversity_score`

How many independent source channels support the belief.

### `self_report_score`

How directly the belief was stated by the user.

### `decay_rate`

How quickly the belief weakens when not reinforced.

These scores should be recomputed or updated as new evidence arrives.

---

## Temporal Model

Beliefs should be treated as time-sensitive modeled states.

Recommended temporal fields on belief nodes:

* `first_supported_at`
* `last_supported_at`
* `valid_from`
* `valid_to`
* `revision_number`
* `status`

Recommended temporal fields on edges:

* `observed_at`
* `active_from`
* `active_to`

### Temporal rules

1. New evidence can strengthen an active belief.
2. Contradictory evidence can weaken or conflict an active belief.
3. Lack of reinforcement over time can decay a belief.
4. A broad belief can split into narrower beliefs.
5. Two narrow beliefs can merge into a broader one.
6. A prior belief can be superseded by a revised model.

---

## Revision Model

Every meaningful change to a belief should create a revision log.

### Example change events

* extracted new direct support
* extracted contradiction
* updated confidence after multiple sources aligned
* user explicitly corrected the belief
* belief decayed below threshold
* belief split into sub-beliefs

### Why revisions matter

* auditability
* explainability
* user trust
* rollback/debugging
* future model retraining

---

## User Authored Beliefs and Corrections

Topos should allow the user to directly author or correct beliefs.

### Rules

1. User edits do not mutate raw source data.
2. User edits do not erase earlier extracted beliefs.
3. User-authored beliefs create new records with provenance.
4. The graph may elevate user-authored inputs above inferred signals when computing current belief state.
5. A user may mark a belief as wrong, outdated, too sensitive, or context-dependent.

### Recommended authored node behavior

A user-authored correction can either:

* create a new `Belief` with `explicitness = user_authored`
* create a `BeliefRevision` on an existing belief
* create a `CONTRADICTS` or `SUPERSEDES` relation against a system-generated belief

---

## Ingestion and Update Pipeline

A recommended pipeline for building and maintaining the belief graph:

### Step 1: Capture source data

Ingest source items as immutable records.

### Step 2: Chunk and embed

Create source chunks and embeddings for retrieval and extraction.

### Step 3: Extract semantic artifacts

Extract entities, topics, emotion signals, candidate claims, and candidate propositions.

### Step 4: Normalize propositions

Resolve semantically similar claims into canonical propositions where appropriate.

### Step 5: Update beliefs

Create or update belief nodes based on proposition support patterns, contradictions, temporality, and score thresholds.

### Step 6: Update edges

Recompute support, contradiction, implication, aboutness, and evidence edges.

### Step 7: Write revision log

Store belief revisions for all material state changes.

### Step 8: Recompute policy views

Recalculate what should be visible under different sharing and cognitive firewall policies.

---

## Suggested Threshold Logic

The exact thresholds can evolve, but Topos should include explicit state transitions.

### Candidate belief lifecycle

* `candidate`
* `active`
* `weak`
* `conflicted`
* `dormant`
* `superseded`
* `rejected`

### Example transitions

* candidate -> active when repeated direct or indirect support crosses confidence threshold
* active -> conflicted when contradiction score rises above threshold
* active -> weak when decay plus low recent reinforcement lowers endorsement
* active -> superseded when a better structured replacement belief is created
* any -> rejected when user explicitly rejects it

---

## Example JSON Shapes

## SourceItem

```json
{
  "id": "src_001",
  "user_id": "user_123",
  "source_type": "imessage",
  "thread_id": "thread_88",
  "author_id": "user_123",
  "created_at": "2026-03-18T12:33:00Z",
  "raw_content": "I feel more and more like I need to leave Austin.",
  "metadata": {
    "participants": ["user_123", "friend_9"]
  },
  "schema_version": "1.0"
}
```

## Proposition

```json
{
  "id": "prop_014",
  "canonical_text": "User desires leaving Austin",
  "subject_ref": "user_123",
  "predicate": "desires_leaving",
  "object_ref": "place_austin",
  "qualifiers": {
    "intensity": "moderate"
  },
  "polarity": "positive_toward_departure",
  "modality": "expressed_desire",
  "first_seen_at": "2026-03-18T12:33:00Z",
  "last_seen_at": "2026-03-18T12:33:00Z",
  "confidence": 0.82
}
```

## Belief

```json
{
  "id": "belief_022",
  "user_id": "user_123",
  "belief_type": "goal",
  "proposition_id": "prop_014",
  "status": "active",
  "endorsement_strength": 0.71,
  "model_confidence": 0.79,
  "explicitness": "explicit",
  "persistence_score": 0.58,
  "salience_score": 0.73,
  "contradiction_score": 0.18,
  "source_diversity_score": 0.42,
  "self_report_score": 0.94,
  "decay_rate": 0.02,
  "first_supported_at": "2026-02-20T11:00:00Z",
  "last_supported_at": "2026-03-18T12:33:00Z",
  "valid_from": "2026-02-20T11:00:00Z",
  "valid_to": null,
  "revision_number": 3,
  "created_by": "belief_pipeline",
  "schema_version": "1.0"
}
```

## BeliefEvidence edge payload

```json
{
  "belief_id": "belief_022",
  "source_chunk_id": "chunk_991",
  "support_type": "direct_support",
  "weight": 0.84,
  "confidence": 0.87,
  "extraction_method": "llm_claim_extractor_v2",
  "observed_at": "2026-03-18T12:33:00Z"
}
```

## BeliefRevision

```json
{
  "id": "brev_090",
  "belief_id": "belief_022",
  "timestamp": "2026-03-18T12:34:20Z",
  "change_type": "strengthen",
  "previous_snapshot": {
    "endorsement_strength": 0.63,
    "revision_number": 2
  },
  "new_snapshot": {
    "endorsement_strength": 0.71,
    "revision_number": 3
  },
  "cause_summary": "New direct self-report message aligned with prior evidence.",
  "trigger_ref": "chunk_991",
  "performed_by": "belief_pipeline"
}
```

---

## Example Graph Pattern

A simple graph neighborhood might look like this:

```text
SourceItem -> SourceChunk -> Proposition -> Belief
                               |
                               -> ABOUT -> Topic: relocation
                               -> ABOUT -> Entity: Austin
Belief -> EVIDENCED_BY -> SourceChunk
Belief -> SUPPORTS -> Belief: user values change
Belief -> CONTRADICTS -> Belief: user wants stability in current city
Belief -> REVISED_BY -> BeliefRevision
Belief -> VISIBLE_TO_POLICY -> PolicyView
```

---

## Storage Recommendation

Topos does not need a single storage technology for every concern, but it should expose a unified graph abstraction.

### Recommended persistence split

* relational/document storage for source items, chunks, revisions, and extraction artifacts
* graph storage or graph tables for belief nodes and edges
* vector index for chunk and proposition retrieval

### Logical contract

Regardless of implementation details, the application should treat the belief system as a single typed graph with provenance-aware queries.

---

## Query Patterns Topos Should Support

Examples of first-class queries:

### Explainability

* Why does Topos think the user believes X?
* Which sources most strongly support this belief?
* Which evidence contradicts it?

### Temporal

* Which beliefs strengthened in the last 30 days?
* Which beliefs have decayed?
* Which beliefs became conflicted recently?

### Structural

* What beliefs cluster around money, work, or relationships?
* What values support this goal?
* Which goals conflict with each other?

### Sharing / policy

* Which beliefs are safe to share with this agent?
* Which beliefs require summarization rather than raw exposure?
* Which beliefs were user-marked as private or sensitive?

---

## Cognitive Firewall Integration

Belief data is derived, compressed, and often more sensitive than raw text. The cognitive firewall should therefore be able to operate at the belief-graph layer as well as the raw-data layer.

### Firewall actions should include

* allow raw belief exposure
* allow only summarized exposure
* allow only topic-level exposure
* redact evidence links
* hide user-authored corrections
* block sensitive identity, health, political, or relational inferences
* require confidence threshold before external sharing

### Example

A third-party agent may be allowed to know:

* user is interested in relocation

But not allowed to know:

* exact messages that imply the user distrusts a particular person
* confidence-weighted inferred romantic beliefs
* private user-authored contradictions

---

## What Topos Should Not Do

### 1. Do not overwrite source truth

No belief extraction should mutate the original source item.

### 2. Do not collapse extraction and belief modeling into one record

This harms explainability and trust.

### 3. Do not allow unlimited free-form node types

Schema drift will make the system brittle.

### 4. Do not treat all beliefs as equally stable

Some are transient moods, some are deep values.

### 5. Do not expose inferred beliefs without policy review

Derived beliefs can be more sensitive than the underlying text.

---

## Recommended V1 Scope

Topos V1 should include:

* SourceItem and SourceChunk
* Entity, Topic, Proposition, Belief
* BeliefRevision
* EVIDENCED_BY, ABOUT, SUPPORTS, CONTRADICTS, SUPERSEDES
* scoring model
* temporal fields
* user-authored corrections
* basic policy views for sharing

Topos V1 should not require:

* full hypergraph implementation
* full Bayesian network implementation
* perfect proposition canonicalization
* fully automated ontology expansion

---

## Recommended V2 Additions

* EvidenceBundle nodes
* belief clustering and centrality measures
* richer identity and value modeling
* influence-source modeling
* multi-agent policy-specific views
* probabilistic reasoning overlay for selected domains

---

## Final Recommendation

Topos Belief Graph should be implemented as a **temporal, provenance-rich property graph with ontology-governed node and edge types**.

This architecture gives Topos:

* flexibility across many source types
* explainability back to evidence
* support for belief evolution over time
* user control without source corruption
* compatibility with authorization and cognitive firewall systems
* a strong foundation for future reasoning and agent workflows

In practical terms, Topos should be able to say not merely:

> The user believes X.

But rather:

> Across multiple source items over time, Topos inferred a medium-strength active belief that the user values autonomy over stability, with explicit self-report support, moderate contradiction from recent financial-anxiety messages, and restricted sharing under current cognitive firewall policy.
