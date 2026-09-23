"""Native attribution vetoes survive signed authorization and fresh owner review."""
import pytest

from topos.permissions_v2.canonical import PolicyError
from tests.permissions_v2.test_evidence import corpus, attest, canonical_role, source_settings, runtime_source
from tests.permissions_v2.test_release import release_setup, issue, dispatch


@pytest.mark.parametrize("kind", ["fact_role", "leaf_role", "source_override", "source_runtime"])
@pytest.mark.parametrize("renew_review", [False, True])
def test_attribution_changes_after_signed_issuance_cannot_dispatch(release_setup, kind, renew_review):
    envelope, payload = issue(release_setup)
    corpus = release_setup[5]
    if kind == "fact_role": canonical_role(corpus,"signal_objects","inferred")
    elif kind == "leaf_role": canonical_role(corpus,"conversation_messages","observed")
    elif kind == "source_override": source_settings(corpus,"ambient")
    else: runtime_source(corpus,"ambient")
    if renew_review:
        attest(corpus,review_id="review-after-restriction")
    sent = []
    with pytest.raises(PolicyError):
        dispatch(release_setup,envelope,payload,send=lambda *args: sent.append(args))
    assert sent == []
