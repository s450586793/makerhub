import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PARSER_ROOT = ROOT / "app/services/makerworld_parsers"
PARSER_PACKAGE = "app.services.makerworld_parsers"
BROWSER_FETCH_SYMBOL = "app.services.cloakbrowser_session.browser_fetch"
HTTP_MODULES = ("requests", "httpx", "aiohttp")
HTTP_CLIENT_NAMES = {"client", "http", "http_client", "session"}
PARSER_ALLOWED_MODULES = frozenset({
    "__future__",
    "bs4",
    "hashlib",
    "html",
    "json",
    "re",
    "typing",
    "urllib.parse",
    "app.services.makerworld_parsers.comments",
    "app.services.makerworld_parsers.common",
    "app.services.makerworld_parsers.listing",
    "app.services.makerworld_parsers.model",
    "app.services.three_mf",
})


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


PARSER_FILES = _python_files(PARSER_ROOT)
PIPELINE_FILES = _python_files(ROOT / "app/services/makerworld_pipeline")
CONTROL_GET_FILES = (
    ROOT / "app/services/source_library.py",
    ROOT / "app/services/archive_worker.py",
    ROOT / "app/services/subscriptions.py",
    ROOT / "app/services/online_accounts.py",
    ROOT / "app/services/asset_downloader.py",
    ROOT / "app/services/process_jobs.py",
    ROOT / "app/services/source_health.py",
    ROOT / "app/services/runtime_engine/archive_adapter.py",
    *PIPELINE_FILES,
)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _parser_package(path: Path) -> str:
    try:
        relative_parent = path.relative_to(PARSER_ROOT).parent
    except ValueError:
        return PARSER_PACKAGE
    if relative_parent == Path("."):
        return PARSER_PACKAGE
    return ".".join((PARSER_PACKAGE, *relative_parent.parts))


