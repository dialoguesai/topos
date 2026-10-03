"""No file outside ``topos/permissions_v2/switches.py`` reads a sharing switch from the environment or from settings.

Any-to-any N1. The walk parses every Python file of the ``topos`` package; it never greps, so a name held in a
constant, imported from another module, or looked up through an alias of ``os.environ`` is found as surely as a
literal one. Outside the switch module it refuses:

1. **spelling a switch name**: a string that is one (a docstring or a message that mentions one is fine), or a
   computed one (``f"TOPOS_PERMISSIONS_V2_{x}"``, ``"TOPOS_PERMISSIONS_V2_" + x``). Every other file takes the
   name from the module, so the table stays the one place a name is written;
2. **looking a switch up**: ``.get``, ``.pop``, ``.setdefault``, ``getenv``, a subscript or ``in``, on anything,
   with a key that resolves to a switch name: a literal, a constant of the same module, one imported from another
   module of the package, a module's attribute, or ``switches.X.name``;
3. **in sharing code** (``topos/permissions_v2/``, the ``permissions_v2*`` handlers and the ``permissions_*`` owner
   routes): any read of the environment (``os.environ``, an alias of it, or a mapping handed in as ``env``) whose
   key the walk cannot pin to fixed names, so a switch cannot come back through a variable or a helper;
4. **settings**: a field named after a switch (pydantic would fill ``topos_permissions_v2_x`` from
   ``TOPOS_PERMISSIONS_V2_X``), an attribute of that name, or such a name as a string (``getattr``).

The second half of this file runs the same walk over small sources that each read a switch one way, including
the three parsers 1.4.4 had, and over sources that must pass, so the guard cannot pass by finding nothing.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import topos
from topos.permissions_v2 import switches

PACKAGE = Path(topos.__file__).resolve().parent
MODULE = PACKAGE / "permissions_v2" / "switches.py"
SWITCHES_MODULE = "topos.permissions_v2.switches"
NAME_SHAPE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TAIL = re.compile(r"(?:^|[^A-Za-z0-9_])" + re.escape(switches.PREFIX) + r"[A-Z0-9_]*$")
SETTINGS_NAMES = frozenset(name.lower() for name in switches.BY_NAME)
LOOKUPS = ("get", "pop", "setdefault")
#: What sharing code calls an environment it was handed (``def enabled(env=None)``).
ENVIRONMENT_NAMES = frozenset({"env", "environ", "environment"})
#: The files that read a switch at 1.4.4 (inventory E1 §4: 28 names, 19 files) and the one related setting.
FORMER_READERS = (
    "permissions_v2/runtime.py", "permissions_v2/search_transport.py", "core/handlers/permissions_v2.py",
    "api/source_retention.py", "api/permissions_native_probe.py", "permissions_v2/release_transport.py",
    "permissions_v2/fact_release_transport.py", "permissions_v2/refresh_loop.py",
    "permissions_v2/protection_doorbell.py", "permissions_v2/evidence_families.py",
    "permissions_v2/journal_goal_field.py", "permissions_v2/interest_index.py", "permissions_v2/interest_relabel.py",
    "permissions_v2/inferred_facts.py", "permissions_v2/permitted_derivation.py",
    "permissions_v2/entailment_grounding.py", "permissions_v2/search_timing.py", "permissions_v2/shadow_index.py",
    "permissions_v2/shadow_rescore.py", "permissions_v2/ai_chat_capture.py",
)


def is_switch_name(value) -> bool:
    return isinstance(value, str) and (value in switches.BY_NAME
                                       or (value.startswith(switches.PREFIX) and bool(NAME_SHAPE.match(value))))


def is_settings_name(value) -> bool:
    return isinstance(value, str) and (value in SETTINGS_NAMES or value.startswith(switches.PREFIX.lower()))


def sharing_code(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return (relative.startswith("permissions_v2/")
            or (relative.startswith("core/handlers/") and name.startswith("permissions_v2"))
            or (relative.startswith("api/") and name.startswith("permissions_")))


# --- the index: every module's constants and imports ---------------------------------------------------------------

class Module:
    def __init__(self, name: str, tree: ast.Module, package: bool):
        self.name, self.tree = name, tree
        self.consts: dict[str, list] = {}       # module-level name -> the expressions bound to it
        self.imports: dict[str, tuple] = {}     # local name -> ("module", dotted) | ("name", dotted, name)
        self.os_names, self.environ_names, self.getenv_names = set(), set(), set()
        base = name.split(".") if package else name.split(".")[:-1]
        self.base = base

    def absolute(self, level: int, module: str | None) -> str:
        parts = self.base[:len(self.base) - (level - 1)] if level else []
        return ".".join(parts + ([module] if module else [])) if level else (module or "")


class Index:
    def __init__(self, package: Path):
        self.modules: dict[str, Module] = {}
        for path in sorted(package.rglob("*.py")):
            self.add(path, path.read_text(encoding="utf-8"))

    @staticmethod
    def name_of(path: Path) -> tuple[str, bool]:
        relative = path.resolve().relative_to(PACKAGE.parent).with_suffix("")
        parts = list(relative.parts)
        package = parts[-1] == "__init__"
        if package:
            parts = parts[:-1]
        return ".".join(parts), package

    def add(self, path: Path, source: str, name: str | None = None) -> Module:
        dotted, package = self.name_of(path) if name is None else (name, False)
        module = Module(dotted, ast.parse(source, filename=str(path)), package)
        for statement in module.tree.body:
            self._bind(module, statement)
        self.modules[dotted] = module
        return module

    def _bind(self, module: Module, statement):
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    module.consts.setdefault(target.id, []).append(statement.value)
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value:
            module.consts.setdefault(statement.target.id, []).append(statement.value)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            self._import(module, statement)
        elif isinstance(statement, (ast.If, ast.Try)):
            for child in ast.iter_child_nodes(statement):
                if isinstance(child, ast.stmt):
                    self._bind(module, child)
            for handler in getattr(statement, "handlers", ()):
                for child in handler.body:
                    self._bind(module, child)

    @staticmethod
    def _import(module: Module, statement):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == "os":
                    module.os_names.add(alias.asname or "os")
                if alias.asname:
                    module.imports[alias.asname] = ("module", alias.name)
                else:
                    module.imports[alias.name.split(".")[0]] = ("module", alias.name.split(".")[0])
            return
        source = module.absolute(statement.level, statement.module)
        for alias in statement.names:
            local = alias.asname or alias.name
            if source == "os" and alias.name == "environ":
                module.environ_names.add(local)
            if source == "os" and alias.name == "getenv":
                module.getenv_names.add(local)
            module.imports[local] = ("name", source, alias.name)

    def module_of(self, expr, module: Module) -> str | None:
        """The dotted module an expression names, when it is an imported module."""
        if isinstance(expr, ast.Name):
            bound = module.imports.get(expr.id)
            if bound is None:
                return None
            if bound[0] == "module":
                return bound[1]
            dotted = f"{bound[1]}.{bound[2]}" if bound[1] else bound[2]
            return dotted if dotted in self.modules else None
        if isinstance(expr, ast.Attribute):
            parent = self.module_of(expr.value, module)
            if parent is not None and f"{parent}.{expr.attr}" in self.modules:
                return f"{parent}.{expr.attr}"
        return None

    def switch_object(self, expr, module: Module):
        """The switches.Switch an expression names (``switches.X`` or an imported ``X``), else None."""
        if isinstance(expr, ast.Attribute) and self.module_of(expr.value, module) == SWITCHES_MODULE:
            found = getattr(switches, expr.attr, None)
            return found if isinstance(found, switches.Switch) else None
        if isinstance(expr, ast.Name):
            bound = module.imports.get(expr.id)
            if bound and bound[0] == "name" and bound[1] == SWITCHES_MODULE:
                found = getattr(switches, bound[2], None)
                return found if isinstance(found, switches.Switch) else None
        return None

    def values(self, expr, module: Module, scope=None, seen=frozenset()):
        """Every string the expression can be (a frozenset), or None when the walk cannot pin it down."""
        if isinstance(expr, ast.Constant):
            return frozenset({expr.value}) if isinstance(expr.value, str) else frozenset()
        if isinstance(expr, ast.Name):
            if scope is not None and expr.id in scope:
                return self._union(scope[expr.id], module, seen)
            return self.global_values(module.name, expr.id, seen)
        if isinstance(expr, ast.Attribute):
            if expr.attr == "name":
                item = self.switch_object(expr.value, module)
                if item is not None:
                    return frozenset({item.name})
            target = self.module_of(expr.value, module)
            if target is not None:
                return self.global_values(target, expr.attr, seen)
            return None
        if isinstance(expr, ast.IfExp):
            return self._union([expr.body, expr.orelse], module, seen, scope)
        return None

    def _union(self, exprs, module, seen, scope=None):
        out = set()
        for item in exprs:
            if item is None:
                return None
            found = self.values(item, module, scope, seen)
            if found is None:
                return None
            out |= found
        return frozenset(out)

    def global_values(self, dotted: str, name: str, seen=frozenset()):
        key = (dotted, name)
        module = self.modules.get(dotted)
        if module is None or key in seen:
            return None
        seen = seen | {key}
        if name in module.consts:
            return self._union(module.consts[name], module, seen)
        bound = module.imports.get(name)
        if bound is None:
            return None
        if bound[0] == "module":
            return frozenset()             # a module is not a name
        source, imported = bound[1], bound[2]
        if f"{source}.{imported}" in self.modules:
            return frozenset()             # `from package import module`
        return self.global_values(source, imported, seen)


# --- the walk ------------------------------------------------------------------------------------------------------

def _scope(function) -> dict:
    """A function's own bindings: name -> the expressions bound to it (None for one the walk cannot see)."""
    scope: dict[str, list] = {}
    arguments = function.args
    for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg,
                     arguments.kwarg):
        if argument is not None:
            scope.setdefault(argument.arg, []).append(None)

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            scope.setdefault(name.id, []).append(child.value if target is name else None)
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and isinstance(child.target,
                                                                                                    ast.Name):
                value = child.value if isinstance(child, (ast.AnnAssign, ast.NamedExpr)) else None
                scope.setdefault(child.target.id, []).append(value)
            elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
                literal = isinstance(child.iter, (ast.Tuple, ast.List, ast.Set))
                for name in ast.walk(child.target):
                    if isinstance(name, ast.Name):
                        scope.setdefault(name.id, []).extend(
                            list(child.iter.elts) if literal and child.target is name else [None])
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    for name in ast.walk(item.optional_vars) if item.optional_vars is not None else ():
                        if isinstance(name, ast.Name):
                            scope.setdefault(name.id, []).append(None)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                scope.setdefault(child.name, []).append(None)
            walk(child)

    walk(function)
    return scope


