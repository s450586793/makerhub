import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROWSER_FETCH_SYMBOL = "app.services.cloakbrowser_session.browser_fetch"
HTTP_MODULES = ("requests", "httpx", "aiohttp")
HTTP_CLIENT_NAMES = {"client", "http", "http_client", "session"}
PARSER_FORBIDDEN_IMPORTS = (
    "requests",
    "urllib.request",
    "http.client",
    "httpx",
    "aiohttp",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "app.core.database",
    "app.core.database_json_state",
    "app.core.store",
    "app.services.cloakbrowser_session",
    "app.services.makerworld_browser_client",
)


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


PARSER_FILES = _python_files(ROOT / "app/services/makerworld_parsers")
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


def _imported_modules(path: Path) -> set[str]:
    result = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
            result.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
    return result


def _forbidden_parser_imports(path: Path) -> set[str]:
    return {
        module
        for module in _imported_modules(path)
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in PARSER_FORBIDDEN_IMPORTS)
    }


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
        self._functions: list[str] = []
        self.browser_fetch_references: list[_AstFinding] = []
        self.direct_get_calls: list[_AstFinding] = []
        self._browser_positions: set[tuple[int, int]] = set()
        self._get_positions: set[tuple[int, int]] = set()

    def _bind(self, name: str, symbol: str) -> None:
        self._scopes[-1][name] = symbol

    def _lookup(self, name: str) -> str:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return name

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
        attribute = node.args[1]
        if not isinstance(attribute, ast.Constant) or not isinstance(attribute.value, str):
            return ""
        base = self._qualified_name(node.args[0])
        return f"{base}.{attribute.value}" if base else ""

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
        self._functions.append(node.name)
        self._scopes.append({})
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
        self._scopes.pop()
        self._functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        symbol = self._qualified_name(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, symbol or f"local.{target.id}")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value:
            self.visit(node.value)
        if isinstance(node.target, ast.Name):
            symbol = self._qualified_name(node.value) or self._qualified_name(node.annotation)
            self._bind(node.target.id, symbol or f"local.{node.target.id}")

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
    return node.args[0].id if node.args and isinstance(node.args[0], ast.Name) else ""


def _is_named_session_get(finding: _AstFinding) -> bool:
    return (
        isinstance(finding.node, ast.Call)
        and isinstance(finding.node.func, ast.Attribute)
        and finding.symbol == "requests.Session.get"
    )


def _is_allowed_direct_get(path: Path, finding: _AstFinding) -> bool:
    if not _is_named_session_get(finding):
        return False
    node = finding.node
    if path == Path("app/services/asset_downloader.py"):
        keywords = _keyword_map(node)
        return (
            finding.function == "download_file"
            and _first_argument_name(node) == "url"
            and isinstance(keywords.get("stream"), ast.Constant)
            and keywords["stream"].value is True
        )
    if path != Path("app/services/online_accounts.py") or finding.function != "_exchange_makerworld_ticket":
        return False

    first_argument = _first_argument_name(node)
    keywords = _keyword_map(node)
    if first_argument == "ticket_url":
        return {"proxies", "timeout"}.issubset(keywords)
    if first_argument != "makerworld_ticket_url" or not {
        "params",
        "proxies",
        "timeout",
        "allow_redirects",
    }.issubset(keywords):
        return False
    params = keywords["params"]
    return isinstance(params, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == "ticket"
        for key in params.keys
    )


def _unapproved_direct_gets(path: Path, logical_path: Path | None = None) -> list[_AstFinding]:
    relative_path = logical_path
    if relative_path is None:
        try:
            relative_path = path.relative_to(ROOT)
        except ValueError:
            relative_path = path
    return [
        finding
        for finding in _direct_http_gets(path)
        if not _is_allowed_direct_get(relative_path, finding)
    ]


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

    assert expected in _forbidden_parser_imports(path)


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
        "    session.get(url, stream=True)\n"
        "    session.get(control_url)\n",
        encoding="utf-8",
    )

    findings = _unapproved_direct_gets(path, Path("app/services/asset_downloader.py"))

    assert [finding.lineno for finding in findings] == [4]


def test_ticket_gets_are_the_only_online_account_exceptions(tmp_path):
    path = tmp_path / "online_accounts.py"
    path.write_text(
        "import requests\n"
        "def _exchange_makerworld_ticket(session: requests.Session, ticket_url, makerworld_ticket_url, control_url):\n"
        "    session.get(ticket_url, proxies=None, timeout=(8, 20))\n"
        "    session.get(makerworld_ticket_url, params={'ticket': 'value'}, proxies=None, timeout=(8, 20), allow_redirects=True)\n"
        "    session.get(control_url)\n",
        encoding="utf-8",
    )

    findings = _unapproved_direct_gets(path, Path("app/services/online_accounts.py"))

    assert [finding.lineno for finding in findings] == [5]


def test_parsers_have_no_network_or_store_dependencies():
    for path in PARSER_FILES:
        forbidden = _forbidden_parser_imports(path)
        assert not forbidden, f"{path}: {sorted(forbidden)}"


def test_only_browser_client_calls_browser_fetch():
    service_files = _python_files(ROOT / "app/services")
    callers = {path.relative_to(ROOT) for path in service_files if _browser_fetch_references(path)}
    assert callers == {Path("app/services/makerworld_browser_client.py")}


def test_control_plane_does_not_issue_unapproved_direct_gets():
    for path in CONTROL_GET_FILES:
        findings = _unapproved_direct_gets(path)
        assert not findings, f"{path}: {_finding_summary(findings)}"