def _resolved_from_import_base(path: Path, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module

    package_parts = _parser_package(path).split(".")
    base_length = len(package_parts) - node.level + 1
    if base_length <= 0:
        return None
    base = ".".join(package_parts[:base_length])
    return f"{base}.{node.module}" if node.module else base


def _disallowed_parser_dependencies(path: Path) -> set[str]:
    violations: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            violations.update(alias.name for alias in node.names if alias.name not in PARSER_ALLOWED_MODULES)
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_from_import_base(path, node)
            if module is None:
                violations.add(f"unresolved relative import level {node.level}")
                continue
            if node.module is None:
                for alias in node.names:
                    imported_module = f"{module}.{alias.name}"
                    if alias.name == "*" or imported_module not in PARSER_ALLOWED_MODULES:
                        violations.add(imported_module)
                continue
            if module not in PARSER_ALLOWED_MODULES:
                violations.add(module)
                violations.update(
                    f"{module}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
    violations.update(
        f"dynamic:{finding.symbol}"
        for finding in _transport_findings(path).dynamic_import_calls
    )
    return violations


@dataclass(frozen=True)
class _AstFinding:
    lineno: int
    col_offset: int
    function: str
    symbol: str
    node: ast.AST


class _TransportBoundaryVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scopes: list[dict[str, str]] = [{}]
        self._string_scopes: list[dict[str, str | None]] = [{}]
        self._functions: list[str] = []
        self.browser_fetch_references: list[_AstFinding] = []
        self.direct_get_calls: list[_AstFinding] = []
        self.dynamic_import_calls: list[_AstFinding] = []
        self._browser_positions: set[tuple[int, int]] = set()
        self._get_positions: set[tuple[int, int]] = set()
        self._dynamic_import_positions: set[tuple[int, int]] = set()

    def _bind(self, name: str, symbol: str, string_value: str | None = None) -> None:
        self._scopes[-1][name] = symbol
        self._string_scopes[-1][name] = string_value

    def _lookup(self, name: str) -> str:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return name

    def _string_value(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            for scope in reversed(self._string_scopes):
                if node.id in scope:
                    return scope[node.id]
        return None

    def _qualified_name(self, node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Attribute):
            base = self._qualified_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            dynamic = self._dynamic_attribute(node)
            if dynamic:
                return dynamic
            return self._qualified_name(node.func)
        if isinstance(node, ast.Await):
            return self._qualified_name(node.value)
        return ""

    def _dynamic_attribute(self, node: ast.Call) -> str:
        if not isinstance(node.func, ast.Name) or node.func.id != "getattr" or len(node.args) < 2:
            return ""
        attribute = self._string_value(node.args[1])
        if attribute is None:
            return ""
        base = self._qualified_name(node.args[0])
        return f"{base}.{attribute}" if base else ""

    @staticmethod
    def _is_http_symbol(symbol: str) -> bool:
        if symbol in HTTP_CLIENT_NAMES or symbol in {f"local.{name}" for name in HTTP_CLIENT_NAMES}:
            return True
        client_symbols = {
            "requests.Session",
            "requests.session",
            "requests.sessions",
            "requests.sessions.Session",
            "requests.api",
            "httpx.Client",
            "httpx.AsyncClient",
            "aiohttp.ClientSession",
        }
        return symbol in {*HTTP_MODULES, *client_symbols}

    @staticmethod
    def _is_browser_fetch_symbol(symbol: str) -> bool:
        return symbol == "browser_fetch" or symbol.endswith(".browser_fetch")

    @staticmethod
    def _looks_like_http_factory(symbol: str) -> bool:
        name = symbol.rsplit(".", 1)[-1].strip("_").lower()
        tokens = name.split("_")
        return (
            len(tokens) >= 2
            and tokens[0] in {"build", "create", "make", "new", "open"}
            and tokens[-1] in {"client", "session"}
        )

    @staticmethod
    def _request_may_be_get(node: ast.Call) -> bool:
        method: ast.AST | None = node.args[0] if node.args else None
        if method is None:
            method = next((keyword.value for keyword in node.keywords if keyword.arg == "method"), None)
        if isinstance(method, ast.Constant):
            return str(method.value).upper() == "GET"
        return True

    def _record_browser_reference(self, node: ast.AST, symbol: str) -> None:
        position = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if position in self._browser_positions:
            return
        self._browser_positions.add(position)
        self.browser_fetch_references.append(
            _AstFinding(*position, self._functions[-1] if self._functions else "", symbol, node)
        )

    def _record_direct_get(self, node: ast.Call, symbol: str) -> None:
        position = (node.lineno, node.col_offset)
        if position in self._get_positions:
            return
        self._get_positions.add(position)
        self.direct_get_calls.append(
            _AstFinding(*position, self._functions[-1] if self._functions else "", symbol, node)
        )

    def _record_dynamic_import(self, node: ast.Call, symbol: str) -> None:
        position = (node.lineno, node.col_offset)
        if position in self._dynamic_import_positions:
            return
        self._dynamic_import_positions.add(position)
        self.dynamic_import_calls.append(
            _AstFinding(*position, self._functions[-1] if self._functions else "", symbol, node)
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", 1)[0]
            self._bind(bound_name, alias.name if alias.asname else bound_name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            return
        for alias in node.names:
            if alias.name == "*":
                if node.module in {
                    BROWSER_FETCH_SYMBOL.rsplit(".", 1)[0],
                    "app.services.makerworld_browser_client",
                }:
                    self._record_browser_reference(node, BROWSER_FETCH_SYMBOL)
                continue
            symbol = f"{node.module}.{alias.name}"
            self._bind(alias.asname or alias.name, symbol)
            if self._is_browser_fetch_symbol(symbol):
                self._record_browser_reference(node, symbol)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *(item for item in node.args.kw_defaults if item)):
            self.visit(default)
        if node.returns:
            self.visit(node.returns)
        return_symbol = self._qualified_name(node.returns)
        if self._is_http_symbol(return_symbol):
            self._bind(node.name, return_symbol)
        elif self._looks_like_http_factory(node.name):
            self._bind(node.name, "local.session")
        else:
            self._bind(node.name, f"function.{node.name}")
        self._functions.append(node.name)
        self._scopes.append({})
        self._string_scopes.append({})
        arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if node.args.vararg:
            arguments += (node.args.vararg,)
        if node.args.kwarg:
            arguments += (node.args.kwarg,)
        for argument in arguments:
            if argument.annotation:
                self.visit(argument.annotation)
            annotation = self._qualified_name(argument.annotation)
            self._bind(argument.arg, annotation or f"local.{argument.arg}")
        for statement in node.body:
            self.visit(statement)
        self._string_scopes.pop()
        self._scopes.pop()
        self._functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        symbol = self._qualified_name(node.value)
        string_value = self._string_value(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if target.id in HTTP_CLIENT_NAMES and not self._is_http_symbol(symbol):
                    assigned_symbol = f"local.{target.id}"
                elif self._looks_like_http_factory(symbol):
                    assigned_symbol = "local.session"
                else:
                    assigned_symbol = symbol or f"local.{target.id}"
                self._bind(target.id, assigned_symbol, string_value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value:
            self.visit(node.value)
        if isinstance(node.target, ast.Name):
            symbol = self._qualified_name(node.value) or self._qualified_name(node.annotation)
            if node.target.id in HTTP_CLIENT_NAMES and not self._is_http_symbol(symbol):
                symbol = f"local.{node.target.id}"
            elif self._looks_like_http_factory(symbol):
                symbol = "local.session"
            self._bind(node.target.id, symbol or f"local.{node.target.id}", self._string_value(node.value))

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if isinstance(item.optional_vars, ast.Name):
                symbol = self._qualified_name(item.context_expr)
                self._bind(item.optional_vars.id, symbol or f"local.{item.optional_vars.id}")
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_Name(self, node: ast.Name) -> None:
        symbol = self._qualified_name(node)
        if isinstance(node.ctx, ast.Load) and self._is_browser_fetch_symbol(symbol):
            self._record_browser_reference(node, symbol)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        symbol = self._qualified_name(node)
        if isinstance(node.ctx, ast.Load) and self._is_browser_fetch_symbol(symbol):
            self._record_browser_reference(node, symbol)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        dynamic_symbol = self._dynamic_attribute(node)
        if self._is_browser_fetch_symbol(dynamic_symbol):
            self._record_browser_reference(node, dynamic_symbol)
        if dynamic_symbol.endswith(".get") and self._is_http_symbol(dynamic_symbol.rsplit(".", 1)[0]):
            self._record_direct_get(node, dynamic_symbol)

        symbol = self._qualified_name(node.func)
        if symbol in {"__import__", "builtins.__import__", "importlib.import_module"}:
            self._record_dynamic_import(node, symbol)
        if symbol.endswith(".get") and self._is_http_symbol(symbol.rsplit(".", 1)[0]):
            self._record_direct_get(node, symbol)
        elif (
            symbol.endswith(".request")
            and self._is_http_symbol(symbol.rsplit(".", 1)[0])
            and self._request_may_be_get(node)
        ):
            self._record_direct_get(node, symbol)
        self.generic_visit(node)


def _transport_findings(path: Path) -> _TransportBoundaryVisitor:
    visitor = _TransportBoundaryVisitor()
    visitor.visit(_tree(path))
    return visitor


def _browser_fetch_references(path: Path) -> list[_AstFinding]:
    return _transport_findings(path).browser_fetch_references


def _direct_http_gets(path: Path) -> list[_AstFinding]:
    return _transport_findings(path).direct_get_calls


def _keyword_map(node: ast.Call) -> dict[str, ast.AST]:
    return {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}


def _first_argument_name(node: ast.Call) -> str:
    return node.args[0].id if len(node.args) == 1 and isinstance(node.args[0], ast.Name) else ""


def _literal_value(node: ast.AST | None, expected) -> bool:
    try:
        return ast.literal_eval(node) == expected
    except (ValueError, TypeError):
        return False


def _ticket_params_are_exact(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Dict)
        and len(node.keys) == 1
        and isinstance(node.keys[0], ast.Constant)
        and node.keys[0].value == "ticket"
        and isinstance(node.values[0], ast.Name)
        and node.values[0].id == "ticket"
    )


def _is_named_session_get(finding: _AstFinding) -> bool:
    return (
        isinstance(finding.node, ast.Call)
        and isinstance(finding.node.func, ast.Attribute)
        and finding.symbol == "requests.Session.get"
    )


def _direct_get_exception_kind(path: Path, finding: _AstFinding) -> str:
    if not _is_named_session_get(finding):
        return ""
    node = finding.node
    if path == Path("app/services/asset_downloader.py"):
        keywords = _keyword_map(node)
        allowed = (
            finding.function == "download_file"
            and _first_argument_name(node) == "url"
            and set(keywords) == {"stream", "timeout"}
            and isinstance(keywords["timeout"], ast.Name)
            and keywords["timeout"].id == "timeout"
            and isinstance(keywords.get("stream"), ast.Constant)
            and keywords["stream"].value is True
        )
        return "asset-stream" if allowed else ""
    if path != Path("app/services/online_accounts.py") or finding.function != "_exchange_makerworld_ticket":
        return ""

    first_argument = _first_argument_name(node)
    keywords = _keyword_map(node)
    if first_argument == "ticket_url":
        allowed = set(keywords) == {"proxies", "timeout"} and _literal_value(keywords["timeout"], (8, 20))
        return "account-ticket" if allowed else ""
    if first_argument != "makerworld_ticket_url" or set(keywords) != {
        "params",
        "proxies",
        "timeout",
        "allow_redirects",
    }:
        return ""
    allowed = (
        _ticket_params_are_exact(keywords["params"])
        and _literal_value(keywords["timeout"], (8, 20))
        and _literal_value(keywords["allow_redirects"], True)
    )
    return "makerworld-ticket" if allowed else ""


def _unapproved_direct_gets(path: Path, logical_path: Path | None = None) -> list[_AstFinding]:
    relative_path = logical_path
    if relative_path is None:
        try:
            relative_path = path.relative_to(ROOT)
        except ValueError:
            relative_path = path
    findings = _direct_http_gets(path)
    expected_counts = {
        Path("app/services/asset_downloader.py"): {"asset-stream": 1},
        Path("app/services/online_accounts.py"): {"account-ticket": 1, "makerworld-ticket": 1},
    }.get(relative_path, {})
    findings_by_kind = {
        kind: [finding for finding in findings if _direct_get_exception_kind(relative_path, finding) == kind]
        for kind in expected_counts
    }
    contract_violations = [
        _AstFinding(
            0,
            0,
            "<contract>",
            f"contract violation: expected {expected_count} {kind} GET call(s), found {len(findings_by_kind[kind])}",
            ast.Constant(value=None),
        )
        for kind, expected_count in expected_counts.items()
        if len(findings_by_kind[kind]) != expected_count
    ]
    allowed_ids = {
        id(finding)
        for kind, expected_count in expected_counts.items()
        if len(findings_by_kind[kind]) == expected_count
        for finding in findings_by_kind[kind]
    }
    return [finding for finding in findings if id(finding) not in allowed_ids] + contract_violations


def _finding_summary(findings: list[_AstFinding]) -> list[str]:
    return [
        f"line {finding.lineno} ({finding.function or '<module>'}): {finding.symbol}"
        for finding in findings
    ]


@pytest.mark.parametrize(
    ("source", "expected_lineno"),
    (
        (
            "from app.services.cloakbrowser_session import browser_fetch as fetch\nfetch('cn', 'https://example.test')\n",
            2,
        ),
        (
            "import app.services.cloakbrowser_session as bridge\nbridge.browser_fetch('cn', 'https://example.test')\n",
            2,
        ),
        (
            "from app.services import cloakbrowser_session as bridge\ncallback = bridge.browser_fetch\n",
            2,
        ),
        (
            "from app.services.cloakbrowser_session import browser_fetch\ncallback = browser_fetch\n",
            2,
        ),
        (
            "from app.services.cloakbrowser_session import browser_fetch\nregister(browser_fetch)\n",
            2,
        ),
        (
            "from app.services.cloakbrowser_session import browser_fetch\ndef run(callback=browser_fetch):\n    pass\n",
            2,
        ),
        (
            "from app.services.makerworld_browser_client import browser_fetch as fetch\ncallback = fetch\n",
            2,
        ),
        (
            "import app.services.makerworld_browser_client as client\ncallback = client.browser_fetch\n",
            2,
        ),
    ),
    ids=(
        "from-import-alias",
        "module-attribute",
        "module-from-alias",
        "assigned-reference",
        "forwarded-reference",
        "default-argument-reference",
        "wrapper-from-import",
        "wrapper-module-attribute",
    ),
)
def test_browser_fetch_guard_detects_reference_mutations(tmp_path, source, expected_lineno):
    path = tmp_path / "mutated_service.py"
    path.write_text(source, encoding="utf-8")

    assert expected_lineno in {finding.lineno for finding in _browser_fetch_references(path)}


def test_browser_fetch_guard_resolves_constant_getattr_name(tmp_path):
    path = tmp_path / "mutated_service.py"
    path.write_text(
        "import app.services.cloakbrowser_session as bridge\n"
        "name = 'browser_fetch'\n"
        "callback = getattr(bridge, name)\n",
        encoding="utf-8",
    )

    assert 3 in {finding.lineno for finding in _browser_fetch_references(path)}


@pytest.mark.parametrize(
    "source",
    (
        "import requests as http\nhttp.get('https://example.test')\n",
        "import requests\nrequests.Session().get('https://example.test')\n",
        "import requests\nrequests.request('GET', 'https://example.test')\n",
        "import requests\ngetattr(requests, 'get')('https://example.test')\n",
        "from requests import get as fetch\nfetch('https://example.test')\n",
        "import requests as http\nfetch = http.get\nfetch('https://example.test')\n",
        "from requests import request as send\nsend(method='GET', url='https://example.test')\n",
        "import requests\ndef fetch(method):\n    return requests.request(method, 'https://example.test')\n",
        (
            "async def fetch(client):\n"
            "    return await client.get('https://example.test')\n"
        ),
    ),
    ids=(
        "module-alias",
        "session-constructor",
        "request-method",
        "dynamic-getattr",
        "from-import-alias",
        "assigned-method-reference",
        "request-from-import-alias",
        "dynamic-request-method",
        "await-client",
    ),
)
def test_direct_get_guard_detects_call_mutations(tmp_path, source):
    path = tmp_path / "mutated_service.py"
    path.write_text(source, encoding="utf-8")

    assert _direct_http_gets(path)


def test_direct_get_guard_ignores_mapping_access_and_explicit_post(tmp_path):
    path = tmp_path / "ordinary_gets.py"
    path.write_text(
        "import requests\n"
        "def read(data, session: requests.Session):\n"
        "    data.get('key')\n"
        "    session.headers.get('User-Agent')\n"
        "    session.request('POST', 'https://example.test')\n",
        encoding="utf-8",
    )

    assert not _direct_http_gets(path)


@pytest.mark.parametrize(
    "source",
    (
        (
            "def _make_session():\n"
            "    return object()\n"
            "session = _make_session()\n"
            "session.get('https://example.test')\n"
        ),
        (
            "def _make_session():\n"
            "    return object()\n"
            "transport = _make_session()\n"
            "transport.get('https://example.test')\n"
        ),
        (
            "def build():\n"
            "    return object()\n"
            "client = build()\n"
            "client.get('https://example.test')\n"
        ),
        (
            "import requests\n"
            "def build() -> requests.Session:\n"
            "    return requests.Session()\n"
            "transport = build()\n"
            "transport.get('https://example.test')\n"
        ),
    ),
    ids=("session-target", "known-factory", "client-target", "return-annotation"),
)
def test_direct_get_guard_preserves_factory_http_origin(tmp_path, source):
    path = tmp_path / "mutated_service.py"
    path.write_text(source, encoding="utf-8")

    assert _direct_http_gets(path)


def test_direct_get_guard_resolves_constant_getattr_name(tmp_path):
    path = tmp_path / "mutated_service.py"
    path.write_text(
        "def _make_session():\n"
        "    return object()\n"
        "session = _make_session()\n"
        "name = 'get'\n"
        "getattr(session, name)('https://example.test')\n",
        encoding="utf-8",
    )

    assert _direct_http_gets(path)


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("import requests as http\n", "requests"),
        ("import requests.sessions as http\n", "requests.sessions"),
        ("from urllib import request as http\n", "urllib.request"),
        ("from app.core import store as state\n", "app.core.store"),
        ("import app.core.database_json_state as state\n", "app.core.database_json_state"),
    ),
    ids=("requests-alias", "requests-submodule", "urllib-from-alias", "store-from-alias", "database-state"),
)
def test_parser_guard_detects_dependency_import_mutations(tmp_path, source, expected):
    path = tmp_path / "mutated_parser.py"
    path.write_text(source, encoding="utf-8")

    assert expected in _disallowed_parser_dependencies(path)


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("import socket\n", "socket"),
        ("import urllib3\n", "urllib3"),
        ("import asyncpg\n", "asyncpg"),
        ("import sqlite3\n", "sqlite3"),
        ("import importlib as loader\nloader.import_module('json')\n", "importlib"),
        ("module = __import__('json')\n", "dynamic:__import__"),
        (
            "import app.services.makerworld_parsers.transport\n",
            "app.services.makerworld_parsers.transport",
        ),
        ("import app.services.three_mf.transport\n", "app.services.three_mf.transport"),
    ),
    ids=(
        "socket",
        "urllib3",
        "asyncpg",
        "sqlite3",
        "importlib",
        "dunder-import",
        "unknown-parser-module",
        "unknown-three-mf-module",
    ),
)
def test_parser_guard_rejects_non_allowlisted_and_dynamic_imports(tmp_path, source, expected):
    path = tmp_path / "mutated_parser.py"
    path.write_text(source, encoding="utf-8")

    assert expected in _disallowed_parser_dependencies(path)


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("from . import transport\n", "app.services.makerworld_parsers.transport"),
        (
            "from .. import makerworld_browser_client as network\n",
            "app.services.makerworld_browser_client",
        ),
        ("from ..... import transport\n", "unresolved relative import level 5"),
    ),
    ids=("sibling-module", "parent-service-module", "unresolved-parent-module"),
)
def test_parser_guard_rejects_relative_import_mutations(tmp_path, source, expected):
    path = tmp_path / "mutated_parser.py"
    path.write_text(source, encoding="utf-8")

    assert expected in _disallowed_parser_dependencies(path)


