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


if __name__ == "__main__":
    unittest.main()