class Walk(ast.NodeVisitor):
    def __init__(self, index: Index, module: Module, relative: str):
        self.index, self.module, self.relative = index, module, relative
        self.sharing = sharing_code(relative)
        self.findings: list[str] = []
        self.scopes: list = [None]
        self.texts = {id(node.value) for node in ast.walk(module.tree)
                      if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                      and isinstance(node.value.value, str)}

    # what counts as the environment
    def mentions_environ(self, expr) -> bool:
        for node in ast.walk(expr):
            if isinstance(node, ast.Attribute) and node.attr == "environ":
                return True
            if isinstance(node, ast.Name) and node.id in self.module.environ_names:
                return True
        return False

    def is_environment(self, expr) -> bool:
        if self.mentions_environ(expr) and not isinstance(expr, ast.Call):
            return True
        if isinstance(expr, ast.Name):
            if expr.id in ENVIRONMENT_NAMES:
                return True
            scope = self.scopes[-1]
            bindings = scope.get(expr.id) if scope is not None and expr.id in scope else self.module.consts.get(expr.id)
            return bool(bindings) and any(item is not None and self.mentions_environ(item) for item in bindings)
        return False

    def is_getenv(self, func) -> bool:
        if isinstance(func, ast.Attribute) and func.attr == "getenv":
            return isinstance(func.value, ast.Name) and (func.value.id in self.module.os_names or func.value.id == "os")
        return isinstance(func, ast.Name) and func.id in self.module.getenv_names

    def flag(self, node, why: str):
        self.findings.append(f"{self.relative}:{getattr(node, 'lineno', 0)}: {why}")

    def check(self, node, key, environment: bool):
        found = self.index.values(key, self.module, self.scopes[-1])
        if found is None:
            if environment and self.sharing:
                self.flag(node, "reads the environment with a key the guard cannot pin down")
            return
        if any(is_switch_name(value) for value in found):
            self.flag(node, "looks up a sharing switch outside the switch module")

    # scopes
    def visit_FunctionDef(self, node):
        self.scopes.append(_scope(node))
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.scopes.append({argument.arg: [None] for argument in node.args.args})
        self.generic_visit(node)
        self.scopes.pop()

    # reads
    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in LOOKUPS and node.args:
            self.check(node, node.args[0], self.is_environment(func.value))
        elif self.is_getenv(func) and node.args:
            self.check(node, node.args[0], True)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.ctx, ast.Load):
            self.check(node, node.slice, self.is_environment(node.value))
        self.generic_visit(node)

    def visit_Compare(self, node):
        for operator, right in zip(node.ops, node.comparators):
            if isinstance(operator, (ast.In, ast.NotIn)):
                self.check(node, node.left, self.is_environment(right))
        self.generic_visit(node)

    # names
    def visit_Constant(self, node):
        if id(node) not in self.texts:
            if is_switch_name(node.value):
                self.flag(node, "spells a sharing switch name outside the switch module")
            elif is_settings_name(node.value):
                self.flag(node, "names a settings field after a sharing switch")

    def visit_JoinedStr(self, node):
        for part, following in zip(node.values, node.values[1:]):
            if (isinstance(part, ast.Constant) and isinstance(part.value, str) and TAIL.search(part.value)
                    and isinstance(following, ast.FormattedValue)):
                self.flag(node, "computes a sharing switch name")
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                self.visit(part)

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.Add) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) \
                and TAIL.search(node.left.value):
            self.flag(node, "computes a sharing switch name")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if is_settings_name(node.attr):
            self.flag(node, "reads a settings field named after a sharing switch")
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        for statement in node.body:
            targets = (statement.targets if isinstance(statement, ast.Assign)
                       else [statement.target] if isinstance(statement, ast.AnnAssign) else [])
            for target in targets:
                if isinstance(target, ast.Name) and is_settings_name(target.id.lower()):
                    self.flag(statement, "declares a settings field named after a sharing switch")
        self.generic_visit(node)


