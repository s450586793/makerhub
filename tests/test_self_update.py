import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import self_update


ROOT_DIR = Path(__file__).resolve().parents[1]


class SelfUpdateSplitDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.db_patches = [
            patch.object(
                self_update,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.state.get(key) or default),
            ),
            patch.object(
                self_update,
                "save_database_json_state",
                side_effect=lambda key, value: self.state.__setitem__(key, value) or value,
            ),
        ]
        for item in self.db_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.db_patches):
            item.stop()

    def test_repository_compose_defaults_match_simple_deployment_policy(self):
        compose_text = (ROOT_DIR / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("makerhub_password_123456", compose_text)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", compose_text)
        self.assertNotIn("MAKERHUB_POSTGRES_PASSWORD", compose_text)
        self.assertNotIn("    depends_on:", compose_text)
        self.assertNotIn("    healthcheck:", compose_text)
        self.assertIn("/app/config", compose_text)
        self.assertIn("/app/data", compose_text)
        self.assertNotIn("/app/logs", compose_text)
        self.assertNotIn("/app/state", compose_text)
        self.assertNotIn("/app/archive", compose_text)
        self.assertNotIn("/app/local", compose_text)

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

    def test_update_capability_requires_postgres_compose_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            socket_path = Path(temp_dir) / "docker.sock"
            socket_path.write_text("", encoding="utf-8")

            original_socket = self_update.DOCKER_SOCKET_PATH
            original_state_dir = self_update.STATE_DIR
            original_update_state = self_update.UPDATE_STATE_PATH
            original_client = self_update.DockerSocketClient
            original_candidates = self_update._extract_container_id_candidates

            def app_inspect() -> dict:
                return {
                    "Id": "app-container-id",
                    "Name": "/makerhub-app",
                    "Image": "sha256:current-image",
                    "Config": {
                        "Image": "ghcr.io/example/makerhub:latest",
                        "Env": ["MAKERHUB_ENTRYPOINT=app"],
                    },
                    "HostConfig": {
                        "Binds": [
                            f"{state_dir}:{state_dir}",
                            f"{socket_path}:{socket_path}",
                        ],
                    },
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"makerhub_default": {"Aliases": ["makerhub-app"]}}},
                }

            class FakeDockerSocketClient:
                def __init__(self, *args, **kwargs):
                    pass

                def inspect_container(self, container_id):
                    if container_id == "app-container-id":
                        return app_inspect()
                    raise RuntimeError(f"missing container {container_id}")

            try:
                self_update.DOCKER_SOCKET_PATH = socket_path
                self_update.STATE_DIR = state_dir
                self_update.UPDATE_STATE_PATH = state_dir / "system_update.json"
                self_update.DockerSocketClient = FakeDockerSocketClient
                self_update._extract_container_id_candidates = lambda: ["app-container-id"]

                status = self_update.get_update_status()
            finally:
                self_update.DOCKER_SOCKET_PATH = original_socket
                self_update.STATE_DIR = original_state_dir
                self_update.UPDATE_STATE_PATH = original_update_state
                self_update.DockerSocketClient = original_client
                self_update._extract_container_id_candidates = original_candidates

            self.assertFalse(status["supported"])
            self.assertTrue(status["compose_migration_required"])
            self.assertIn("MAKERHUB_DATABASE_URL", status["compose_example"])
            self.assertIn("makerhub-postgres", status["compose_example"])
            self.assertIn("makerhub_password_123456", status["compose_example"])
            self.assertIn("/var/run/docker.sock:/var/run/docker.sock", status["compose_example"])
            self.assertNotIn("MAKERHUB_POSTGRES_PASSWORD", status["compose_example"])
            self.assertNotIn("    depends_on:", status["compose_example"])
            self.assertNotIn("    healthcheck:", status["compose_example"])
            self.assertIn("/app/config", status["compose_example"])
            self.assertIn("/app/data", status["compose_example"])
            self.assertNotIn("/app/logs", status["compose_example"])
            self.assertNotIn("/app/state", status["compose_example"])
            self.assertNotIn("/app/archive", status["compose_example"])
            self.assertNotIn("/app/local", status["compose_example"])

    def test_request_system_update_blocks_without_database_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            socket_path = Path(temp_dir) / "docker.sock"
            socket_path.write_text("", encoding="utf-8")

            original_socket = self_update.DOCKER_SOCKET_PATH
            original_state_dir = self_update.STATE_DIR
            original_update_state = self_update.UPDATE_STATE_PATH
            original_client = self_update.DockerSocketClient
            original_candidates = self_update._extract_container_id_candidates

            class FakeDockerSocketClient:
                def __init__(self, *args, **kwargs):
                    pass

                def inspect_container(self, container_id):
                    if container_id != "app-container-id":
                        raise RuntimeError(f"missing container {container_id}")
                    return {
                        "Id": "app-container-id",
                        "Name": "/makerhub-app",
                        "Image": "sha256:current-image",
                        "Config": {
                            "Image": "ghcr.io/example/makerhub:latest",
                            "Env": ["MAKERHUB_ENTRYPOINT=app"],
                        },
                        "HostConfig": {
                            "Binds": [
                                f"{state_dir}:{state_dir}",
                                f"{socket_path}:{socket_path}",
                            ],
                        },
                        "Mounts": [],
                        "NetworkSettings": {"Networks": {"makerhub_default": {"Aliases": ["makerhub-app"]}}},
                    }

            try:
                self_update.DOCKER_SOCKET_PATH = socket_path
                self_update.STATE_DIR = state_dir
                self_update.UPDATE_STATE_PATH = state_dir / "system_update.json"
                self_update.DockerSocketClient = FakeDockerSocketClient
                self_update._extract_container_id_candidates = lambda: ["app-container-id"]

                with self.assertRaises(RuntimeError) as context:
                    self_update.request_system_update(requested_by="admin", target_version="0.6.128")
            finally:
                self_update.DOCKER_SOCKET_PATH = original_socket
                self_update.STATE_DIR = original_state_dir
                self_update.UPDATE_STATE_PATH = original_update_state
                self_update.DockerSocketClient = original_client
                self_update._extract_container_id_candidates = original_candidates

            self.assertIn("MAKERHUB_DATABASE_URL", str(context.exception))
            self.assertIn("makerhub-postgres", str(context.exception))

    def test_parent_config_and_data_mounts_satisfy_compose_layout(self):
        inspect = {
            "Config": {
                "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"],
            },
            "HostConfig": {
                "Binds": [
                    "/host/makerhub/config:/app/config",
                    "/host/makerhub/data:/app/data",
                ],
            },
            "Mounts": [],
        }
        original_state_dir = self_update.STATE_DIR
        original_logs_dir = self_update.LOGS_DIR
        original_archive_dir = self_update.ARCHIVE_DIR
        original_local_dir = self_update.LOCAL_DIR
        try:
            self_update.STATE_DIR = Path("/app/config/state")
            self_update.LOGS_DIR = Path("/app/config/logs")
            self_update.ARCHIVE_DIR = Path("/app/data")
            self_update.LOCAL_DIR = Path("/app/data/local")

            state_mount = self_update._state_mount_spec_from_inspect(inspect)
            logs_mount = self_update._mount_spec_from_inspect(inspect, self_update.LOGS_DIR)
            migration_required = self_update._compose_migration_required(inspect)
        finally:
            self_update.STATE_DIR = original_state_dir
            self_update.LOGS_DIR = original_logs_dir
            self_update.ARCHIVE_DIR = original_archive_dir
            self_update.LOCAL_DIR = original_local_dir

        self.assertEqual(state_mount, "/host/makerhub/config/state:/app/config/state")
        self.assertEqual(logs_mount, "/host/makerhub/config/logs:/app/config/logs")
        self.assertFalse(migration_required)

    def test_old_archive_and_local_mounts_require_layout_migration(self):
        inspect = {
            "Config": {
                "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"],
            },
            "HostConfig": {
                "Binds": [
                    "/host/makerhub/state:/app/config/state",
                    "/host/makerhub/archive:/app/archive",
                    "/host/makerhub/local:/app/local",
                ],
            },
            "Mounts": [],
        }
        original_state_dir = self_update.STATE_DIR
        original_archive_dir = self_update.ARCHIVE_DIR
        original_local_dir = self_update.LOCAL_DIR
        try:
            self_update.STATE_DIR = Path("/app/config/state")
            self_update.ARCHIVE_DIR = Path("/app/data")
            self_update.LOCAL_DIR = Path("/app/data/local")

            migration_required = self_update._compose_migration_required(inspect)
        finally:
            self_update.STATE_DIR = original_state_dir
            self_update.ARCHIVE_DIR = original_archive_dir
            self_update.LOCAL_DIR = original_local_dir

        self.assertTrue(migration_required)

    def test_replacement_body_applies_app_web_workers_only(self):
        inspect = {
            "Id": "app-container-id",
            "Name": "/makerhub-app",
            "Config": {
                "Image": "ghcr.io/example/makerhub:latest",
                "Env": ["MAKERHUB_ENTRYPOINT=app", "MAKERHUB_WEB_WORKERS=1"],
            },
            "HostConfig": {"NanoCpus": 2_000_000_000, "CpusetCpus": "0-1", "CpuShares": 2048},
            "NetworkSettings": {"Networks": {}},
        }

        body = self_update._build_replacement_container_body(
            inspect,
            "ghcr.io/example/makerhub:latest",
            runtime_config={
                "web_workers": 3,
                "worker_concurrency": 4,
            },
            role="app",
        )

        self.assertIn("MAKERHUB_WEB_WORKERS=3", body["Env"])
        self.assertNotIn("HostConfig", body)

    def test_replacement_body_applies_worker_concurrency_env_only(self):
        inspect = {
            "Id": "worker-container-id",
            "Name": "/makerhub-worker",
            "Config": {
                "Image": "ghcr.io/example/makerhub:latest",
                "Env": ["MAKERHUB_ENTRYPOINT=worker", "MAKERHUB_WORKER_CONCURRENCY=1"],
            },
            "HostConfig": {"NanoCpus": 4_000_000_000, "CpusetCpus": "2-5", "CpuShares": 768},
            "NetworkSettings": {"Networks": {}},
        }

        body = self_update._build_replacement_container_body(
            inspect,
            "ghcr.io/example/makerhub:latest",
            runtime_config={
                "web_workers": 3,
                "worker_concurrency": 4,
            },
            role="worker",
        )

        self.assertIn("MAKERHUB_WORKER_CONCURRENCY=4", body["Env"])
        self.assertNotIn("HostConfig", body)

    def test_startup_marks_version_mismatch_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"

            original_state_dir = self_update.STATE_DIR
            original_update_state = self_update.UPDATE_STATE_PATH
            original_app_version = self_update.APP_VERSION
            try:
                self_update.STATE_DIR = state_dir
                self_update.UPDATE_STATE_PATH = state_dir / "system_update.json"
                self_update.APP_VERSION = "0.6.97"
                self_update._write_update_state(
                    {
                        "status": "pending_startup",
                        "phase": "starting",
                        "target_version": "0.6.98",
                        "request_id": "version-mismatch-request",
                        "replacement_container_id": "new-container",
                        "container_name": "makerhub-app",
                    }
                )

                self_update.mark_update_started_after_restart()
                state = self_update._read_update_state()
            finally:
                self_update.STATE_DIR = original_state_dir
                self_update.UPDATE_STATE_PATH = original_update_state
                self_update.APP_VERSION = original_app_version

            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["phase"], "version_mismatch")
            self.assertIn("当前版本仍为 v0.6.97", state["message"])
            self.assertIn("目标版本 v0.6.98", state["message"])

    def test_request_system_update_passes_web_container_to_helper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            data_dir = Path(temp_dir) / "data"
            socket_path = Path(temp_dir) / "docker.sock"
            socket_path.write_text("", encoding="utf-8")

            original_socket = self_update.DOCKER_SOCKET_PATH
            original_state_dir = self_update.STATE_DIR
            original_logs_dir = self_update.LOGS_DIR
            original_archive_dir = self_update.ARCHIVE_DIR
            original_local_dir = self_update.LOCAL_DIR
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
                    "Config": {"Image": "ghcr.io/example/makerhub:latest", "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"]},
                    "HostConfig": {
                        "Binds": [
                            f"{state_dir}:{state_dir}",
                            f"{data_dir}:{data_dir}",
                            f"{socket_path}:{socket_path}",
                        ],
                        "NetworkMode": "makerhub_default",
                    },
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"makerhub_default": {"Aliases": ["makerhub-api"]}}},
                }

            def web_inspect() -> dict:
                return {
                    "Id": "web-container-id",
                    "Name": "/makerhub-web",
                    "Image": "sha256:current-image",
                    "Config": {"Image": "ghcr.io/example/makerhub:latest", "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"]},
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
                self_update.ARCHIVE_DIR = data_dir / "archive"
                self_update.LOCAL_DIR = data_dir / "local"
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
                self_update.ARCHIVE_DIR = original_archive_dir
                self_update.LOCAL_DIR = original_local_dir
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
            self.assertIn(
                "MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub",
                created[0]["body"]["Env"],
            )
            self.assertEqual(created[0]["body"]["HostConfig"]["NetworkMode"], "makerhub_default")
            self.assertEqual(started, [created[0]["name"] + "-id"])
            self.assertEqual(written_state["old_image_ids"], ["sha256:current-image"])

    def test_request_system_update_passes_worker_container_to_helper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            data_dir = Path(temp_dir) / "data"
            socket_path = Path(temp_dir) / "docker.sock"
            socket_path.write_text("", encoding="utf-8")

            original_socket = self_update.DOCKER_SOCKET_PATH
            original_state_dir = self_update.STATE_DIR
            original_logs_dir = self_update.LOGS_DIR
            original_archive_dir = self_update.ARCHIVE_DIR
            original_local_dir = self_update.LOCAL_DIR
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
                    "Config": {"Image": "ghcr.io/example/makerhub:latest", "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"]},
                    "HostConfig": {
                        "Binds": [
                            f"{state_dir}:{state_dir}",
                            f"{data_dir}:{data_dir}",
                            f"{socket_path}:{socket_path}",
                            f"{state_dir.parent / 'logs'}:{state_dir.parent / 'logs'}",
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
                    "Config": {"Image": "ghcr.io/example/makerhub:latest", "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"]},
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
                self_update.LOGS_DIR = state_dir.parent / "logs"
                self_update.ARCHIVE_DIR = data_dir / "archive"
                self_update.LOCAL_DIR = data_dir / "local"
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
                self_update.LOGS_DIR = original_logs_dir
                self_update.ARCHIVE_DIR = original_archive_dir
                self_update.LOCAL_DIR = original_local_dir
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
            self.assertIn(f"MAKERHUB_LOGS_DIR={state_dir.parent / 'logs'}", created[0]["body"]["Env"])
            self.assertIn(f"{state_dir.parent / 'logs'}:{state_dir.parent / 'logs'}", created[0]["body"]["HostConfig"]["Binds"])
            self.assertEqual(started, [created[0]["name"] + "-id"])
            self.assertEqual(written_state["old_image_ids"], ["sha256:current-image"])

    def test_related_container_skips_pull_when_image_was_already_pulled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            pulled: list[str] = []
            phases: list[str] = []

            original_state_dir = self_update.STATE_DIR
            original_update_state = self_update.UPDATE_STATE_PATH
            original_wait = self_update._wait_for_replacement_container
            try:
                self_update.STATE_DIR = state_dir
                self_update.UPDATE_STATE_PATH = state_dir / "system_update.json"
                self_update._write_update_state({"request_id": "skip-pull"})
                self_update._wait_for_replacement_container = lambda *_args, **_kwargs: {}

                class FakeDockerSocketClient:
                    def inspect_container(self, container_id):
                        return {
                            "Id": container_id,
                            "Name": "/makerhub-worker",
                            "Image": "sha256:old-image",
                            "Config": {"Image": "ghcr.io/example/makerhub:latest", "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"]},
                            "HostConfig": {},
                            "NetworkSettings": {"Networks": {}},
                        }

                    def pull_image(self, image_ref):
                        pulled.append(image_ref)

                    def create_container(self, _body, *, name=""):
                        return f"{name}-id"

                    def stop_container(self, _container_id, *, timeout_seconds=10):
                        pass

                    def rename_container(self, _container_id, *, name):
                        pass

                    def start_container(self, _container_id):
                        pass

                    def remove_container(self, _container_id, *, force=False):
                        pass

                original_update_state_from_helper = self_update._update_state_from_helper

                def tracking_update_state(request_id, **fields):
                    if fields.get("phase"):
                        phases.append(fields["phase"])
                    return original_update_state_from_helper(request_id, **fields)

                self_update._update_state_from_helper = tracking_update_state
                result = self_update._replace_related_container(
                    FakeDockerSocketClient(),
                    request_id="skip-pull",
                    container_ref="worker-container-id",
                    image_ref="ghcr.io/example/makerhub:latest",
                    role="worker",
                    image_already_pulled=True,
                )
            finally:
                self_update.STATE_DIR = original_state_dir
                self_update.UPDATE_STATE_PATH = original_update_state
                self_update._wait_for_replacement_container = original_wait
                self_update._update_state_from_helper = original_update_state_from_helper

            self.assertEqual(result["container_name"], "makerhub-worker")
            self.assertEqual(pulled, [])
            self.assertIn("creating_worker", phases)
            self.assertIn("starting_worker", phases)

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
                        "Config": {"Image": "ghcr.io/example/makerhub:latest", "Env": ["MAKERHUB_DATABASE_URL=postgresql://makerhub:makerhub@makerhub-postgres:5432/makerhub"]},
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
