"""GT-EN-P3-S1-03: GameLayer strategies and PublicResult."""

import pytest

from topos.query.game_layer import DefaultGameLayer, RevealStrategy
from topos.query.types import PublicResult

pytestmark = pytest.mark.gap


def test_all_game_layer_strategies_selectable() -> None:
    layer = DefaultGameLayer()
    for strategy in RevealStrategy:
        layer.reveal_strategy = strategy
        result = layer.apply(
            context_packet={"scores": [{"value": 0.5}]},
            access_mode="inference",
            scope_id="availability:read",
        )
        assert isinstance(result, PublicResult)
        assert result.strategy