def test_python_file_discovery_is_recursive(tmp_path):
    nested = tmp_path / "nested" / "parser.py"
    nested.parent.mkdir()
    nested.write_text("VALUE = 1\n", encoding="utf-8")

    assert nested in _python_files(tmp_path)


def test_static_asset_stream_get_is_the_only_asset_downloader_exception(tmp_path):
    path = tmp_path / "asset_downloader.py"
    path.write_text(
        "import requests\n"
        "def download_file(session: requests.Session, url, control_url):\n"
        "    session.get(url, timeout=timeout, stream=True)\n"
        "    session.get(control_url)\n",
        encoding="utf-8",
    )

    findings = _unapproved_direct_gets(path, Path("app/services/asset_downloader.py"))

    assert [finding.lineno for finding in findings] == [4]


def test_asset_downloader_rejects_a_duplicate_static_stream_get(tmp_path):
    path = tmp_path / "asset_downloader.py"
    path.write_text(
        "import requests\n"
        "def download_file(session: requests.Session, url):\n"
        "    session.get(url, timeout=timeout, stream=True)\n"
        "    session.get(url, timeout=timeout, stream=True)\n",
        encoding="utf-8",
    )

    findings = _unapproved_direct_gets(path, Path("app/services/asset_downloader.py"))

    assert any("contract violation" in finding.symbol for finding in findings)


