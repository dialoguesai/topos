from topos.canonicalization.resolver import CanonicalResolver, deterministic_id


def test_deterministic_id_is_stable():
    first = deterministic_id("chatgpt", "hello")
    second = deterministic_id("chatgpt", "hello")
    other = deterministic_id("chatgpt", "world")
    assert first == second
    assert first != other


def test_resolver_uses_content_hash():
    resolver = CanonicalResolver()
    payload = {"content": "hello", "source_id": "chatgpt"}
    record = resolver.resolve_message(payload)
    assert record.record_id == deterministic_id("chatgpt", "hello")
