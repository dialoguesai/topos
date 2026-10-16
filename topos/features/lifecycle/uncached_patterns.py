"""NR178: regular expressions built from the Off-limits list, compiled without the `re` module's own cache.

`re.compile`, and every module-level `re.search`/`re.sub` given a pattern string, keeps the compiled pattern in a
process-wide cache keyed by the pattern's TEXT (`re._cache`; 3.13 adds `re._cache2`), up to 512 patterns, until other
patterns push it out. A pattern built from protected names (``sam|abe|...``) holds those names, so compiling it that
way keeps them in the process past the request it was made for, whatever the caller does with the pattern
afterwards (owner rule: nothing derived from the Off-limits list or excluded entities outlives a request).

`compile_uncached` compiles through the compiler `re` itself calls, skipping only the cache: `re._compiler.compile` on
Python 3.11 and later, `sre_compile.compile` on 3.10 (where `re._compiler` does not exist; `sre_compile` is
deprecated from 3.11, so it is never imported there). The pattern object is the same type, built by the same code,
and matches exactly as `re.compile`'s does. A private API, so a test holds it: after a match, no key of any `re`
cache holds the pattern text, on every Python the node supports.

The caller decides how long the compiled pattern lives: on an object made for one request, or in a memory scoped to
one pass (`blackhole_rebuild.term_patterns`)."""
from __future__ import annotations

import re

try:  # Python 3.11 and later
    from re import _compiler as _sre_compiler
except ImportError:  # Python 3.10
    import sre_compile as _sre_compiler


def compile_uncached(pattern: str, flags: int = 0) -> "re.Pattern[str]":
    """`re.compile(pattern, flags)`, kept in no cache of the `re` module."""
    if not isinstance(pattern, str):
        raise TypeError("compile_uncached takes a pattern string")
    return _sre_compiler.compile(pattern, int(flags))