def test_asset_downloader_rejects_an_incomplete_stream_get_shape(tmp_path):
    path = tmp_path / "asset_downloader.py"
    path.write_text(
        "import requests\n"
        "def download_file(session: requests.Session, url):\n"
        "    session.get(url, stream=True)\n",
        encoding="utf-8",
    )

    assert _unapproved_direct_gets(path, Path("app/services/asset_downloader.py"))


def test_asset_downloader_rejects_a_missing_static_stream_get(tmp_path):
    path = tmp_path / "asset_downloader.py"
    path.write_text(
        "import requests\n"
        "def download_file(session: requests.Session, url):\n"
        "    return None\n",
        encoding="utf-8",
    )

    findings = _unapproved_direct_gets(path, Path("app/services/asset_downloader.py"))

    assert any("contract violation" in finding.symbol for finding in findings)


def test_ticket_gets_are_the_only_online_account_exceptions(tmp_path):
    path = tmp_path / "online_accounts.py"
    path.write_text(
        "import requests\n"
        "def _exchange_makerworld_ticket(session: requests.Session, ticket_url, makerworld_ticket_url, control_url, ticket):\n"
        "    session.get(ticket_url, proxies=None, timeout=(8, 20))\n"
        "    session.get(makerworld_ticket_url, params={'ticket': ticket}, proxies=None, timeout=(8, 20), allow_redirects=True)\n"
        "    session.get(control_url)\n",
        encoding="utf-8",
    )

    findings = _unapproved_direct_gets(path, Path("app/services/online_accounts.py"))

    assert [finding.lineno for finding in findings] == [5]


