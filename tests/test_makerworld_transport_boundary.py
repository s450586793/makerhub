import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARSER_FILES = tuple((ROOT / "app/services/makerworld_parsers").glob("*.py"))
PIPELINE_FILES = tuple((ROOT / "app/services/makerworld_pipeline").glob("*.py"))
CONTROL_GET_FILES = (
    ROOT / "app/services/process_jobs.py",
    ROOT / "app/services/source_health.py",
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
    return result


def _calls_name(path: Path, name: str) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(_tree(path))
    )


def _calls_get(path: Path) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"requests", "session"}
        for node in ast.walk(_tree(path))
    )


def test_parsers_have_no_network_or_store_dependencies():
    forbidden = {"requests", "app.core.store", "app.services.cloakbrowser_session"}
    for path in PARSER_FILES:
        imported = _imported_modules(path)
        assert imported.isdisjoint(forbidden), f"{path}: {sorted(imported & forbidden)}"


def test_only_browser_client_calls_browser_fetch():
    service_files = tuple((ROOT / "app/services").glob("*.py"))
    callers = {path.relative_to(ROOT) for path in service_files if _calls_name(path, "browser_fetch")}
    assert callers == {Path("app/services/makerworld_browser_client.py")}


def test_pipeline_does_not_issue_requests_get():
    for path in CONTROL_GET_FILES:
        assert not _calls_get(path), path
