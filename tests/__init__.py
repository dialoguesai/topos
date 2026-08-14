"""Test suite package.

Intentionally a *regular* package (this file must exist), not an implicit
namespace package. Test modules import each other's fixtures absolutely
(``from tests.features.test_attention_triage import ...``), and a namespace
package loses name resolution to any regular ``tests`` package found anywhere
on sys.path. Anaconda ships exactly that — conda 24.x installs a top-level
``tests`` package into site-packages — so without this file the whole suite
fails collection with ``No module named 'tests.features'`` on a conda machine.
"""