def test_online_accounts_rejects_a_duplicate_ticket_get(tmp_path):
    path = tmp_path / "online_accounts.py"
    path.write_text(
        "import requests\n"
        "def _exchange_makerworld_ticket(session: requests.Session, ticket_url, makerworld_ticket_url, ticket):\n"
        "    session.get(ticket_url, proxies=None, timeout=(8, 20))\n"
        "    session.get(ticket_url, proxies=None, timeout=(8, 20))\n"
        "    session.get(makerworld_ticket_url, params={'ticket': ticket}, proxies=None, timeout=(8, 20), allow_redirects=True)\n",
        encoding="utf-8",
    )

    findings = _unapproved_direct_gets(path, Path("app/services/online_accounts.py"))

    assert any("contract violation" in finding.symbol for finding in findings)


def test_online_accounts_rejects_a_changed_ticket_get_shape(tmp_path):
    path = tmp_path / "online_accounts.py"
    path.write_text(
        "import requests\n"
        "def _exchange_makerworld_ticket(session: requests.Session, ticket_url, makerworld_ticket_url, ticket):\n"
        "    session.get(ticket_url, proxies=None, timeout=(8, 20))\n"
        "    session.get(makerworld_ticket_url, params={'ticket': ticket}, proxies=None, timeout=(8, 20), allow_redirects=False)\n",
        encoding="utf-8",
    )

    assert _unapproved_direct_gets(path, Path("app/services/online_accounts.py"))