def findings_in(index: Index, module: Module, relative: str) -> list[str]:
    walk = Walk(index, module, relative)
    walk.visit(module.tree)
    return walk.findings


@pytest.fixture(scope="module")
def index() -> Index:
    return Index(PACKAGE)


def test_no_file_outside_the_switch_module_reads_a_sharing_switch(index):
    found, walked = [], 0
    for dotted, module in sorted(index.modules.items()):
        if dotted == SWITCHES_MODULE:
            continue
        walked += 1
        relative = dotted.removeprefix("topos.").replace(".", "/")
        found += findings_in(index, module, relative + ".py")
    assert found == []
    assert walked > 500 and SWITCHES_MODULE in index.modules   # the whole package, not a corner of it


def test_every_file_that_read_a_switch_in_1_4_4_asks_the_module_now(index):
    for relative in FORMER_READERS:
        dotted = "topos." + relative.removesuffix(".py").replace("/", ".")
        module = index.modules[dotted]
        imports = {local for local, bound in module.imports.items()
                   if bound == ("name", "topos.permissions_v2", "switches")
                   or bound == ("module", SWITCHES_MODULE)}
        calls = [node for node in ast.walk(module.tree) if isinstance(node, ast.Attribute)
                 and isinstance(node.value, ast.Name) and node.value.id == "switches"]
        imported_inside = any(isinstance(node, ast.ImportFrom) and any(alias.name == "switches" for alias in node.names)
                              for node in ast.walk(module.tree))
        assert (imports or imported_inside) and calls, relative


