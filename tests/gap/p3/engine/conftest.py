"""Shared fixtures for engine Phase 3 gap tests."""

from __future__ import annotations

import pytest

from helpers import make_adapter_bundle


@pytest.fixture
def adapter_bundle():
    return make_adapter_bundle()
