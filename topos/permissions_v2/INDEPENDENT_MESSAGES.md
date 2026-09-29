# Independently reviewed owner messages (p2c-v2)

This opt-in capability removes the qualifying-fact prerequisite. It does not
turn a source message into a fact or infer a profile claim from it. The existing
p2c-v1 grammar, evaluator and fact-backed eligibility stay intact.

## Authority and eligibility

- A new signed capability and evaluator identify the changed consent semantics.
- Every complete message requires live durable native origin, exact owner/source/
  body/time identity, native authored role, and an explicit whole-message owner
  review. Mutable canonical owner/role flags or raw receipts alone cannot qualify.
- Reviews bind the exact message revision and classification rubric. They share
  the existing enrolled review store's external rollback pin and clock. The
  private `message-review:` key is storage identity, not a canonical fact.
- Each category must satisfy the same allow clause. Highest sensitivity applies;
  existing source classification ceilings cannot be lowered by this review.
- Known copies, quotes, forwards, other people's messages, assistant text,
  unknown labels and NSFW records withhold. The owner confirms original wording
  and absence of protected information, including indirect references.
- Current per-record Off-limits and relevant sibling-fact restrictions remain
  independent vetoes. Deselected facts cannot reopen their backing messages.
  Message opt-outs also veto fact-backed raw release.
- A changed Off-limits list invalidates the human absence assessment. An unrelated
  record exclusion does not invalidate a message review. Having an unrelated
  protected person on the node does not itself prevent eligibility.
- Only qualified records enter the index. Review-state changes invalidate the v2
  index before ranking. Final release freshly rechecks proof, review, filters,
  protection, time, grant authority, expiry and revocation.
- At most 10 messages per search. Opaque IDs, timestamp precision, read budgets,
  signed requests/results, replay prevention and fresh CP consent checks remain.

## Owner surface

`permissions_v2_message_review` is registered owner-only. The local socket route
is `/v1/permissions-beta/v2/message-search/message-review`; the CP owner routes
are `/v1/permissions-beta/v2/evidence/messages/{operation}`. Operations are queue,
preview, record, opt_out and opt_in. Record uses exact-snapshot comparison and
compare-and-swap of the prior review revision. No caller can provide a database
path, source enrollment, recipient principal or trusted resolver.

The first queue scans at most 200 enrolled iMessage rows and returns at most 20
previews within a requested window of at most 31 days. It does not sync history.
The UI loads 10 recent previews without automatically reviewing any. The new
journey profile is `owner_message_search`; changing profile requires an explicit
new agreement while retaining the chosen filters. Older drafts remain v1.

## Limits and acceptance

This is an explicit-review implementation, not automatic classifier promotion.
The experimental local labeler remains unqualified as sole release authority.
Existing ChatGPT receipt matches remain diagnostics until native origin is
established; account ownership confirmation alone does not certify each message.
Safe span redaction, graph/fact/contact/calendar release and automatic semantic
entity-absence proof are not implemented by this capability.

The observed entity boundary catches names, recorded identity links and relevant
conversation/reply context. The human protected-content assessment covers
semantic/indirect references the observed boundary cannot prove absent. Neither
is a claim of perfect automatic semantic detection.

Synthetic signed-search and negative tests are engineering evidence. Acceptance
still requires actual recipient UI questions producing useful cited responses,
and a private audit of every released record against the owner's grant.
