import tempfile
import unittest
from pathlib import Path

from app.services import self_update


class SelfUpdateSplitDeploymentTest(unittest.TestCase):
    def test_web_update_target_uses_current_image_when_web_image_is_not_set(self):
        original_name = self_update.os.environ.get(self_update.WEB_CONTAINER_NAME_ENV)
        original_image = self_update.os.environ.get(self_update.WEB_IMAGE_REF_ENV)
        try:
            self_update.os.environ[self_update.WEB_CONTAINER_NAME_ENV] = "makerhub-web"
            self_update.os.environ.pop(self_update.WEB_IMAGE_REF_ENV, None)

            target = self_update._web_update_target("ghcr.io/example/makerhub:latest")

            self.assertEqual(target["container_name"], "makerhub-web")
            self.assertEqual(target["image_ref"], "ghcr.io/example/makerhub:latest")
        finally:
            if original_name is None:
                self_update.os.environ.pop(self_update.WEB_CONTAINER_NAME_ENV, None)
            else:
                self_update.os.environ[self_update.WEB_CONTAINER_NAME_ENV] = original_name
            if original_image is None:
                self_update.os.environ.pop(self_update.WEB_IMAGE_REF_ENV, None)
            else:
                self_update.os.environ[self_update.WEB_IMAGE_REF_ENV] = original_image

    def test_worker_update_target_uses_current_image_when_worker_image_is_not_set(self):
        original_name = self_update.os.environ.get(self_update.WORKER_CONTAINER_NAME_ENV)
        original_image = self_update.os.environ.get(self_update.WORKER_IMAGE_REF_ENV)
        try:
            self_update.os.environ[self_update.WORKER_CONTAINER_NAME_ENV] = "makerhub-worker"
            self_update.os.environ.pop(self_update.WORKER_IMAGE_REF_ENV, None)

            target = self_update._worker_update_target("ghcr.io/example/makerhub:latest")

            self.assertEqual(target["container_name"], "makerhub-worker")
            self.assertEqual(target["image_ref"], "ghcr.io/example/makerhub:latest")
        finally:
            if original_name is None:
                self_update.os.environ.pop(self_update.WORKER_CONTAINER_NAME_ENV, None)
            else:
                self_update.os.environ[self_update.WORKER_CONTAINER_NAME_ENV] = original_name
            if original_image is None:
                self_update.os.environ.pop(self_update.WORKER_IMAGE_REF_ENV, None)
            else:
                self_update.os.environ[self_update.WORKER_IMAGE_REF_ENV] = original_image

    def test_replacement_body_applies_app_runtime_resources(self):
        inspect = {
            "Id": "app-container-id",
            "Name": "/makerhub-app",
            "Config": {
                "Image": "ghcr.io/example/makerhub:latest",
                "Env": ["MAKERHUB_ENTRYPOINT=app", "MAKERHUB_WEB_WORKERS=1"],
            },
            "HostConfig": {},
            "NetworkSettings": {"Networks": {}},
        }

        body = self_update._build_replacement_container_body(
            inspect,
            "ghcr.io/example/makerhub:latest",
            runtime_config={
                "web_workers": 3,
                "app_cpu_limit": "2.5",
                "app_cpuset_cpus": "0-1",
                "app_cpu_shares": 2048,
            },
            role="app",
        )

        self.assertIn("MAKERHUB_WEB_WORKERS=3", body["Env"])
        self.assertEqual(body["HostConfig"]["NanoCpus"], 2_500_000_000)
        self.assertEqual(body["HostConfig"]["CpusetCpus"], "0-1")
        self.assertEqual(body["HostConfig"]["CpuShares"], 2048)

    def test_replacement_body_applies_worker_runtime_resources(self):
        inspect = {
            "Id": "worker-container-id",
            "Name": "/makerhub-worker",
            "Config": {
                "Image": "ghcr.io/example/makerhub:latest",
                "Env": ["MAKERHUB_ENTRYPOINT=worker"],
            },
            "HostConfig": {},
            "NetworkSettings": {"Networks": {}},
        }

        body = self_update._build_replacement_container_body(
            inspect,
            "ghcr.io/example/makerhub:latest",
            runtime_config={
                "worker_cpu_limit": "4",
                "worker_cpuset_cpus": "2-5",
                "worker_cpu_shares": 768,
            },
            role="worker",
        )

        self.assertEqual(body["HostConfig"]["NanoCpus"], 4_000_000_000)
        self.assertEqual(body["HostConfig"]["CpusetCpus"], "2-5")
        self.assertEqual(body["HostConfig"]["CpuShares"], 768)

    def test_request_system_update_passes_web_container_to_helper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            socket_path = Path(temp_dir) / "docker.sock"
            socket_path.write_text("", encoding="utf-8")

            original_socket = self_update.DOCKER_SOCKET_PATH
            original_state_dir = self_update.STATE_DIR
            original_update_state = self_update.UPDATE_STATE_PATH
            original_client = self_update.DockerSocketClient
            original_candidates = self_update._extract_container_id_candidates
            original_env = {
                self_update.DEPLOYMENT_MODE_ENV: self_update.os.environ.get(self_update.DEPLOYMENT_MODE_ENV),
                self_update.WEB_CONTAINER_NAME_ENV: self_update.os.environ.get(self_update.WEB_CONTAINER_NAME_ENV),
                self_update.WEB_IMAGE_REF_ENV: self_update.os.environ.get(self_update.WEB_IMAGE_REF_ENV),
                self_update.WORKER_CONTAINER_NAME_ENV: self_update.os.environ.get(self_update.WORKER_CONTAINER_NAME_ENV),
                self_update.WORKER_IMAGE_REF_ENV: self_update.os.environ.get(self_update.WORKER_IMAGE_REF_ENV),
            }
            created: list[dict] = []
            started: list[str] = []
            written_state: dict = {}

            def api_inspect() -> dict:
                return {
                    "Id": "api-container-id",
                    "Name": "/makerhub-api",
                    "Image": "sha256:current-image",
                    "Config": {"Image": "ghcr.io/example/makerhub:latest"},
                    "HostConfig": {
                        "Binds": [
                            f"{state_dir}:{state_dir}",
                            f"{socket_path}:{socket_path}",
                        ],
                    },
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"makerhub_default": {"Aliases": ["makerhub-api"]}}},
                }

            def web_inspect() -> dict:
                return {
                    "Id": "web-container-id",
                    "Name": "/makerhub-web",
                    "Image": "sha256:current-image",
                    "Config": {"Image": "ghcr.io/example/makerhub:latest"},
                    "HostConfig": {},
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"makerhub_default": {"Aliases": ["makerhub-web"]}}},
                }

            class FakeDockerSocketClient:
                def __init__(self, *args, **kwargs):
                    pass

                def inspect_container(self, container_id):
                    if container_id in {"api-container-id", "makerhub-api"}:
                        return api_inspect()
                    if container_id == "makerhub-web":
                        return web_inspect()
                    raise RuntimeError(f"missing container {container_id}")

                def create_container(self, body, *, name=""):
                    created.append({"body": body, "name": name})
                    return f"{name}-id"

                def start_container(self, container_id):
                    started.append(container_id)

            try:
                self_update.DOCKER_SOCKET_PATH = socket_path
                self_update.STATE_DIR = state_dir
                self_update.UPDATE_STATE_PATH = state_dir / "system_update.json"
                self_update.DockerSocketClient = FakeDockerSocketClient
                self_update._extract_container_id_candidates = lambda: ["api-container-id"]
                self_update.os.environ[self_update.DEPLOYMENT_MODE_ENV] = "split"
                self_update.os.environ[self_update.WEB_CONTAINER_NAME_ENV] = "makerhub-web"
                self_update.os.environ[self_update.WEB_IMAGE_REF_ENV] = "ghcr.io/example/makerhub:latest"
                self_update.os.environ.pop(self_update.WORKER_CONTAINER_NAME_ENV, None)
                self_update.os.environ.pop(self_update.WORKER_IMAGE_REF_ENV, None)

                status = self_update.request_system_update(requested_by="admin", target_version="0.6.0")
                written_state = self_update._read_update_state()
            finally:
                self_update.DOCKER_SOCKET_PATH = original_socket
                self_update.STATE_DIR = original_state_dir
                self_update.UPDATE_STATE_PATH = original_update_state
                self_update.DockerSocketClient = original_client
                self_update._extract_container_id_candidates = original_candidates
                for key, value in original_env.items():
                    if value is None:
                        self_update.os.environ.pop(key, None)
                    else:
                        self_update.os.environ[key] = value

            self.assertEqual(status["deployment_mode"], "split")
            self.assertEqual(status["web_container_name"], "makerhub-web")
            self.assertEqual(len(created), 1)
            command = created[0]["body"]["Cmd"]
            self.assertIn("--web-container", command)
            self.assertIn("makerhub-web", command)
            self.assertIn("--web-image-ref", command)
            self.assertIn("ghcr.io/example/makerhub:latest", command)
            self.assertEqual(started, [created[0]["name"] + "-id"])
            self.assertEqual(written_state["old_image_ids"], ["sha256:current-image"])

    def test_request_system_update_passes_worker_container_to_helper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            socket_path = Path(temp_dir) / "docker.sock"
            socket_path.write_text("", encoding="utf-8")

            original_socket = self_update.DOCKER_SOCKET_PATH
            original_state_dir = self_update.STATE_DIR
            original_update_state = self_update.UPDATE_STATE_PATH
            original_client = self_update.DockerSocketClient
            original_candidates = self_update._extract_container_id_candidates
            original_env = {
                self_update.DEPLOYMENT_MODE_ENV: self_update.os.environ.get(self_update.DEPLOYMENT_MODE_ENV),
                self_update.WEB_CONTAINER_NAME_ENV: self_update.os.environ.get(self_update.WEB_CONTAINER_NAME_ENV),
                self_update.WEB_IMAGE_REF_ENV: self_update.os.environ.get(self_update.WEB_IMAGE_REF_ENV),
                self_update.WORKER_CONTAINER_NAME_ENV: self_update.os.environ.get(self_update.WORKER_CONTAINER_NAME_ENV),
                self_update.WORKER_IMAGE_REF_ENV: self_update.os.environ.get(self_update.WORKER_IMAGE_REF_ENV),
            }
            created: list[dict] = []
            started: list[str] = []
            written_state: dict = {}

            def app_inspect() -> dict:
                return {
                    "Id": "app-container-id",
                    "Name": "/makerhub-app",
                    "Image": "sha256:current-image",
                    "Config": {"Image": "ghcr.io/example/makerhub:latest"},
                    "HostConfig": {
                        "Binds": [
                            f"{state_dir}:{state_dir}",
                            f"{socket_path}:{socket_path}",
                        ],
                    },
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"makerhub_default": {"Aliases": ["makerhub-app"]}}},
                }

            def worker_inspect() -> dict:
                return {
                    "Id": "worker-container-id",
                    "Name": "/makerhub-worker",
                    "Image": "sha256:current-image",
                    "Config": {"Image": "ghcr.io/example/makerhub:latest"},
                    "HostConfig": {},
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"makerhub_default": {"Aliases": ["makerhub-worker"]}}},
                }

            class FakeDockerSocketClient:
                def __init__(self, *args, **kwargs):
                    pass

                def inspect_container(self, container_id):
                    if container_id in {"app-container-id", "makerhub-app"}:
                        return app_inspect()
                    if container_id == "makerhub-worker":
                        return worker_inspect()
                    raise RuntimeError(f"missing container {container_id}")

                def create_container(self, body, *, name=""):
                    created.append({"body": body, "name": name})
                    return f"{name}-id"

                def start_container(self, container_id):
                    started.append(container_id)

            try:
                self_update.DOCKER_SOCKET_PATH = socket_path
                self_update.STATE_DIR = state_dir
                self_update.UPDATE_STATE_PATH = state_dir / "system_update.json"
                self_update.DockerSocketClient = FakeDockerSocketClient
                self_update._extract_container_id_candidates = lambda: ["app-container-id"]
                self_update.os.environ[self_update.DEPLOYMENT_MODE_ENV] = "app-worker"
                self_update.os.environ.pop(self_update.WEB_CONTAINER_NAME_ENV, None)
                self_update.os.environ.pop(self_update.WEB_IMAGE_REF_ENV, None)
                self_update.os.environ[self_update.WORKER_CONTAINER_NAME_ENV] = "makerhub-worker"
                self_update.os.environ[self_update.WORKER_IMAGE_REF_ENV] = "ghcr.io/example/makerhub:latest"

                status = self_update.request_system_update(requested_by="admin", target_version="0.6.2")
                written_state = self_update._read_update_state()
            finally:
                self_update.DOCKER_SOCKET_PATH = original_socket
                self_update.STATE_DIR = original_state_dir
                self_update.UPDATE_STATE_PATH = original_update_state
                self_update.DockerSocketClient = original_client
                self_update._extract_container_id_candidates = original_candidates
                for key, value in original_env.items():
                    if value is None:
                        self_update.os.environ.pop(key, None)
                    else:
                        self_update.os.environ[key] = value

            self.assertEqual(status["deployment_mode"], "app-worker")
            self.assertEqual(status["worker_container_name"], "makerhub-worker")
            self.assertEqual(len(created), 1)
            command = created[0]["body"]["Cmd"]
            self.assertIn("--worker-container", command)
            self.assertIn("makerhub-worker", command)
            self.assertIn("--worker-image-ref", command)
            self.assertIn("ghcr.io/example/makerhub:latest", command)
            self.assertEqual(started, [created[0]["name"] + "-id"])
            self.assertEqual(written_state["old_image_ids"], ["sha256:current-image"])

    def test_delayed_cleanup_removes_old_image_after_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            socket_path = Path(temp_dir) / "docker.sock"
            socket_path.write_text("", encoding="utf-8")

            original_socket = self_update.DOCKER_SOCKET_PATH
            original_state_dir = self_update.STATE_DIR
            original_update_state = self_update.UPDATE_STATE_PATH
            original_client = self_update.DockerSocketClient
            original_candidates = self_update._extract_container_id_candidates
            removed: list[str] = []

            class FakeDockerSocketClient:
                def __init__(self, *args, **kwargs):
                    pass

                def inspect_container(self, container_id):
                    return {
                        "Id": "new-container-id",
                        "Name": "/makerhub-app",
                        "Image": "sha256:new-image",
                        "Config": {"Image": "ghcr.io/example/makerhub:latest"},
                        "HostConfig": {"Binds": [f"{state_dir}:{state_dir}"]},
                        "Mounts": [],
                    }

                def remove_image(self, image_id, *, force=False, noprune=False):
                    removed.append(image_id)

            try:
                self_update.DOCKER_SOCKET_PATH = socket_path
                self_update.STATE_DIR = state_dir
                self_update.UPDATE_STATE_PATH = state_dir / "system_update.json"
                self_update.DockerSocketClient = FakeDockerSocketClient
                self_update._extract_container_id_candidates = lambda: ["new-container-id"]
                self_update._write_update_state(
                    {
                        "status": "succeeded",
                        "request_id": "cleanup-request",
                        "old_image_ids": ["sha256:old-image", "sha256:new-image"],
                        "image_cleanup_done": False,
                    }
                )

                result = self_update._cleanup_old_update_images(self_update._read_update_state())
            finally:
                self_update.DOCKER_SOCKET_PATH = original_socket
                self_update.STATE_DIR = original_state_dir
                self_update.UPDATE_STATE_PATH = original_update_state
                self_update.DockerSocketClient = original_client
                self_update._extract_container_id_candidates = original_candidates

            self.assertEqual(removed, ["sha256:old-image"])
            self.assertTrue(result["image_cleanup_done"])
            self.assertEqual(result["image_cleanup_removed"], ["sha256:old-image"])
            self.assertEqual(result["image_cleanup_errors"], [])


if __name__ == "__main__":
    unittest.main()