@pytest.mark.parametrize(
    "source",
    (
        "import requests\n"
        "def _exchange_makerworld_ticket(session: requests.Session, ticket_url, makerworld_ticket_url, ticket):\n"
        "    return None\n",
        "import requests\n"
        "def _exchange_makerworld_ticket(session: requests.Session, ticket_url, makerworld_ticket_url, ticket):\n"
        "    session.get(ticket_url, proxies=None, timeout=(8, 20))\n",
    ),
    ids=("zero-ticket-gets", "one-ticket-get"),
)
def test_online_accounts_rejects_missing_ticket_gets(tmp_path, source):
    path = tmp_path / "online_accounts.py"
    path.write_text(source, encoding="utf-8")

    findings = _unapproved_direct_gets(path, Path("app/services/online_accounts.py"))

    assert any("contract violation" in finding.symbol for finding in findings)


def test_parsers_have_no_network_or_store_dependencies():
    for path in PARSER_FILES:
        violations = _disallowed_parser_dependencies(path)
        assert not violations, f"{path}: {sorted(violations)}"


def test_only_browser_client_calls_browser_fetch():
    service_files = _python_files(ROOT / "app/services")
    callers = {path.relative_to(ROOT) for path in service_files if _browser_fetch_references(path)}
    assert callers == {Path("app/services/makerworld_browser_client.py")}


def test_control_plane_does_not_issue_unapproved_direct_gets():
    for path in CONTROL_GET_FILES:
        findings = _unapproved_direct_gets(path)
        assert not findings, f"{path}: {_finding_summary(findings)}"
