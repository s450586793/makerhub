import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main as main_app
from tests.test_helpers import iter_app_routes


class RemovedEmbeddedVerificationWebRouteTest(unittest.TestCase):
    def test_embedded_verification_direct_url_is_not_spa_route(self):
        route_prefix = "/" + "-".join(["browser", "verification"])
        paths = {route.path for route in iter_app_routes(main_app.app)}
        self.assertNotIn(f"{route_prefix}/{{session_id}}", paths)
        response = TestClient(main_app.app).get(f"{route_prefix}/bv_test", follow_redirects=False)
        self.assertIn(response.status_code, {302, 303, 404})

    def test_core_api_routes_are_registered(self):
        paths = {route.path for route in iter_app_routes(main_app.app) if hasattr(route, "path")}
        self.assertIn("/api/dashboard", paths)
        self.assertIn("/api/dashboard/light", paths)
        self.assertIn("/api/models", paths)
        self.assertIn("/api/models/light", paths)
        self.assertIn("/api/tasks", paths)
        self.assertIn("/api/tasks/light", paths)
        self.assertIn("/api/tasks/missing-3mf/verification-verified", paths)
        self.assertIn("/api/runtime", paths)
        self.assertIn("/api/runtime/runs", paths)
        self.assertIn("/api/runtime/runs/{run_id}", paths)
        self.assertIn("/api/runtime/runs/{run_id}/failures", paths)
        self.assertIn("/api/runtime/failures/retry", paths)
        self.assertIn("/api/remote-refresh", paths)
        self.assertIn("/api/source-refresh", paths)
        self.assertIn("/api/source-library/light", paths)
        self.assertIn("/api/source-refresh/run", paths)
        self.assertIn("/api/source-refresh/repair", paths)
        self.assertIn("/api/subscriptions", paths)
        self.assertIn("/api/subscriptions/light", paths)
        self.assertIn("/api/logs", paths)

    def test_logs_api_forwards_filter_parameters(self):
        captured = {}

        def fake_read_log_entries(**kwargs):
            captured.update(kwargs)
            return {"entries": [], "files": [], "facets": {}, "count": 0}

        with patch("app.api.logs_routes.run_web_io", side_effect=lambda func, **kwargs: func(**kwargs)), \
                patch("app.api.logs_routes.read_log_entries", side_effect=fake_read_log_entries):
            import asyncio
            from app.api.logs_routes import get_logs_data

            response = asyncio.run(
                get_logs_data(
                    file="business.log",
                    limit=80,
                    q="failed",
                    level="error",
                    category="archive",
                    event="download_failed",
                    since="2026-06-05T00:00:00+08:00",
                    cursor="99",
                )
            )

        self.assertEqual(response["count"], 0)
        self.assertEqual(captured["level"], "error")
        self.assertEqual(captured["category"], "archive")
        self.assertEqual(captured["event"], "download_failed")
        self.assertEqual(captured["since"], "2026-06-05T00:00:00+08:00")
        self.assertEqual(captured["cursor"], "99")
        self.assertEqual(captured["query"], "failed")
        self.assertEqual(captured["limit"], 80)

    def test_static_model_api_routes_are_registered_before_detail_route(self):
        routes = [
            route
            for route in iter_app_routes(main_app.app)
            if getattr(route, "path", "").startswith("/api/models/")
        ]
        route_order = [route.path for route in routes]

        detail_index = route_order.index("/api/models/{model_dir:path}")

        for static_path in (
            "/api/models/delete",
            "/api/models/flags",
            "/api/models/flags/favorite",
            "/api/models/flags/printed",
            "/api/models/flags/deleted",
        ):
            with self.subTest(static_path=static_path):
                self.assertLess(route_order.index(static_path), detail_index)


if __name__ == "__main__":
    unittest.main()
