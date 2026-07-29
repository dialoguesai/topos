"""M3 / eval class C8: content about a protected entity may only be inferred securely.

D1 fixes the admissible set — the local adapters plus the Red Pill TEE for the
default tier, local only for `local_only`. BYOK and direct OpenAI are admitted
by neither.

The assertions here are about the *routing decision*, not the response text: a
gate that lets the payload reach a cloud adapter and then filters the answer has
already lost. So these check which provider the task ends up carrying, and that
an inadmissible one is never the answer.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.engine.engine import apply_blackhole_egress_policy
from topos.engine.tasks import ModelRequest, ProcessingTask
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_llm import evaluate, text_of
from topos.storage.db.migrations import apply_all_migrations

pytestmark = [pytest.mark.bhlr, pytest.mark.private]

PROTECTED = "Dana Qx71reyes"
ALIAS = "Dqx72nickname"
VISIBLE = "Sam Ok91okoye"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "llm.db"))
    apply_all_migrations(c)
    c.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json)
        VALUES ('ent-bh', 'person', ?, ?, ?)
        """,
        (PROTECTED, PROTECTED.lower(), f'["{ALIAS}"]'),
    )
    c.commit()
    BlackholeStore(c).blackhole_entity(entity_ref="ent-bh")
    # The policy reads the owner store through core.state; point it at this one.
    import topos.engine.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_policy_db_connection", lambda: (c, False))
    yield c
    c.close()


def _task(text: str, provider: str) -> ProcessingTask:
    return ProcessingTask(
        id="t-1",
        type="enrichment",
        subtype="topic_extraction",
        input={"text": text},
        model_request=ModelRequest(provider=provider, model="some-model"),
    )


# ------------------------------------------------------------- taint scan


def test_untainted_text_places_no_constraint(conn):
    verdict = evaluate(conn, {"text": f"lunch with {VISIBLE}"}, provider="openai")
    assert verdict.tainted is False
    assert verdict.provider == "openai"


def test_protected_name_taints(conn):
    verdict = evaluate(conn, {"text": f"lunch with {PROTECTED}"}, provider="openai")
    assert verdict.tainted is True


def test_alias_taints(conn):
    verdict = evaluate(conn, {"text": f"lunch with {ALIAS}"}, provider="openai")
    assert verdict.tainted is True


def test_taint_scan_reaches_nested_payloads(conn):
    """Task inputs are arbitrary dicts; a name buried in a list must still count."""
    payload = {"messages": [{"role": "user", "content": f"about {PROTECTED}"}], "n": 3}
    assert PROTECTED.lower() in text_of(payload).lower()
    assert evaluate(conn, payload, provider="openai").tainted is True


def test_no_database_means_no_constraint():
    """An engine with no store has no black holes; it must not refuse everything."""
    verdict = evaluate(None, {"text": PROTECTED}, provider="openai")
    assert verdict.tainted is False
    assert verdict.provider == "openai"


# ----------------------------------------------------- D1: admissible set


@pytest.mark.parametrize("provider", ["ollama", "huggingface", "redpill"])
def test_secure_providers_pass_through_untouched(conn, provider):
    """Red Pill's TEE counts as secure (D1), alongside the local adapters."""
    task, err = apply_blackhole_egress_policy(_task(f"about {PROTECTED}", provider))
    assert err is None
    assert task.model_request.provider == provider


@pytest.mark.parametrize("provider", ["openai", "anthropic", "grok", "platform"])
def test_cloud_providers_never_receive_protected_content(conn, provider):
    """The core claim of C8, asserted on the routing decision itself."""
    task, err = apply_blackhole_egress_policy(_task(f"about {PROTECTED}", provider))
    assert err is None, "a secure provider exists, so this should redirect not fail"
    assert task.model_request.provider != provider
    assert task.model_request.provider in {"ollama", "huggingface"}


def test_redirect_clears_the_model_name(conn):
    """The old model belonged to the old provider; carrying it over would break routing."""
    task, _ = apply_blackhole_egress_policy(_task(f"about {PROTECTED}", "openai"))
    assert task.model_request.model is None


def test_untainted_task_is_left_completely_alone(conn):
    task, err = apply_blackhole_egress_policy(_task(f"about {VISIBLE}", "openai"))
    assert err is None
    assert task.model_request.provider == "openai"
    assert task.model_request.model == "some-model"


def test_local_only_tier_excludes_redpill(conn):
    """The stricter dial: some entities may not leave the device at all."""
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-bh", processing_tier="local_only")

    task, err = apply_blackhole_egress_policy(_task(f"about {PROTECTED}", "redpill"))

    assert err is None
    assert task.model_request.provider in {"ollama", "huggingface"}


def test_strictest_tier_wins_when_several_entities_are_mentioned(conn):
    """Intersection, never union — one strict entity constrains the whole payload."""
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)
        VALUES ('ent-strict', 'person', 'Kit Qx80alvarez', 'kit qx80alvarez')
        """
    )
    conn.commit()
    BlackholeStore(conn).blackhole_entity(
        entity_ref="ent-strict", processing_tier="local_only"
    )

    verdict = evaluate(
        conn, {"text": f"{PROTECTED} and Kit Qx80alvarez"}, provider="redpill"
    )

    assert verdict.tainted is True
    assert "redpill" not in verdict.allowed_providers
    assert verdict.provider in {"ollama", "huggingface"}


# -------------------------------------------------------- failure posture


def test_sick_database_refuses_the_task_rather_than_routing_it(tmp_path):
    """A store that cannot be read must not be treated as 'nothing is protected'."""
    import topos.engine.engine as engine_mod

    class SickConn:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database disk image is malformed")

    original = engine_mod._policy_db_connection
    engine_mod._policy_db_connection = lambda: (SickConn(), False)
    try:
        task, err = apply_blackhole_egress_policy(_task("anything", "openai"))
    finally:
        engine_mod._policy_db_connection = original

    assert err is not None
    assert "blackhole_policy_unavailable" in err


def test_block_reason_names_no_entity(conn):
    """The reason may reach a log or a job record; it must not confirm who."""
    from topos.features.lifecycle.blackhole_llm import EgressVerdict, describe_block

    reason = describe_block(
        EgressVerdict(tainted=True, allowed_providers=frozenset(), matched_terms=(PROTECTED,))
    )

    assert PROTECTED.lower() not in reason.lower()
    assert ALIAS.lower() not in reason.lower()
