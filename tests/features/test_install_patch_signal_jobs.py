"""signal_derivation_jobs must be user-patchable — the whitelist silently
dropped it (found configuring goal_extraction for the live grow stream)."""

from topos.sources.install_service import _PATCHABLE_DEFINITION_KEYS


def test_signal_derivation_jobs_is_patchable():
    assert "signal_derivation_jobs" in _PATCHABLE_DEFINITION_KEYS
