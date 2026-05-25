from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiTokenRoutePolicy:
    methods: frozenset[str]
    path_prefixes: tuple[str, ...] = ()
    exact_paths: frozenset[str] = frozenset()
    suffix: str = ""
    permission: str = ""

    def matches(self, method: str, path: str) -> bool:
        clean_method = str(method or "").upper()
        clean_path = str(path or "")
        if self.methods and clean_method not in self.methods:
            return False
        if self.exact_paths and clean_path in self.exact_paths:
            return True
        if self.path_prefixes and any(clean_path.startswith(prefix) for prefix in self.path_prefixes):
            if self.suffix and not clean_path.endswith(self.suffix):
                return False
            return True
        return False


API_TOKEN_ROUTE_POLICIES: tuple[ApiTokenRoutePolicy, ...] = (
    ApiTokenRoutePolicy(
        methods=frozenset({"GET", "POST"}),
        path_prefixes=("/api/mobile-import",),
        permission="mobile_import",
    ),
    ApiTokenRoutePolicy(
        methods=frozenset({"POST"}),
        exact_paths=frozenset({"/api/archive", "/api/archive/preview"}),
        permission="archive_write",
    ),
    ApiTokenRoutePolicy(
        methods=frozenset({"GET"}),
        path_prefixes=("/archive/",),
        permission="models_read",
    ),
    ApiTokenRoutePolicy(
        methods=frozenset({"GET"}),
        exact_paths=frozenset(
            {
                "/api/dashboard",
                "/api/models",
                "/api/models/flags",
                "/api/source-library",
                "/api/subscriptions",
                "/api/tasks",
                "/api/remote-refresh",
            }
        ),
        permission="models_read",
    ),
    ApiTokenRoutePolicy(
        methods=frozenset({"GET"}),
        path_prefixes=("/api/models/", "/api/source-library/", "/api/events/"),
        permission="models_read",
    ),
)


SESSION_ONLY_API_PREFIXES: tuple[str, ...] = (
    "/api/admin/",
    "/api/config",
    "/api/local-library",
    "/api/remote-refresh",
    "/api/sharing",
    "/api/subscriptions",
    "/api/system",
    "/api/tasks",
)


PUBLIC_API_PREFIXES: tuple[str, ...] = (
    "/api/public/",
)


PUBLIC_API_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/login",
        "/api/bootstrap",
        "/api/mobile-import",
    }
)


def api_token_permission_for_request(method: str, path: str) -> str:
    for policy in API_TOKEN_ROUTE_POLICIES:
        if policy.matches(method, path):
            return policy.permission
    return ""


def is_public_api_route(path: str) -> bool:
    clean_path = str(path or "")
    return clean_path in PUBLIC_API_EXACT_PATHS or any(clean_path.startswith(prefix) for prefix in PUBLIC_API_PREFIXES)


def is_session_only_api_route(method: str, path: str) -> bool:
    clean_method = str(method or "").upper()
    clean_path = str(path or "")
    if not clean_path.startswith("/api/"):
        return False
    if clean_method == "GET" and api_token_permission_for_request(clean_method, clean_path):
        return False
    if is_public_api_route(clean_path):
        return False
    if api_token_permission_for_request(clean_method, clean_path):
        return False
    return True
