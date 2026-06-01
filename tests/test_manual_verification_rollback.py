import inspect
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as main_app
from app.services import archive_worker


class ManualVerificationRollbackTest(unittest.TestCase):
    def test_embedded_verification_api_routes_are_removed(self):
        route_prefix = "/" + "/".join(["api", "-".join(["browser", "verification"])])
        sessions_route = f"{route_prefix}/sessions"
        paths = {route.path for route in main_app.app.routes}
        self.assertNotIn(sessions_route, paths)
        self.assertFalse(any(str(path).startswith(route_prefix) for path in paths))

    def test_embedded_verification_spa_route_is_removed(self):
        route_prefix = "/" + "-".join(["browser", "verification"])
        route_path = f"{route_prefix}/{{session_id}}"
        paths = {route.path for route in main_app.app.routes}
        self.assertNotIn(route_path, paths)
        response = TestClient(main_app.app).get(f"{route_prefix}/bv_test", follow_redirects=False)
        self.assertIn(response.status_code, {302, 303, 404})

    def test_archive_worker_no_longer_consumes_embedded_verification_proofs(self):
        proof_consumer = "_".join(["consume", "browser", "verification", "proof"])
        proof_id_field = "_".join(["browser", "verification", "proof", "id"])
        source = inspect.getsource(archive_worker)
        self.assertNotIn(proof_consumer, source)
        self.assertNotIn(proof_id_field, source)

    def test_embedded_verification_service_file_is_deleted(self):
        service_path = Path("app/services") / ("_".join(["browser", "verification"]) + ".py")
        self.assertFalse(service_path.exists())
