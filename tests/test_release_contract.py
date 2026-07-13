from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT_DIR / ".github" / "workflows" / "docker.yml"
VERSION_SCRIPT = ROOT_DIR / "scripts" / "check_release_version.py"


def _load_workflow() -> dict:
    payload = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    if not isinstance(payload, dict):
        raise AssertionError("docker workflow must be a mapping")
    return payload


def _step(job: dict, name: str) -> dict:
    for item in job.get("steps", []):
        if item.get("name") == name:
            return item
    raise AssertionError(f"workflow step not found: {name}")


class ReleaseWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = _load_workflow()
        cls.jobs = cls.workflow["jobs"]

    def test_pull_requests_main_and_version_tags_run_verification(self):
        triggers = self.workflow["on"]

        self.assertIn("pull_request", triggers)
        self.assertIn("main", triggers["push"]["branches"])
        self.assertIn("v*", triggers["push"]["tags"])
        self.assertIn("verify", self.jobs)
        self.assertNotIn("if", self.jobs["verify"])

    def test_same_ref_workflow_runs_are_serialized_without_cancellation(self):
        concurrency = self.workflow["concurrency"]

        self.assertEqual(concurrency["group"], "docker-${{ github.ref }}")
        self.assertEqual(concurrency["cancel-in-progress"], "false")

    def test_verify_job_runs_all_quality_gates_in_order(self):
        verify = self.jobs["verify"]
        step_names = [item.get("name") for item in verify["steps"]]
        expected_order = [
            "Checkout",
            "Set up Python",
            "Install backend dependencies",
            "Run backend tests",
            "Set up Node.js",
            "Install frontend dependencies",
            "Run frontend tests",
            "Build frontend",
            "Validate Compose files",
            "Check release version",
            "Set up Docker Buildx",
            "Build image",
            "Smoke test image",
        ]

        self.assertEqual(step_names, expected_order)
        self.assertIn("python -m pytest", _step(verify, "Run backend tests")["run"])
        self.assertEqual(_step(verify, "Install frontend dependencies")["run"], "npm ci")
        self.assertEqual(_step(verify, "Run frontend tests")["run"], "npm test")
        self.assertEqual(_step(verify, "Build frontend")["run"], "npm run build")
        compose_run = _step(verify, "Validate Compose files")["run"]
        self.assertIn("compose.yaml", compose_run)
        self.assertIn("compose.external-flaresolverr.yaml", compose_run)
        self.assertIn(
            "docker compose -f compose.yaml -f compose.external-flaresolverr.yaml config --quiet",
            compose_run,
        )
        self.assertIn("scripts/check_release_version.py", _step(verify, "Check release version")["run"])
        self.assertEqual(_step(verify, "Build image")["with"]["push"], "false")

    def test_verify_build_loads_and_smoke_tests_the_runtime_image(self):
        verify = self.jobs["verify"]
        step_names = [item.get("name") for item in verify["steps"]]
        build = _step(verify, "Build image")
        smoke = _step(verify, "Smoke test image")

        self.assertEqual(build["with"]["load"], "true")
        self.assertEqual(step_names.index("Smoke test image"), step_names.index("Build image") + 1)
        self.assertIn("docker run --rm makerhub:verify", smoke["run"])
        self.assertNotIn("--entrypoint", smoke["run"])
        self.assertIn("import app.main", smoke["run"])
        self.assertIn("/app/VERSION", smoke["run"])
        self.assertIn("version('fastapi')", smoke["run"])

    def test_release_only_runs_for_version_tags_after_verification(self):
        release = self.jobs["release"]

        self.assertEqual(release["needs"], "verify")
        self.assertIn("refs/tags/v", release["if"])
        self.assertEqual(_step(release, "Build and push image")["with"]["push"], "true")
        self.assertEqual(release["permissions"]["packages"], "write")

    def test_release_publishes_only_prefixed_version_sha_and_latest_tags(self):
        release = self.jobs["release"]
        metadata_tags = _step(release, "Extract image metadata")["with"]["tags"]

        self.assertIn("type=raw,value=${{ github.ref_name }}", metadata_tags)
        self.assertIn("type=sha", metadata_tags)
        self.assertIn("type=raw,value=latest", metadata_tags)
        self.assertNotIn("pattern={{version}}", metadata_tags)
        self.assertNotRegex(metadata_tags, r"value=\$\{\{\s*steps\.[^.]+\.outputs\.version\s*\}\}")

    def test_release_refuses_existing_or_ambiguous_version_manifest(self):
        release = self.jobs["release"]
        step_names = [item.get("name") for item in release["steps"]]
        guard = _step(release, "Refuse existing version tag")
        guard_run = guard["run"]

        self.assertLess(step_names.index("Log in to GHCR"), step_names.index("Refuse existing version tag"))
        self.assertLess(step_names.index("Refuse existing version tag"), step_names.index("Build and push image"))
        self.assertEqual(guard["env"]["GHCR_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertIn("https://ghcr.io/token", guard_run)
        self.assertIn("Authorization: Bearer ${REGISTRY_TOKEN}", guard_run)
        self.assertIn("/manifests/${GITHUB_REF_NAME}", guard_run)
        self.assertIn("200)", guard_run)
        self.assertIn("404)", guard_run)
        self.assertGreaterEqual(guard_run.count("exit 1"), 2)


class ReadmeCloakBrowserContractTest(unittest.TestCase):
    def test_readme_requires_token_and_documents_safe_manager_binding(self):
        readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
        compose = (ROOT_DIR / "compose.yaml").read_text(encoding="utf-8")
        required_token = (
            "${MAKERHUB_CLOAKBROWSER_AUTH_TOKEN:"
            "?set MAKERHUB_CLOAKBROWSER_AUTH_TOKEN in .env}"
        )
        local_bind = "${MAKERHUB_CLOAKBROWSER_BIND_ADDRESS:-127.0.0.1}:9050:8080"

        self.assertGreaterEqual(compose.count(required_token), 3)
        self.assertNotIn("${MAKERHUB_CLOAKBROWSER_AUTH_TOKEN:-}", compose)
        self.assertIn(local_bind, compose)
        self.assertRegex(readme, r"MAKERHUB_CLOAKBROWSER_AUTH_TOKEN.{0,40}必填")
        self.assertIn("MAKERHUB_CLOAKBROWSER_BIND_ADDRESS=<LAN IP>", readme)
        self.assertIn("127.0.0.1", readme)
        self.assertIn("扩大攻击面", readme)


class DeploymentComposeContractTest(unittest.TestCase):
    def test_external_flaresolverr_file_is_a_minimal_override(self):
        payload = yaml.safe_load(
            (ROOT_DIR / "compose.external-flaresolverr.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(set(payload), {"services"})
        services = payload["services"]
        self.assertEqual(set(services), {"makerhub-app", "makerhub-worker", "flaresolverr"})
        expected_url = "${MAKERHUB_FLARESOLVERR_URL:?set MAKERHUB_FLARESOLVERR_URL in .env}"
        self.assertEqual(
            services["makerhub-app"],
            {"environment": {"MAKERHUB_FLARESOLVERR_URL": expected_url}},
        )
        self.assertEqual(
            services["makerhub-worker"],
            {"environment": {"MAKERHUB_FLARESOLVERR_URL": expected_url}},
        )
        self.assertEqual(services["flaresolverr"], {"profiles": ["bundled-flaresolverr"]})

    def test_canonical_compose_keeps_security_and_readiness_contracts(self):
        compose = yaml.safe_load((ROOT_DIR / "compose.yaml").read_text(encoding="utf-8"))
        services = compose["services"]
        required_token = (
            "${MAKERHUB_CLOAKBROWSER_AUTH_TOKEN:"
            "?set MAKERHUB_CLOAKBROWSER_AUTH_TOKEN in .env}"
        )

        for name in ("makerhub-app", "makerhub-worker", "makerhub-postgres"):
            self.assertIn("healthcheck", services[name])
        for name in ("makerhub-app", "makerhub-worker"):
            self.assertEqual(
                services[name]["depends_on"]["makerhub-postgres"]["condition"],
                "service_healthy",
            )
            self.assertEqual(
                services[name]["environment"]["MAKERHUB_CLOAKBROWSER_AUTH_TOKEN"],
                required_token,
            )
        app_environment = services["makerhub-app"]["environment"]
        self.assertIn("MAKERHUB_TRUSTED_PROXIES", app_environment)
        self.assertEqual(app_environment["MAKERHUB_TRUSTED_PROXIES"], "${MAKERHUB_TRUSTED_PROXIES:-}")
        self.assertEqual(
            services["cloakbrowser"]["ports"],
            ["${MAKERHUB_CLOAKBROWSER_BIND_ADDRESS:-127.0.0.1}:9050:8080"],
        )

    def test_dockerfile_packages_the_canonical_compose_for_update_diagnostics(self):
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY compose.yaml ./compose.yaml", dockerfile)


class ReleaseDocumentationContractTest(unittest.TestCase):
    def test_release_metadata_and_visible_readme_history_match_0_11_1(self):
        version = (ROOT_DIR / "VERSION").read_text(encoding="utf-8").strip()
        package = json.loads((ROOT_DIR / "frontend" / "package.json").read_text(encoding="utf-8"))
        package_lock = json.loads(
            (ROOT_DIR / "frontend" / "package-lock.json").read_text(encoding="utf-8")
        )
        readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
        visible_history = readme.split("<details>", 1)[0]

        self.assertEqual(version, "0.11.1")
        self.assertEqual(package["version"], version)
        self.assertEqual(package_lock["version"], version)
        self.assertEqual(package_lock["packages"][""]["version"], version)
        self.assertIn("> 当前版本：`v0.11.1`", readme)
        self.assertIn("## 2026-07-13 · v0.11.1", changelog)
        self.assertEqual(
            [line.rsplit("v", 1)[-1] for line in visible_history.splitlines() if line.startswith("### 20")],
            ["0.11.1", "0.11.0", "0.10.3"],
        )

    def test_operations_docs_cover_the_release_safety_contract(self):
        documentation = "\n".join(
            (ROOT_DIR / path).read_text(encoding="utf-8")
            for path in (
                "README.md",
                "docs/modules/deployment_update.md",
                "docs/modules/core.md",
                "docs/modules/archive.md",
            )
        )

        for expected in (
            "MAKERHUB_ADMIN_PASSWORD",
            "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN",
            "MAKERHUB_POSTGRES_PASSWORD",
            "MAKERHUB_TRUSTED_PROXIES",
            "哈希",
            "Runtime Engine",
            "冻结",
            "14 天",
            "90 天",
            "整组回滚",
            "首次网页更新",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, documentation)


class FrontendTestContractTest(unittest.TestCase):
    def test_npm_test_runs_all_node_test_modules(self):
        package = json.loads((ROOT_DIR / "frontend" / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(package["scripts"]["test"], "node --test src/lib/*.test.mjs")


class DockerIgnoreContractTest(unittest.TestCase):
    def test_dockerignore_excludes_non_build_content(self):
        path = ROOT_DIR / ".dockerignore"
        self.assertTrue(path.is_file())
        patterns = {
            line.strip().rstrip("/")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        expected = {
            ".env",
            ".env.*",
            ".git",
            ".venv",
            "venv",
            "**/node_modules",
            "frontend/dist",
            ".workflow",
            ".superpowers",
            ".worktrees",
            "worktrees",
            "config",
            "data",
            "logs",
            "state",
            "archive",
            "local",
            "docs",
            "tests",
            "videos/**/output",
        }
        self.assertTrue(expected.issubset(patterns), expected - patterns)

    def test_dockerignore_retains_docker_build_inputs(self):
        patterns = {
            line.strip().rstrip("/")
            for line in (ROOT_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        required_inputs = {
            "Dockerfile",
            "requirements.txt",
            "VERSION",
            "app",
            "docker",
            "frontend",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/src",
        }
        self.assertTrue(required_inputs.isdisjoint(patterns), required_inputs & patterns)


class ReleaseVersionContractTest(unittest.TestCase):
    def _write_version_fixture(self, root: Path, *, version: str, package_version: str | None = None) -> None:
        frontend = root / "frontend"
        frontend.mkdir(parents=True)
        package_version = package_version or version
        (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        (frontend / "package.json").write_text(
            json.dumps({"version": package_version}),
            encoding="utf-8",
        )
        (frontend / "package-lock.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "packages": {"": {"version": version}},
                }
            ),
            encoding="utf-8",
        )

    def _run_checker(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, VERSION_SCRIPT.as_posix(), "--root", root.as_posix(), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_repository_versions_and_release_tag_are_consistent(self):
        version = (ROOT_DIR / "VERSION").read_text(encoding="utf-8").strip()

        result = self._run_checker(ROOT_DIR, "--tag", f"v{version}")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_version_checker_rejects_file_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_version_fixture(root, version="1.2.3", package_version="1.2.4")

            result = self._run_checker(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("frontend/package.json", result.stderr)

    def test_version_checker_rejects_non_matching_release_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_version_fixture(root, version="1.2.3")

            result = self._run_checker(root, "--tag", "v1.2.4")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("v1.2.3", result.stderr)


if __name__ == "__main__":
    unittest.main()
