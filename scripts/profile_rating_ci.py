"""Temporary diagnostic branch only; never a substitute for release CI."""
import cProfile
import gc
import io
import json
from pathlib import Path
import pstats
import sys
import time
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TARGET = 'test_the_door_stays_within_the_render_budget_on_a_corpus_sized_fixture'

class Diagnostic:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection_modifyitems(self, config, items):
        yield
        index = next(i for i, item in enumerate(items) if TARGET in item.nodeid)
        removed = items[index + 1:]
        del items[index + 1:]
        config.hook.pytest_deselected(items=removed)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        if TARGET not in item.nodeid:
            yield
            return
        events = []
        def observe(phase, info):
            events.append({'at': time.perf_counter(), 'phase': phase, **info})
        gc.callbacks.append(observe)
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            yield
        finally:
            profiler.disable()
            gc.callbacks.remove(observe)
            profiler.dump_stats('rating-diagnostic.prof')
            text = io.StringIO()
            pstats.Stats(profiler, stream=text).strip_dirs().sort_stats('cumtime').print_stats(60)
            Path('rating-diagnostic.txt').write_text(text.getvalue())
            Path('rating-diagnostic.json').write_text(json.dumps(events, indent=2))
            print(text.getvalue())
            print('GC events:', json.dumps(events))

raise SystemExit(pytest.main(sys.argv[1:], plugins=[Diagnostic()]))