def test_the_ten_files_mirrored_with_the_control_plane_were_not_touched(index):
    """The brief: a switch read inside one of them would have stopped the step; none reads one, none imports one."""
    for name in ("contract", "fact_contract", "search_contract", "knowledge_contract", "registry", "signing",
                 "protocol", "forwarding", "identity_protocol", "ingest_protocol"):
        module = index.modules[f"topos.permissions_v2.{name}"]
        assert "switches" not in module.imports, name
        assert findings_in(index, module, f"permissions_v2/{name}.py") == []


# --- the guard finds what it must, and nothing else ------------------------------------------------------------------

def _run(index: Index, source: str, relative: str) -> list[str]:
    dotted = "topos." + relative.removesuffix(".py").replace("/", ".")
    module = index.add(PACKAGE / relative, source, name=dotted)
    try:
        return findings_in(index, module, relative)
    finally:
        index.modules.pop(dotted, None)


READS = {
    # the three parsers 1.4.4 had, as they were written
    "exact_true": 'import os\ndef f():\n    return os.environ.get("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "").lower() == "true"\n',
    "truthy_set": ('import os\nJOURNAL_FLAG = "TOPOS_PERMISSIONS_V2_JOURNAL_SOURCES"\nclass Family:\n    flag = None\n'
                   '    def enabled(self, env=None):\n'
                   '        return str((os.environ if env is None else env).get(self.flag, "")).strip().lower() in ("1", "true")\n'),
    "on_unless_off": ('import os\n_OFF = ("0", "false", "off", "no")\ndef enabled():\n'
                      '    return os.environ.get("TOPOS_PERMISSIONS_V2_AUTO_RESYNC", "on").strip().lower() not in _OFF\n'),
    "default_true": ('import os\nFLAG = "TOPOS_PERMISSIONS_V2_EVIDENCE_REVIEWS_ENABLED"\ndef f():\n'
                     '    return os.environ.get(FLAG, "true").lower() != "true"\n'),
    # names held elsewhere
    "module_constant": 'import os\nFLAG = "TOPOS_PERMISSIONS_V2_X"\ndef f():\n    return os.getenv(FLAG)\n',
    "imported_constant": ('import os\nfrom topos.permissions_v2.search_transport import FLAG\n'
                          'def f():\n    return os.environ.get(FLAG)\n'),
    "imported_module_attribute": ('import os\nfrom topos.permissions_v2 import search_transport\n'
                                  'def f():\n    return os.environ[search_transport.BATCH_FLAG]\n'),
    "the_module_name": ('import os\nfrom topos.permissions_v2 import switches\n'
                        'def f():\n    return os.environ.get(switches.MESSAGE_SEARCH.name)\n'),
    "an_imported_row": ('import os\nfrom topos.permissions_v2.switches import JOURNAL_SOURCES\n'
                        'def f():\n    return os.environ.get(JOURNAL_SOURCES.name)\n'),
    "alias_of_environ": ('import os\nfrom topos.permissions_v2.evidence_families import JOURNAL_FLAG\n'
                         'def f(env=None):\n    env = os.environ if env is None else env\n    return env.get(JOURNAL_FLAG)\n'),
    "any_mapping": ('from topos.permissions_v2.interest_relabel import FLAG\n'
                    'def f(env):\n    return str(env.get(FLAG, "")).strip().lower()\n'),
    "membership": 'import os\ndef f():\n    return "TOPOS_PERMISSIONS_V2_AUTO_RESYNC" in os.environ\n',
    "from_os_import": 'from os import environ, getenv\ndef f():\n    return environ.get("TOPOS_OWNER_CAPTURE_APP_IDS") or getenv("x")\n',
    "f_string": 'import os\ndef f(part):\n    return os.environ.get(f"TOPOS_PERMISSIONS_V2_{part}")\n',
    "concatenation": 'import os\ndef f(part):\n    return os.environ.get("TOPOS_PERMISSIONS_V2_" + part)\n',
    "settings_attribute": 'from topos.config.settings import settings\ndef f():\n    return settings.topos_permissions_v2_enabled\n',
    "settings_getattr": ('from topos.config.settings import settings\n'
                         'def f():\n    return getattr(settings, "topos_permissions_v2_message_search_enabled")\n'),
    "settings_field": 'class Settings:\n    topos_permissions_v2_journal_sources: bool = False\n',
}
#: A relative import resolves only where it is written, so this one is run inside the sharing package.
RELATIVE = 'import os\nfrom .interest_index import FLAG as SOURCES\ndef f():\n    return os.getenv(SOURCES)\n'
#: Reads the guard refuses in sharing code only: a key it cannot pin down could be a switch.
SHARING_ONLY = {
    "unknown_key": 'import os\ndef f(name):\n    return os.environ.get(name)\n',
    "a_helper_handed_the_name": 'def _flag(name, env):\n    return env.get(name, "").lower() == "true"\n',
    "unknown_key_subscript": 'import os\ndef f(name):\n    return os.environ[name]\n',
}
CLEAN = {
    "docstring": '"""Off unless TOPOS_PERMISSIONS_V2_ENABLED is on."""\ndef f():\n    """TOPOS_PERMISSIONS_V2_X=true"""\n',
    "message": 'import logging\ndef f():\n    logging.info("doorbell off (TOPOS_PERMISSIONS_V2_AUTO_RESYNC)")\n',
    "the_module": 'from . import switches\ndef f():\n    return switches.on(switches.MESSAGE_SEARCH)\n',
    "a_constant_from_the_module": 'from . import switches\nFLAG = switches.MESSAGE_SEARCH.name\n',
    "other_variables": 'import os\ndef f():\n    return os.environ.get("WEB_CONCURRENCY", "1")\n',
    "a_literal_loop": ('import os\ndef f():\n    for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):\n'
                       '        os.environ.get(variable, "1")\n'),
}


@pytest.mark.parametrize("case", sorted(READS))
@pytest.mark.parametrize("where", ["permissions_v2/n1_guard_probe.py", "features/n1_guard_probe.py"])
def test_the_guard_finds_every_way_of_reading_a_switch(index, case, where):
    assert _run(index, READS[case], where), case


def test_the_guard_follows_a_relative_import(index):
    assert _run(index, RELATIVE, "permissions_v2/n1_guard_probe.py")


@pytest.mark.parametrize("case", sorted(SHARING_ONLY))
def test_in_sharing_code_an_environment_read_must_name_its_key(index, case):
    assert _run(index, SHARING_ONLY[case], "permissions_v2/n1_guard_probe.py"), case
    assert _run(index, SHARING_ONLY[case], "features/n1_guard_probe.py") == [], case


@pytest.mark.parametrize("case", sorted(CLEAN))
@pytest.mark.parametrize("where", ["permissions_v2/n1_guard_probe.py", "features/n1_guard_probe.py"])
def test_the_guard_passes_what_is_not_a_switch_read(index, case, where):
    assert _run(index, CLEAN[case], where) == [], case
