import unittest

from fastapi.testclient import TestClient

from app import main as main_app


class RemovedEmbeddedVerificationWebRouteTest(unittest.TestCase):
    def test_embedded_verification_direct_url_is_not_spa_route(self):
        route_prefix = "/" + "-".join(["browser", "verification"])
        paths = {route.path for route in main_app.app.routes}
        self.assertNotIn(f"{route_prefix}/{{session_id}}", paths)
        response = TestClient(main_app.app).get(f"{route_prefix}/bv_test", follow_redirects=False)
        self.assertIn(response.status_code, {302, 303, 404})

    def test_core_api_routes_are_registered(self):
        paths = {route.path for route in main_app.app.routes if hasattr(route, "path")}
        self.assertIn("/api/dashboard", paths)
        self.assertIn("/api/models", paths)
        self.assertIn("/api/tasks", paths)
        self.assertIn("/api/remote-refresh", paths)
        self.assertIn("/api/subscriptions", paths)
        self.assertIn("/api/logs", paths)

    def test_static_model_api_routes_are_registered_before_detail_route(self):
        routes = [
            route
            for route in main_app.app.routes
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
