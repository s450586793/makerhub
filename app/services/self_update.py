from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.database_json_state import (
    load_database_json_state,
    load_database_json_state_without_initialization,
    save_database_json_state,
)
from app.core.settings import APP_VERSION, ARCHIVE_DIR, LOCAL_DIR, LOGS_DIR, ROOT_DIR, STATE_DIR
from app.core.timezone import now_iso as china_now_iso
from app.services.business_logs import append_business_log
from app.services.state_events import publish_state_event


DOCKER_SOCKET_PATH = Path("/var/run/docker.sock")
UPDATE_STATE_PATH = STATE_DIR / "system_update.json"
UPDATE_STATE_KEY = "system_update"
ACTIVE_UPDATE_STATUSES = {"queued", "launching_helper", "running", "pending_startup"}
HELPER_CONTAINER_PREFIX = "makerhub-self-update"
HELPER_LABEL_KEY = "com.makerhub.self_update.role"
HELPER_LABEL_VALUE = "helper"
DEPLOYMENT_MODE_ENV = "MAKERHUB_DEPLOYMENT_MODE"
WEB_CONTAINER_NAME_ENV = "MAKERHUB_WEB_CONTAINER_NAME"
WEB_IMAGE_REF_ENV = "MAKERHUB_WEB_IMAGE_REF"
WORKER_CONTAINER_NAME_ENV = "MAKERHUB_WORKER_CONTAINER_NAME"
WORKER_IMAGE_REF_ENV = "MAKERHUB_WORKER_IMAGE_REF"
RUNTIME_CONFIG_ENV = "MAKERHUB_RUNTIME_CONFIG_JSON"
DATABASE_URL_ENV = "MAKERHUB_DATABASE_URL"
WORKER_HEARTBEAT_STATE_KEY = "worker_heartbeat"
WORKER_HEARTBEAT_MAX_AGE_SECONDS = 30
WORKER_START_TOKEN_ENV = "MAKERHUB_WORKER_START_TOKEN"
_CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}")
_RELEASE_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$")
STARTUP_WAIT_TIMEOUT_SECONDS = 20
STARTUP_WAIT_INTERVAL_SECONDS = 1.0
STARTUP_WAIT_STABLE_POLLS = 3
_NANOSECONDS_PER_SECOND = 1_000_000_000
IMAGE_CLEANUP_INITIAL_DELAY_SECONDS = 45
IMAGE_CLEANUP_RETRY_DELAY_SECONDS = 60
IMAGE_CLEANUP_MAX_ATTEMPTS = 5
HELPER_CLEANUP_INITIAL_DELAY_SECONDS = 15
HELPER_CLEANUP_RETRY_DELAY_SECONDS = 10
HELPER_CLEANUP_MAX_ATTEMPTS = 6
_IMAGE_CLEANUP_THREAD: threading.Thread | None = None
_IMAGE_CLEANUP_LOCK = threading.Lock()
_HELPER_CLEANUP_THREAD: threading.Thread | None = None
_HELPER_CLEANUP_LOCK = threading.Lock()

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None

PACKAGED_CANONICAL_COMPOSE_PATH = ROOT_DIR / "compose.yaml"
POSTGRES_COMPOSE_MIGRATION_MESSAGE = (
    "当前版本将归档模型索引迁移到 Postgres。检测到当前容器仍是旧 compose，"
    "缺少 MAKERHUB_DATABASE_URL / makerhub-postgres 服务，或仍使用旧的 /app/state、/app/archive、/app/local 分散挂载；"
    "请先按示例 compose 改成 App + Worker + Postgres + FlareSolverr 部署，并使用 /app/config + /app/data 新目录布局后，再执行网页更新。"
)


def packaged_canonical_compose() -> str:
    try:
        return PACKAGED_CANONICAL_COMPOSE_PATH.read_text(encoding="utf-8")
    except OSError:
        return "无法读取镜像内置的 compose.yaml，请从发布包重新获取 compose.yaml 后再升级。"


def _now_iso() -> str:
    return china_now_iso()


def record_worker_heartbeat(
    *,
    start_token: str = "",
    version: str = APP_VERSION,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    payload = {
        "start_token": str(start_token or ""),
        "version": str(version or ""),
        "updated_at": _now_iso(),
        "updated_at_epoch": float(time.time() if now_epoch is None else now_epoch),
    }
    return save_database_json_state(WORKER_HEARTBEAT_STATE_KEY, payload)


def worker_heartbeat_readiness(
    *,
    expected_start_token: str | None = None,
    expected_version: str | None = None,
    now_epoch: float | None = None,
    max_age_seconds: int = WORKER_HEARTBEAT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    payload = load_database_json_state_without_initialization(WORKER_HEARTBEAT_STATE_KEY, {})
    if not isinstance(payload, dict):
        return {"ready": False, "reason": "missing"}
    if expected_start_token is not None and str(payload.get("start_token") or "") != str(expected_start_token or ""):
        return {"ready": False, "reason": "start_token_mismatch"}
    if expected_version is not None and str(payload.get("version") or "") != str(expected_version or ""):
        return {"ready": False, "reason": "version_mismatch"}
    try:
        updated_at_epoch = float(payload.get("updated_at_epoch"))
    except (TypeError, ValueError):
        return {"ready": False, "reason": "missing"}
    current_epoch = time.time() if now_epoch is None else float(now_epoch)
    if current_epoch - updated_at_epoch > max(int(max_age_seconds or 0), 1):
        return {"ready": False, "reason": "stale"}
    return {"ready": True, "reason": ""}


def _default_update_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "phase": "idle",
        "message": "",
        "request_id": "",
        "requested_at": "",
        "started_at": "",
        "finished_at": "",
        "requested_by": "",
        "helper_container_id": "",
        "replacement_container_id": "",
        "container_name": "",
        "image_ref": "",
        "deployment_mode": "",
        "web_container_name": "",
        "web_image_ref": "",
        "web_replacement_container_id": "",
        "worker_container_name": "",
        "worker_image_ref": "",
        "worker_replacement_container_id": "",
        "old_image_ids": [],
        "image_cleanup_done": False,
        "image_cleanup_at": "",
        "image_cleanup_removed": [],
        "image_cleanup_errors": [],
        "target_version": "",
        "current_version": APP_VERSION,
        "last_error": "",
    }


def _read_update_state() -> dict[str, Any]:
    payload = load_database_json_state(UPDATE_STATE_KEY, {})
    state = _default_update_state()
    if isinstance(payload, dict):
        state.update(payload)
    state["current_version"] = APP_VERSION
    return state


def _write_update_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = _default_update_state()
    state.update(payload)
    state["current_version"] = APP_VERSION
    saved = save_database_json_state(UPDATE_STATE_KEY, state)
    publish_state_event(
        UPDATE_STATE_KEY,
        "system_update.changed",
        {
            "status": saved.get("status") or "idle",
            "phase": saved.get("phase") or "idle",
            "request_id": saved.get("request_id") or "",
        },
    )
    return saved


@contextmanager
def _update_state_process_lock(name: str):
    lock_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(name or "state")).strip("-") or "state"
    lock_path = UPDATE_STATE_PATH.with_name(f"{UPDATE_STATE_PATH.name}.{lock_name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _looks_like_container_id(value: str) -> bool:
    return bool(value and _CONTAINER_ID_PATTERN.fullmatch(str(value).strip()))


def _parse_bind_destination(bind_spec: str) -> tuple[str, str, str]:
    parts = str(bind_spec or "").split(":", 2)
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[2]


def _path_within_mount(destination: str, target: str) -> bool:
    if destination == target:
        return True
    destination_path = destination.rstrip("/")
    return bool(destination_path and target.startswith(f"{destination_path}/"))


def _join_mount_subpath(source: str, destination: str, target: str) -> str:
    if destination == target:
        return source
    relative = target.removeprefix(destination.rstrip("/")).lstrip("/")
    if not relative:
        return source
    return f"{source.rstrip('/')}/{relative}"


def _mount_spec_from_inspect(container_inspect: dict[str, Any], destination_path: Path | str) -> str:
    destination_path_text = str(destination_path)
    for bind in container_inspect.get("HostConfig", {}).get("Binds") or []:
        source, destination, options = _parse_bind_destination(bind)
        if _path_within_mount(destination, destination_path_text):
            mounted_source = _join_mount_subpath(source, destination, destination_path_text)
            return ":".join(part for part in (mounted_source, destination_path_text, options) if part)

    for mount in container_inspect.get("Mounts") or []:
        destination = str(mount.get("Destination") or "")
        if not _path_within_mount(destination, destination_path_text):
            continue
        mount_type = str(mount.get("Type") or "")
        if mount_type == "volume" and mount.get("Name"):
            source = str(mount.get("Name") or "")
        else:
            source = str(mount.get("Source") or "")
        if not source:
            continue
        suffix = ":ro" if mount.get("RW") is False else ""
        mounted_source = _join_mount_subpath(source, destination, destination_path_text)
        return f"{mounted_source}:{destination_path_text}{suffix}"
    return ""


def _state_mount_spec_from_inspect(container_inspect: dict[str, Any]) -> str:
    return _mount_spec_from_inspect(container_inspect, STATE_DIR)


def _same_image_ref(left: str, right: str) -> bool:
    return str(left or "").strip() == str(right or "").strip()


def _format_update_step_message(role_label: str, action: str) -> str:
    return f"正在更新{role_label}容器：{action}。"


def _extract_container_id_candidates() -> list[str]:
    candidates: list[str] = []
    hostname_file_value = ""
    if Path("/etc/hostname").exists():
        try:
            hostname_file_value = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        except OSError:
            hostname_file_value = ""

    for value in (
        os.getenv("HOSTNAME", "").strip(),
        hostname_file_value,
    ):
        if value and value not in candidates:
            candidates.append(value)

    cgroup_path = Path("/proc/self/cgroup")
    if cgroup_path.exists():
        try:
            for line in cgroup_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                for match in _CONTAINER_ID_PATTERN.findall(line):
                    if match not in candidates:
                        candidates.append(match)
        except OSError:
            pass

    return candidates


def _friendly_error_message(error: Exception | str) -> str:
    text = str(error or "").strip()
    if not text:
        return "更新失败。"
    return text.replace("\n", " ").strip()[:400]


def _normalize_version_label(value: Any) -> str:
    return str(value or "").strip().lstrip("vV")


def _versioned_image_ref(image_ref: str, target_version: str) -> str:
    """Replace a floating image tag or digest with the immutable release tag."""
    base_ref = str(image_ref or "").strip().split("@", 1)[0]
    version = _normalize_version_label(target_version)
    if not base_ref:
        raise RuntimeError("当前容器缺少镜像引用，无法确定要拉取的目标镜像。")
    if not _RELEASE_VERSION_PATTERN.fullmatch(version):
        raise RuntimeError("目标版本格式无效，无法构造发布镜像标签。")
    image_name, separator, possible_tag = base_ref.rpartition(":")
    if separator and "/" not in possible_tag:
        base_ref = image_name
    return f"{base_ref}:v{version}"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return max(min(numeric, maximum), minimum)


def normalize_runtime_resource_config(value: dict[str, Any] | None) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "web_workers": _bounded_int(payload.get("web_workers"), 1, 1, 8),
        "worker_concurrency": _bounded_int(payload.get("worker_concurrency"), 2, 1, 4),
    }


def _runtime_config_from_env() -> dict[str, Any]:
    raw = str(os.getenv(RUNTIME_CONFIG_ENV, "") or "").strip()
    if not raw:
        return normalize_runtime_resource_config({})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("运行资源配置格式无效。") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("运行资源配置格式无效。")
    try:
        return normalize_runtime_resource_config(payload)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _deployment_mode() -> str:
    value = str(os.getenv(DEPLOYMENT_MODE_ENV, "") or "").strip().lower()
    if value:
        return value
    if str(os.getenv(WORKER_CONTAINER_NAME_ENV, "") or "").strip():
        return "app-worker"
    return "split" if str(os.getenv(WEB_CONTAINER_NAME_ENV, "") or "").strip() else "single"


def _web_update_target(default_image_ref: str = "") -> dict[str, str]:
    container_name = str(os.getenv(WEB_CONTAINER_NAME_ENV, "") or "").strip()
    if not container_name:
        return {"container_name": "", "image_ref": ""}
    image_ref = str(os.getenv(WEB_IMAGE_REF_ENV, "") or "").strip() or str(default_image_ref or "").strip()
    return {"container_name": container_name, "image_ref": image_ref}


def _worker_update_target(default_image_ref: str = "") -> dict[str, str]:
    container_name = str(os.getenv(WORKER_CONTAINER_NAME_ENV, "") or "").strip()
    if not container_name:
        return {"container_name": "", "image_ref": ""}
    image_ref = str(os.getenv(WORKER_IMAGE_REF_ENV, "") or "").strip() or str(default_image_ref or "").strip()
    return {"container_name": container_name, "image_ref": image_ref}


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 60):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class DockerSocketClient:
    def __init__(self, socket_path: str | Path = DOCKER_SOCKET_PATH, timeout: float = 60):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | list[Any] | str | bytes | None = None,
        expected_statuses: set[int] | tuple[int, ...] = (200,),
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)

        raw_body: bytes | str | None
        if body is None:
            raw_body = None
        elif isinstance(body, (dict, list)):
            raw_body = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        else:
            raw_body = body

        connection = UnixSocketHTTPConnection(self.socket_path, timeout=self.timeout)
        response = None
        try:
            connection.request(method, path, body=raw_body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            payload: Any
            if raw and "application/json" in content_type:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = raw.decode("utf-8", errors="replace")
            else:
                payload = raw.decode("utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"Docker socket 访问失败：{exc}") from exc
        finally:
            connection.close()

        if response is None:
            raise RuntimeError("Docker API 未返回响应。")

        if response.status not in set(expected_statuses):
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("message") or payload.get("error") or payload.get("detail") or "").strip()
            elif isinstance(payload, str):
                detail = payload.strip()
            raise RuntimeError(detail or f"Docker API 请求失败：HTTP {response.status}")

        return response.status, payload, dict(response.getheaders())

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        _, payload, _ = self.request(
            "GET",
            f"/containers/{quote(container_id, safe='')}/json",
            expected_statuses={200},
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Docker 容器信息返回格式异常。")
        return payload

    def create_container(self, body: dict[str, Any], *, name: str = "") -> str:
        path = "/containers/create"
        if name:
            path = f"{path}?name={quote(name, safe='')}"
        _, payload, _ = self.request("POST", path, body=body, expected_statuses={201})
        if not isinstance(payload, dict) or not payload.get("Id"):
            raise RuntimeError("Docker 未返回新容器 ID。")
        return str(payload.get("Id") or "")

    def start_container(self, container_id: str) -> None:
        self.request(
            "POST",
            f"/containers/{quote(container_id, safe='')}/start",
            expected_statuses={204, 304},
        )

    def rename_container(self, container_id: str, *, name: str) -> None:
        self.request(
            "POST",
            f"/containers/{quote(container_id, safe='')}/rename?name={quote(name, safe='')}",
            expected_statuses={204},
        )

    def stop_container(self, container_id: str, *, timeout_seconds: int = 10) -> None:
        self.request(
            "POST",
            f"/containers/{quote(container_id, safe='')}/stop?t={int(timeout_seconds)}",
            expected_statuses={204, 304},
        )

    def list_containers(
        self,
        *,
        all_containers: bool = False,
        filters: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        path = f"/containers/json?all={1 if all_containers else 0}"
        if filters:
            encoded_filters = quote(json.dumps(filters, separators=(",", ":")), safe="")
            path = f"{path}&filters={encoded_filters}"
        _, payload, _ = self.request("GET", path, expected_statuses={200})
        if not isinstance(payload, list):
            raise RuntimeError("Docker 容器列表返回格式异常。")
        return [item for item in payload if isinstance(item, dict)]

    def remove_container(
        self,
        container_id: str,
        *,
        force: bool = False,
        missing_ok: bool = False,
    ) -> None:
        force_flag = "1" if force else "0"
        self.request(
            "DELETE",
            f"/containers/{quote(container_id, safe='')}" f"?force={force_flag}",
            expected_statuses={204, 404} if missing_ok else {204},
        )

    def remove_image(self, image_id: str, *, force: bool = False, noprune: bool = False) -> None:
        force_flag = "1" if force else "0"
        noprune_flag = "1" if noprune else "0"
        self.request(
            "DELETE",
            f"/images/{quote(image_id, safe='')}" f"?force={force_flag}&noprune={noprune_flag}",
            expected_statuses={200, 404},
        )

    def get_container_logs(self, container_id: str, *, tail: int = 120) -> str:
        _, payload, _ = self.request(
            "GET",
            (
                f"/containers/{quote(container_id, safe='')}/logs"
                f"?stdout=1&stderr=1&tail={max(int(tail or 0), 1)}"
            ),
            expected_statuses={200},
        )
        return str(payload or "").strip()

    def pull_image(self, image_ref: str) -> None:
        _, payload, _ = self.request(
            "POST",
            f"/images/create?fromImage={quote(image_ref, safe='')}",
            expected_statuses={200},
            headers={"X-Registry-Auth": "e30="},
        )
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            error_text = str(item.get("error") or item.get("message") or "").strip()
            if error_text:
                raise RuntimeError(error_text)


def _copy_fields(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if value in (None, "", [], {}):
            continue
        payload[key] = value
    return payload


def _set_env_value(values: list[Any], key: str, value: Any) -> list[str]:
    prefix = f"{key}="
    replacement = f"{key}={value}"
    result: list[str] = []
    replaced = False
    for item in values:
        text = str(item or "")
        if text.startswith(prefix):
            if not replaced:
                result.append(replacement)
                replaced = True
            continue
        result.append(text)
    if not replaced:
        result.append(replacement)
    return result


def _apply_runtime_resource_config(
    body: dict[str, Any],
    runtime_config: dict[str, Any] | None,
    *,
    role: str,
    worker_start_token: str = "",
) -> None:
    runtime = normalize_runtime_resource_config(runtime_config)
    normalized_role = str(role or "app").strip().lower()
    env = body.get("Env") or []
    if normalized_role == "worker":
        body["Cmd"] = ["worker"]
        env = _set_env_value(env, "MAKERHUB_ENTRYPOINT", "worker")
        env = _set_env_value(env, "MAKERHUB_PROCESS_ROLE", "worker")
        env = _set_env_value(env, "MAKERHUB_BACKGROUND_TASKS", "true")
        env = _set_env_value(env, "MAKERHUB_WORKER_CONCURRENCY", runtime.get("worker_concurrency") or 2)
        if worker_start_token:
            env = _set_env_value(env, WORKER_START_TOKEN_ENV, worker_start_token)
        body["Env"] = env
    elif normalized_role in {"app", "web"}:
        body["Cmd"] = ["app"]
        env = _set_env_value(env, "MAKERHUB_ENTRYPOINT", "app")
        env = _set_env_value(env, "MAKERHUB_PROCESS_ROLE", "app")
        env = _set_env_value(env, "MAKERHUB_BACKGROUND_TASKS", "false")
        env = _set_env_value(env, "MAKERHUB_WEB_WORKERS", runtime.get("web_workers") or 1)
        body["Env"] = env


def _build_replacement_container_body(
    container_inspect: dict[str, Any],
    image_ref: str,
    *,
    runtime_config: dict[str, Any] | None = None,
    role: str = "app",
    worker_start_token: str = "",
) -> dict[str, Any]:
    config = container_inspect.get("Config") or {}
    host_config_source = container_inspect.get("HostConfig") or {}
    container_id = str(container_inspect.get("Id") or "")

    body: dict[str, Any] = {"Image": image_ref}
    body.update(
        _copy_fields(
            config,
            (
                "User",
                "Env",
                "Cmd",
                "Entrypoint",
                "WorkingDir",
                "Labels",
                "ExposedPorts",
                "Volumes",
                "Healthcheck",
                "StopSignal",
                "StopTimeout",
                "Domainname",
                "MacAddress",
            ),
        )
    )

    for flag in ("OpenStdin", "StdinOnce", "Tty", "AttachStdin", "AttachStdout", "AttachStderr", "NetworkDisabled"):
        if flag in config:
            body[flag] = bool(config.get(flag))

    hostname = str(config.get("Hostname") or "").strip()
    if hostname and hostname not in {container_id[:12], container_id}:
        body["Hostname"] = hostname

    host_config = _copy_fields(
        host_config_source,
        (
            "Binds",
            "PortBindings",
            "RestartPolicy",
            "PublishAllPorts",
            "ReadonlyRootfs",
            "Dns",
            "DnsOptions",
            "DnsSearch",
            "ExtraHosts",
            "CapAdd",
            "CapDrop",
            "Devices",
            "DeviceCgroupRules",
            "Tmpfs",
            "ShmSize",
            "LogConfig",
            "SecurityOpt",
            "Ulimits",
            "Links",
            "VolumeDriver",
            "VolumesFrom",
            "GroupAdd",
            "OomScoreAdj",
            "PidMode",
            "Privileged",
            "Runtime",
            "StorageOpt",
            "Sysctls",
            "MaskedPaths",
            "ReadonlyPaths",
        ),
    )

    network_mode = str(host_config_source.get("NetworkMode") or "").strip()
    if network_mode:
        host_config["NetworkMode"] = network_mode

    if host_config:
        body["HostConfig"] = host_config

    endpoints_config: dict[str, Any] = {}
    for network_name, endpoint in (container_inspect.get("NetworkSettings") or {}).get("Networks", {}).items():
        aliases = []
        for alias in endpoint.get("Aliases") or []:
            alias_text = str(alias or "").strip()
            if not alias_text or _looks_like_container_id(alias_text):
                continue
            if alias_text not in aliases:
                aliases.append(alias_text)
        if aliases:
            endpoints_config[str(network_name)] = {"Aliases": aliases}

    if endpoints_config:
        body["NetworkingConfig"] = {"EndpointsConfig": endpoints_config}

    _apply_runtime_resource_config(
        body,
        runtime_config,
        role=role,
        worker_start_token=worker_start_token,
    )
    return body


def _helper_container_name(request_id: str) -> str:
    return f"{HELPER_CONTAINER_PREFIX}-{request_id[:12]}"


def _replacement_container_name(container_name: str, request_id: str) -> str:
    base_name = str(container_name or "makerhub").strip() or "makerhub"
    return f"{base_name}-replacement-{request_id[:8]}"


def _backup_container_name(container_name: str, request_id: str) -> str:
    base_name = str(container_name or "makerhub").strip() or "makerhub"
    return f"{base_name}-backup-{request_id[:8]}"


def _container_display_name(container_inspect: dict[str, Any], fallback: str = "") -> str:
    name = str(container_inspect.get("Name") or "").strip().lstrip("/")
    return name or str(fallback or "").strip()


def _assert_container_name(container_inspect: dict[str, Any], expected_name: str) -> None:
    actual_name = _container_display_name(container_inspect)
    if actual_name != str(expected_name or "").strip():
        raise RuntimeError(f"新容器名称异常：期望 {expected_name}，实际 {actual_name or 'unknown'}。")


def _resolve_self_container(client: DockerSocketClient) -> dict[str, Any]:
    last_error = ""
    for candidate in _extract_container_id_candidates():
        try:
            inspect = client.inspect_container(candidate)
        except Exception as exc:  # noqa: BLE001
            last_error = _friendly_error_message(exc)
            continue

        state_mount = _state_mount_spec_from_inspect(inspect)
        if not state_mount:
            raise RuntimeError(f"当前容器未挂载 {STATE_DIR}，无法持久化更新状态。")
        logs_mount = _mount_spec_from_inspect(inspect, LOGS_DIR)

        return {
            "container_id": str(inspect.get("Id") or candidate),
            "container_name": _container_display_name(inspect),
            "image_ref": str((inspect.get("Config") or {}).get("Image") or ""),
            "container_image_id": str(inspect.get("Image") or ""),
            "state_mount": state_mount,
            "logs_mount": logs_mount,
            "inspect": inspect,
        }

    raise RuntimeError(last_error or "无法识别当前容器实例。")


def _container_image_id(container_inspect: dict[str, Any]) -> str:
    return str(container_inspect.get("Image") or "").strip()


def _env_lookup(container_inspect: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (container_inspect.get("Config") or {}).get("Env") or []:
        text = str(item or "")
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        result[key] = value
    return result


def _container_resource_payload(container_inspect: dict[str, Any]) -> dict[str, Any]:
    env = _env_lookup(container_inspect)
    return {
        "web_workers": _bounded_int(env.get("MAKERHUB_WEB_WORKERS"), 1, 1, 8),
        "worker_concurrency": _bounded_int(env.get("MAKERHUB_WORKER_CONCURRENCY"), 2, 1, 4),
    }


def _helper_network_mode(container_inspect: dict[str, Any]) -> str:
    network_mode = str((container_inspect.get("HostConfig") or {}).get("NetworkMode") or "").strip()
    if network_mode and network_mode != "none":
        return network_mode
    networks = (container_inspect.get("NetworkSettings") or {}).get("Networks") or {}
    if isinstance(networks, dict):
        for name in networks:
            clean = str(name or "").strip()
            if clean and clean != "none":
                return clean
    return "bridge"


def _database_url_from_container(container_inspect: dict[str, Any]) -> str:
    return str(_env_lookup(container_inspect).get(DATABASE_URL_ENV) or "").strip()


def _compose_migration_required(container_inspect: dict[str, Any]) -> bool:
    if not _database_url_from_container(container_inspect):
        return True
    required_paths = (STATE_DIR, ARCHIVE_DIR, LOCAL_DIR)
    return any(not _mount_spec_from_inspect(container_inspect, path) for path in required_paths)


def _compose_migration_payload() -> dict[str, Any]:
    return {
        "compose_migration_required": True,
        "compose_migration_reason": POSTGRES_COMPOSE_MIGRATION_MESSAGE,
        "compose_example": packaged_canonical_compose(),
    }


def _append_unique_image_id(values: list[str], value: str) -> None:
    image_id = str(value or "").strip()
    if not image_id or image_id in values:
        return
    values.append(image_id)


def _cleanup_stopped_update_helpers(client: DockerSocketClient) -> dict[str, list[str]]:
    removed: list[str] = []
    active: list[str] = []
    errors: list[str] = []
    filters = {"label": [f"{HELPER_LABEL_KEY}={HELPER_LABEL_VALUE}"]}
    with _update_state_process_lock("helper-cleanup"):
        containers = client.list_containers(all_containers=True, filters=filters)
        for item in containers:
            container_id = str(item.get("Id") or "").strip()
            labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
            names = item.get("Names") if isinstance(item.get("Names"), list) else []
            managed_name = any(
                str(name or "").strip().lstrip("/").startswith(f"{HELPER_CONTAINER_PREFIX}-")
                for name in names
            )
            if (
                not container_id
                or labels.get(HELPER_LABEL_KEY) != HELPER_LABEL_VALUE
                or not managed_name
            ):
                continue
            state = str(item.get("State") or "").strip().lower()
            if state not in {"created", "exited", "dead"}:
                active.append(container_id)
                continue
            try:
                client.remove_container(container_id, force=True, missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{container_id}: {_friendly_error_message(exc)}")
            else:
                removed.append(container_id)
    return {"removed": removed, "active": active, "errors": errors}


def _run_delayed_stopped_update_helper_cleanup() -> None:
    time.sleep(max(float(HELPER_CLEANUP_INITIAL_DELAY_SECONDS), 0.0))
    last_result: dict[str, list[str]] = {"removed": [], "active": [], "errors": []}
    for attempt in range(max(int(HELPER_CLEANUP_MAX_ATTEMPTS), 1)):
        if not DOCKER_SOCKET_PATH.exists():
            return
        try:
            result = _cleanup_stopped_update_helpers(DockerSocketClient(timeout=30))
        except Exception as exc:  # noqa: BLE001
            result = {"removed": [], "active": [], "errors": [_friendly_error_message(exc)]}
        last_result = result
        if result.get("removed"):
            append_business_log(
                "system",
                "self_update_helper_cleanup_succeeded",
                "已清理网页更新留下的临时 helper 容器。",
                container_ids=result.get("removed") or [],
            )
        if not result.get("active") and not result.get("errors"):
            return
        if attempt < max(int(HELPER_CLEANUP_MAX_ATTEMPTS), 1) - 1:
            time.sleep(max(float(HELPER_CLEANUP_RETRY_DELAY_SECONDS), 1.0))

    append_business_log(
        "system",
        "self_update_helper_cleanup_warning",
        "网页更新临时 helper 容器自动清理未完成。",
        level="warning",
        active_container_ids=last_result.get("active") or [],
        errors=last_result.get("errors") or [],
    )


def _schedule_stopped_update_helper_cleanup() -> None:
    if not DOCKER_SOCKET_PATH.exists():
        return
    global _HELPER_CLEANUP_THREAD
    with _HELPER_CLEANUP_LOCK:
        if _HELPER_CLEANUP_THREAD and _HELPER_CLEANUP_THREAD.is_alive():
            return
        _HELPER_CLEANUP_THREAD = threading.Thread(
            target=_run_delayed_stopped_update_helper_cleanup,
            name="makerhub-helper-cleanup",
            daemon=True,
        )
        _HELPER_CLEANUP_THREAD.start()


def _cleanup_old_update_images(state: dict[str, Any]) -> dict[str, Any]:
    with _update_state_process_lock("image-cleanup"):
        latest_state = _read_update_state()
        if (
            str(latest_state.get("request_id") or "") == str(state.get("request_id") or "")
            and latest_state.get("image_cleanup_done")
        ):
            return latest_state
        if str(latest_state.get("request_id") or "") != str(state.get("request_id") or ""):
            return latest_state
        if str(latest_state.get("request_id") or "") == str(state.get("request_id") or ""):
            state = latest_state

        image_ids = [
            str(item or "").strip()
            for item in (state.get("old_image_ids") or [])
            if str(item or "").strip()
        ]
        if not image_ids:
            return _write_update_state(
                {
                    **state,
                    "image_cleanup_done": True,
                    "image_cleanup_at": _now_iso(),
                    "image_cleanup_removed": [],
                    "image_cleanup_errors": [],
                }
            )
        if not DOCKER_SOCKET_PATH.exists():
            error = "当前容器没有挂载 Docker socket，无法自动清理旧镜像。"
            append_business_log(
                "system",
                "self_update_image_cleanup_warning",
                error,
                level="warning",
                request_id=str(state.get("request_id") or ""),
                image_ids=image_ids,
            )
            return _write_update_state(
                {
                    **state,
                    "image_cleanup_done": False,
                    "image_cleanup_errors": [error],
                }
            )

        client = DockerSocketClient(timeout=60)
        current_image_id = ""
        try:
            metadata = _resolve_self_container(client)
            current_image_id = str(metadata.get("container_image_id") or "").strip()
        except Exception as exc:  # noqa: BLE001
            append_business_log(
                "system",
                "self_update_image_cleanup_warning",
                "旧镜像清理前读取当前容器镜像失败，将跳过当前镜像保护。",
                level="warning",
                request_id=str(state.get("request_id") or ""),
                error=_friendly_error_message(exc),
            )

        removed: list[str] = []
        errors: list[str] = []
        for image_id in image_ids:
            if current_image_id and image_id == current_image_id:
                continue
            try:
                client.remove_image(image_id, force=False, noprune=False)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{image_id}: {_friendly_error_message(exc)}")
            else:
                removed.append(image_id)

        cleanup_done = not errors
        append_business_log(
            "system",
            "self_update_image_cleanup_succeeded" if cleanup_done else "self_update_image_cleanup_warning",
            "网页更新后的旧镜像已清理。" if cleanup_done else "网页更新后的部分旧镜像清理失败。",
            level="info" if cleanup_done else "warning",
            request_id=str(state.get("request_id") or ""),
            removed_image_ids=removed,
            errors=errors,
        )
        return _write_update_state(
            {
                **state,
                "image_cleanup_done": cleanup_done,
                "image_cleanup_at": _now_iso(),
                "image_cleanup_removed": removed,
                "image_cleanup_errors": errors,
            }
        )


def _run_delayed_image_cleanup(request_id: str) -> None:
    time.sleep(max(float(IMAGE_CLEANUP_INITIAL_DELAY_SECONDS), 0.0))
    last_state: dict[str, Any] = {}
    for attempt in range(max(int(IMAGE_CLEANUP_MAX_ATTEMPTS), 1)):
        state = _read_update_state()
        last_state = state
        if str(state.get("request_id") or "") != str(request_id or ""):
            return
        if str(state.get("status") or "") != "succeeded":
            return
        if not state.get("old_image_ids") or state.get("image_cleanup_done"):
            return
        last_state = _cleanup_old_update_images(state)
        if last_state.get("image_cleanup_done"):
            return
        if attempt < max(int(IMAGE_CLEANUP_MAX_ATTEMPTS), 1) - 1:
            time.sleep(max(float(IMAGE_CLEANUP_RETRY_DELAY_SECONDS), 1.0))

    if last_state.get("image_cleanup_errors"):
        append_business_log(
            "system",
            "self_update_image_cleanup_warning",
            "网页更新后的旧镜像自动清理已重试多次，仍有部分镜像未清理。",
            level="warning",
            request_id=str(request_id or ""),
            errors=last_state.get("image_cleanup_errors") or [],
        )


def _schedule_old_update_image_cleanup(request_id: str) -> None:
    global _IMAGE_CLEANUP_THREAD
    with _IMAGE_CLEANUP_LOCK:
        if _IMAGE_CLEANUP_THREAD and _IMAGE_CLEANUP_THREAD.is_alive():
            return
        _IMAGE_CLEANUP_THREAD = threading.Thread(
            target=_run_delayed_image_cleanup,
            args=(str(request_id or ""),),
            name="makerhub-image-cleanup",
            daemon=True,
        )
        _IMAGE_CLEANUP_THREAD.start()


def get_update_capability() -> dict[str, Any]:
    payload = {
        "supported": False,
        "support_reason": "",
        "compose_migration_required": False,
        "compose_migration_reason": "",
        "compose_example": "",
        "docker_socket_mounted": DOCKER_SOCKET_PATH.exists(),
        "container_name": "",
        "image_ref": "",
        "deployment_mode": _deployment_mode(),
        "web_container_name": "",
        "web_image_ref": "",
        "worker_container_name": "",
        "worker_image_ref": "",
        "resources": {},
    }
    if not DOCKER_SOCKET_PATH.exists():
        payload["support_reason"] = "当前容器没有挂载 /var/run/docker.sock，不能从网页直接触发 Docker 更新。"
        return payload

    try:
        client = DockerSocketClient(timeout=15)
        metadata = _resolve_self_container(client)
    except Exception as exc:  # noqa: BLE001
        payload["support_reason"] = _friendly_error_message(exc)
        return payload

    if _compose_migration_required(metadata.get("inspect") or {}):
        payload.update(_compose_migration_payload())
        payload["support_reason"] = POSTGRES_COMPOSE_MIGRATION_MESSAGE
        payload["container_name"] = str(metadata.get("container_name") or "")
        payload["image_ref"] = str(metadata.get("image_ref") or "")
        return payload

    web_target = _web_update_target(str(metadata.get("image_ref") or ""))
    if web_target.get("container_name"):
        try:
            web_inspect = client.inspect_container(str(web_target.get("container_name") or ""))
        except Exception as exc:  # noqa: BLE001
            payload["support_reason"] = f"Web 容器无法访问：{_friendly_error_message(exc)}"
            return payload
        payload["web_container_name"] = str(web_inspect.get("Name") or web_target.get("container_name") or "").lstrip("/")
        payload["web_image_ref"] = str(web_target.get("image_ref") or "")

    worker_target = _worker_update_target(str(metadata.get("image_ref") or ""))
    if worker_target.get("container_name"):
        try:
            worker_inspect = client.inspect_container(str(worker_target.get("container_name") or ""))
        except Exception as exc:  # noqa: BLE001
            payload["support_reason"] = f"Worker 容器无法访问：{_friendly_error_message(exc)}"
            return payload
        payload["worker_container_name"] = str(worker_inspect.get("Name") or worker_target.get("container_name") or "").lstrip("/")
        payload["worker_image_ref"] = str(worker_target.get("image_ref") or "")

    app_resources = _container_resource_payload(metadata.get("inspect") or {})
    worker_resources = _container_resource_payload(worker_inspect) if "worker_inspect" in locals() else {}
    payload.update(
        {
            "supported": True,
            "support_reason": "",
            "container_name": str(metadata.get("container_name") or ""),
            "image_ref": str(metadata.get("image_ref") or ""),
            "deployment_mode": _deployment_mode(),
            "resources": {
                "app": app_resources,
                "worker": worker_resources,
            },
        }
    )
    return payload


def _runtime_role_diagnostics(capability: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    deployment_mode = str(capability.get("deployment_mode") or state.get("deployment_mode") or _deployment_mode() or "")
    app_container_name = str(capability.get("container_name") or state.get("container_name") or "").strip()
    app_image_ref = str(capability.get("image_ref") or state.get("image_ref") or "").strip()
    web_target = _web_update_target(app_image_ref)
    worker_target = _worker_update_target(app_image_ref)
    web_container_name = str(
        capability.get("web_container_name")
        or state.get("web_container_name")
        or web_target.get("container_name")
        or ""
    ).strip()
    web_image_ref = str(
        capability.get("web_image_ref")
        or state.get("web_image_ref")
        or web_target.get("image_ref")
        or ""
    ).strip()
    worker_container_name = str(
        capability.get("worker_container_name")
        or state.get("worker_container_name")
        or worker_target.get("container_name")
        or ""
    ).strip()
    worker_image_ref = str(
        capability.get("worker_image_ref")
        or state.get("worker_image_ref")
        or worker_target.get("image_ref")
        or ""
    ).strip()
    resources = capability.get("resources") if isinstance(capability.get("resources"), dict) else {}
    supported = bool(capability.get("supported"))
    support_reason = str(capability.get("support_reason") or "")

    roles = [
        {
            "role": "app",
            "container_name": app_container_name,
            "image_ref": app_image_ref,
            "reachable": bool(app_container_name) and (supported or not support_reason.startswith("当前容器")),
            "resources": resources.get("app") if isinstance(resources.get("app"), dict) else {},
        }
    ]
    if web_container_name:
        roles.append(
            {
                "role": "web",
                "container_name": web_container_name,
                "image_ref": web_image_ref,
                "reachable": supported or not support_reason.startswith("Web 容器无法访问"),
                "resources": resources.get("web") if isinstance(resources.get("web"), dict) else {},
            }
        )
    if worker_container_name:
        roles.append(
            {
                "role": "worker",
                "container_name": worker_container_name,
                "image_ref": worker_image_ref,
                "reachable": supported or not support_reason.startswith("Worker 容器无法访问"),
                "resources": resources.get("worker") if isinstance(resources.get("worker"), dict) else {},
            }
        )

    return {
        "deployment_mode": deployment_mode,
        "docker_socket_mounted": bool(capability.get("docker_socket_mounted")),
        "supported": supported,
        "support_reason": support_reason,
        "roles": roles,
    }


def get_update_status() -> dict[str, Any]:
    state = _read_update_state()
    capability = get_update_capability()
    runtime_diagnostics = _runtime_role_diagnostics(capability, state)
    return {
        **state,
        "supported": bool(capability.get("supported")),
        "support_reason": str(capability.get("support_reason") or ""),
        "compose_migration_required": bool(capability.get("compose_migration_required")),
        "compose_migration_reason": str(capability.get("compose_migration_reason") or ""),
        "compose_example": str(capability.get("compose_example") or ""),
        "docker_socket_mounted": bool(capability.get("docker_socket_mounted")),
        "container_name": str(capability.get("container_name") or state.get("container_name") or ""),
        "image_ref": str(capability.get("image_ref") or state.get("image_ref") or ""),
        "deployment_mode": str(capability.get("deployment_mode") or state.get("deployment_mode") or ""),
        "web_container_name": str(capability.get("web_container_name") or state.get("web_container_name") or ""),
        "web_image_ref": str(capability.get("web_image_ref") or state.get("web_image_ref") or ""),
        "worker_container_name": str(capability.get("worker_container_name") or state.get("worker_container_name") or ""),
        "worker_image_ref": str(capability.get("worker_image_ref") or state.get("worker_image_ref") or ""),
        "resources": capability.get("resources") if isinstance(capability.get("resources"), dict) else {},
        "runtime_diagnostics": runtime_diagnostics,
    }


def request_system_update(*, requested_by: str = "", target_version: str = "", force: bool = False) -> dict[str, Any]:
    state = _read_update_state()
    if str(state.get("status") or "") in ACTIVE_UPDATE_STATUSES:
        raise RuntimeError("已有系统更新任务在执行中，请稍后再试。")

    if not DOCKER_SOCKET_PATH.exists():
        raise RuntimeError("当前容器没有挂载 /var/run/docker.sock，无法执行网页一键更新。")

    client = DockerSocketClient(timeout=30)
    metadata = _resolve_self_container(client)
    if _compose_migration_required(metadata.get("inspect") or {}):
        raise RuntimeError(
            f"{POSTGRES_COMPOSE_MIGRATION_MESSAGE}\n\n示例 compose:\n{packaged_canonical_compose()}"
        )

    request_id = uuid.uuid4().hex
    helper_name = _helper_container_name(request_id)
    requested_at = _now_iso()
    requested_version = _normalize_version_label(target_version) or _normalize_version_label(APP_VERSION)
    target_image = _versioned_image_ref(str(metadata.get("image_ref") or ""), requested_version)
    helper_image = str(metadata.get("container_image_id") or target_image)
    deployment_mode = _deployment_mode()
    web_target = _web_update_target(str(metadata.get("image_ref") or ""))
    worker_target = _worker_update_target(str(metadata.get("image_ref") or ""))
    if web_target.get("container_name"):
        web_target["image_ref"] = _versioned_image_ref(str(web_target.get("image_ref") or ""), requested_version)
    if worker_target.get("container_name"):
        worker_target["image_ref"] = _versioned_image_ref(str(worker_target.get("image_ref") or ""), requested_version)
    runtime_config = _runtime_config_from_env()
    old_image_ids: list[str] = []
    _append_unique_image_id(old_image_ids, str(metadata.get("container_image_id") or ""))
    if not helper_image:
        raise RuntimeError("当前容器缺少本地镜像 ID，无法启动更新 helper。")
    if web_target.get("container_name"):
        try:
            web_inspect = client.inspect_container(str(web_target.get("container_name") or ""))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Web 容器无法访问：{_friendly_error_message(exc)}") from exc
        _append_unique_image_id(old_image_ids, _container_image_id(web_inspect))
    if worker_target.get("container_name"):
        try:
            worker_inspect = client.inspect_container(str(worker_target.get("container_name") or ""))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Worker 容器无法访问：{_friendly_error_message(exc)}") from exc
        _append_unique_image_id(old_image_ids, _container_image_id(worker_inspect))
    helper_cmd = [
        "python",
        "-m",
        "app.services.self_update",
        "--run-helper",
        "--request-id",
        request_id,
        "--container-id",
        str(metadata.get("container_id") or ""),
        "--image-ref",
        target_image,
        "--deployment-mode",
        deployment_mode,
    ]
    if web_target.get("container_name"):
        helper_cmd.extend(
            [
                "--web-container",
                str(web_target.get("container_name") or ""),
                "--web-image-ref",
                str(web_target.get("image_ref") or target_image),
            ]
        )
    if worker_target.get("container_name"):
        helper_cmd.extend(
            [
                "--worker-container",
                str(worker_target.get("container_name") or ""),
                "--worker-image-ref",
                str(worker_target.get("image_ref") or target_image),
            ]
        )
    helper_binds = [
        f"{DOCKER_SOCKET_PATH}:{DOCKER_SOCKET_PATH}",
        str(metadata.get("state_mount") or ""),
    ]
    logs_mount = str(metadata.get("logs_mount") or "").strip()
    if logs_mount:
        helper_binds.append(logs_mount)
    helper_env = [
        f"MAKERHUB_STATE_DIR={STATE_DIR}",
        f"MAKERHUB_LOGS_DIR={LOGS_DIR}",
        f"{RUNTIME_CONFIG_ENV}={json.dumps(runtime_config, ensure_ascii=False, separators=(',', ':'))}",
        "PYTHONUNBUFFERED=1",
    ]
    database_url = _database_url_from_container(metadata.get("inspect") or {})
    if database_url:
        helper_env.append(f"{DATABASE_URL_ENV}={database_url}")
    helper_body = {
        "Image": helper_image,
        "Cmd": helper_cmd,
        "Env": helper_env,
        "Labels": {
            HELPER_LABEL_KEY: HELPER_LABEL_VALUE,
            "com.makerhub.self_update.request_id": request_id,
        },
        "HostConfig": {
            "AutoRemove": True,
            "Binds": helper_binds,
            "NetworkMode": _helper_network_mode(metadata.get("inspect") or {}),
            "RestartPolicy": {"Name": "no"},
        },
    }

    base_state = _write_update_state(
        {
            "status": "queued",
            "phase": "queued",
            "message": "已提交系统更新，正在启动更新 helper。",
            "request_id": request_id,
            "requested_at": requested_at,
            "started_at": "",
            "finished_at": "",
            "requested_by": requested_by,
            "helper_container_id": "",
            "replacement_container_id": "",
            "container_name": str(metadata.get("container_name") or ""),
            "image_ref": target_image,
            "deployment_mode": deployment_mode,
            "web_container_name": str(web_target.get("container_name") or ""),
            "web_image_ref": str(web_target.get("image_ref") or ""),
            "web_replacement_container_id": "",
            "worker_container_name": str(worker_target.get("container_name") or ""),
            "worker_image_ref": str(worker_target.get("image_ref") or ""),
            "worker_replacement_container_id": "",
            "old_image_ids": old_image_ids,
            "image_cleanup_done": False,
            "image_cleanup_at": "",
            "image_cleanup_removed": [],
            "image_cleanup_errors": [],
            "target_version": requested_version,
            "last_error": "",
        }
    )

    try:
        helper_container_id = client.create_container(helper_body, name=helper_name)
        _write_update_state(
            {
                **base_state,
                "status": "launching_helper",
                "phase": "launching_helper",
                "message": "更新 helper 已创建，准备开始拉取镜像。",
                "helper_container_id": helper_container_id,
            }
        )
        client.start_container(helper_container_id)
    except Exception as exc:  # noqa: BLE001
        error_message = _friendly_error_message(exc)
        failed_state = _write_update_state(
            {
                **base_state,
                "status": "failed",
                "phase": "failed",
                "message": error_message,
                "finished_at": _now_iso(),
                "last_error": error_message,
            }
        )
        append_business_log(
            "system",
            "self_update_failed",
            error_message,
            level="error",
            request_id=request_id,
            container_name=str(metadata.get("container_name") or ""),
            image_ref=target_image,
        )
        return failed_state

    append_business_log(
        "system",
        "self_update_requested",
        "系统更新已提交，等待 helper 执行。",
        request_id=request_id,
        requested_by=requested_by,
        helper_container_id=helper_container_id,
        container_name=str(metadata.get("container_name") or ""),
        image_ref=target_image,
        deployment_mode=deployment_mode,
        web_container_name=str(web_target.get("container_name") or ""),
        web_image_ref=str(web_target.get("image_ref") or ""),
        worker_container_name=str(worker_target.get("container_name") or ""),
        worker_image_ref=str(worker_target.get("image_ref") or ""),
        target_version=requested_version,
        force=bool(force),
    )
    return get_update_status()


def mark_update_started_after_restart() -> None:
    _schedule_stopped_update_helper_cleanup()
    state = _read_update_state()
    status = str(state.get("status") or "")
    if status == "pending_startup":
        with _update_state_process_lock("startup"):
            state = _read_update_state()
            if str(state.get("status") or "") != "pending_startup":
                return
            finished_at = _now_iso()
            target_version = _normalize_version_label(state.get("target_version"))
            current_version = _normalize_version_label(APP_VERSION)
            if target_version and current_version and target_version != current_version:
                error_message = (
                    f"容器已重新启动，但当前版本仍为 v{APP_VERSION}，未达到目标版本 v{target_version}。"
                    " 通常是最新 Docker 镜像还未构建完成或镜像仓库仍返回旧 latest，请稍后再试。"
                )
                state = _write_update_state(
                    {
                        **state,
                        "status": "failed",
                        "phase": "version_mismatch",
                        "message": error_message,
                        "finished_at": finished_at,
                        "last_error": error_message,
                    }
                )
                append_business_log(
                    "system",
                    "self_update_failed",
                    error_message,
                    level="error",
                    request_id=str(state.get("request_id") or ""),
                    replacement_container_id=str(state.get("replacement_container_id") or ""),
                    container_name=str(state.get("container_name") or ""),
                    app_version=APP_VERSION,
                    target_version=target_version,
                    phase="version_mismatch",
                )
                return
            state = _write_update_state(
                {
                    **state,
                    "status": "succeeded",
                    "phase": "completed",
                    "message": f"系统已重新启动，当前版本 v{APP_VERSION}。",
                    "finished_at": finished_at,
                    "last_error": "",
                }
            )
            append_business_log(
                "system",
                "self_update_succeeded",
                "系统更新后已重新启动。",
                request_id=str(state.get("request_id") or ""),
                replacement_container_id=str(state.get("replacement_container_id") or ""),
                container_name=str(state.get("container_name") or ""),
                app_version=APP_VERSION,
            )
    elif status != "succeeded":
        return
    if state.get("old_image_ids") and not state.get("image_cleanup_done"):
        _schedule_old_update_image_cleanup(str(state.get("request_id") or ""))


def _update_state_from_helper(request_id: str, **fields: Any) -> dict[str, Any]:
    state = _read_update_state()
    if str(state.get("request_id") or "") != str(request_id or ""):
        state["request_id"] = str(request_id or "")
    state.update(fields)
    return _write_update_state(state)


def _container_state_summary(container_inspect: dict[str, Any]) -> str:
    state = container_inspect.get("State") or {}
    status = str(state.get("Status") or "").strip() or "unknown"
    running = bool(state.get("Running"))
    restarting = bool(state.get("Restarting"))
    exit_code = state.get("ExitCode")
    error = str(state.get("Error") or "").strip()
    health = state.get("Health") or {}
    health_status = str(health.get("Status") or "").strip()

    parts = [f"status={status}", f"running={running}"]
    if restarting:
        parts.append("restarting=true")
    if exit_code not in (None, ""):
        parts.append(f"exit_code={exit_code}")
    if health_status:
        parts.append(f"health={health_status}")
    if error:
        parts.append(f"error={error}")
    return ", ".join(parts)


def _container_startup_logs(client: DockerSocketClient, container_id: str) -> str:
    try:
        logs = client.get_container_logs(container_id, tail=80)
    except Exception as exc:  # noqa: BLE001
        return f"（启动日志读取失败：{_friendly_error_message(exc)}）"
    if not logs:
        return "（没有读取到启动日志）"
    compact = " | ".join(line.strip() for line in logs.splitlines() if line.strip())
    compact = compact[:1200].strip()
    return compact or "（没有读取到启动日志）"


def _wait_for_replacement_container(
    client: DockerSocketClient,
    container_id: str,
    *,
    timeout_seconds: int = STARTUP_WAIT_TIMEOUT_SECONDS,
    interval_seconds: float = STARTUP_WAIT_INTERVAL_SECONDS,
    stable_polls: int = STARTUP_WAIT_STABLE_POLLS,
) -> dict[str, Any]:
    initial_inspect = client.inspect_container(container_id)
    deadline = time.monotonic() + _replacement_startup_timeout_seconds(
        initial_inspect,
        fallback_timeout_seconds=timeout_seconds,
    )
    stable_count = 0
    last_inspect: dict[str, Any] = {}
    inspect = initial_inspect

    while time.monotonic() < deadline:
        last_inspect = inspect
        state = inspect.get("State") or {}
        status = str(state.get("Status") or "").strip().lower()
        running = bool(state.get("Running"))
        restarting = bool(state.get("Restarting"))
        exit_code = state.get("ExitCode")
        health = state.get("Health") or {}
        health_status = str(health.get("Status") or "").strip().lower()

        if running and not restarting:
            if health_status == "unhealthy":
                logs = _container_startup_logs(client, container_id)
                raise RuntimeError(
                    f"新容器健康检查失败（{_container_state_summary(inspect)}）。启动日志：{logs}"
                )
            if health_status not in {"", "healthy"}:
                stable_count = 0
            else:
                stable_count += 1
                if stable_count >= max(int(stable_polls or 0), 1):
                    return inspect
        else:
            stable_count = 0
            if status in {"exited", "dead"} or (exit_code not in (None, "") and not running):
                logs = _container_startup_logs(client, container_id)
                raise RuntimeError(
                    f"新容器启动后未能保持运行（{_container_state_summary(inspect)}）。启动日志：{logs}"
                )

        time.sleep(max(float(interval_seconds or 0), 0.2))
        inspect = client.inspect_container(container_id)

    logs = _container_startup_logs(client, container_id) if last_inspect else "（未读取到容器状态）"
    raise RuntimeError(
        f"等待新容器恢复超时（{_container_state_summary(last_inspect) if last_inspect else 'no-state'}）。"
        f" 启动日志：{logs}"
    )


def _replacement_startup_timeout_seconds(
    container_inspect: dict[str, Any],
    *,
    fallback_timeout_seconds: int,
) -> int:
    fallback = max(int(fallback_timeout_seconds or 0), 1)
    healthcheck = (container_inspect.get("Config") or {}).get("Healthcheck") or {}
    test = healthcheck.get("Test") or []
    if not healthcheck or (test and str(test[0] or "").upper() == "NONE"):
        return fallback

    def duration(value: Any, default_seconds: int) -> int:
        try:
            value_ns = int(value)
        except (TypeError, ValueError):
            value_ns = 0
        if value_ns <= 0:
            return default_seconds * _NANOSECONDS_PER_SECOND
        return value_ns

    try:
        retries = max(int(healthcheck.get("Retries") or 0), 1)
    except (TypeError, ValueError):
        retries = 1
    total_ns = (
        duration(healthcheck.get("StartPeriod"), 0)
        + retries * duration(healthcheck.get("Interval"), 30)
        + duration(healthcheck.get("Timeout"), 30)
    )
    healthcheck_timeout = (total_ns + _NANOSECONDS_PER_SECOND - 1) // _NANOSECONDS_PER_SECOND
    return max(fallback, healthcheck_timeout)


def _replace_related_container(
    client: DockerSocketClient,
    *,
    request_id: str,
    container_ref: str,
    image_ref: str,
    role: str,
    runtime_config: dict[str, Any] | None = None,
    image_already_pulled: bool = False,
) -> dict[str, str]:
    container_inspect = client.inspect_container(container_ref)
    old_container_id = str(container_inspect.get("Id") or container_ref)
    container_name = _container_display_name(container_inspect, str(container_ref or ""))
    replacement_container_id = ""
    replacement_container_name = container_name
    backup_container_name = _backup_container_name(container_name, request_id)
    old_container_renamed = False
    replacement_container_renamed = False

    def rollback() -> None:
        rollback_errors: list[str] = []
        if replacement_container_id:
            try:
                client.remove_container(replacement_container_id, force=True)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"remove-{role}-new:{_friendly_error_message(exc)}")
        if old_container_renamed:
            try:
                client.rename_container(old_container_id, name=container_name)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"rename-{role}-old:{_friendly_error_message(exc)}")
            else:
                try:
                    client.start_container(old_container_id)
                except Exception as exc:  # noqa: BLE001
                    rollback_errors.append(f"start-{role}-old:{_friendly_error_message(exc)}")
        else:
            try:
                client.start_container(old_container_id)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"restart-{role}-old:{_friendly_error_message(exc)}")
        if rollback_errors:
            raise RuntimeError("；".join(rollback_errors))

    try:
        role_label = "Worker" if role == "worker" else "Web" if role == "web" else role
        if image_already_pulled:
            _update_state_from_helper(
                request_id,
                status="running",
                phase=f"updating_{role}",
                message=_format_update_step_message(role_label, "复用已拉取镜像"),
            )
        else:
            _update_state_from_helper(
                request_id,
                status="running",
                phase=f"pulling_{role}",
                message=_format_update_step_message(role_label, "正在拉取镜像"),
            )
            client.pull_image(image_ref)
        _update_state_from_helper(
            request_id,
            status="running",
            phase=f"creating_{role}",
            message=_format_update_step_message(role_label, "正在创建新实例"),
        )
        replacement_body = _build_replacement_container_body(
            container_inspect,
            image_ref,
            runtime_config=runtime_config,
            role=role,
        )
        client.stop_container(old_container_id, timeout_seconds=10)
        client.rename_container(old_container_id, name=backup_container_name)
        old_container_renamed = True
        replacement_container_id = client.create_container(replacement_body, name=container_name)
        replacement_container_renamed = True
        state_fields: dict[str, str] = {}
        if role in {"web", "worker"}:
            state_fields[f"{role}_replacement_container_id"] = replacement_container_id
        _update_state_from_helper(request_id, **state_fields)
        _update_state_from_helper(
            request_id,
            status="running",
            phase=f"switching_{role}",
            message=_format_update_step_message(role_label, "正在切换旧实例"),
            **state_fields,
        )
        _update_state_from_helper(
            request_id,
            status="running",
            phase=f"starting_{role}",
            message=_format_update_step_message(role_label, "正在启动并等待健康检查"),
            **state_fields,
        )
        client.start_container(replacement_container_id)
        started_inspect = _wait_for_replacement_container(client, replacement_container_id)
        _assert_container_name(started_inspect, container_name)
        try:
            client.remove_container(old_container_id, force=True)
        except Exception as exc:  # noqa: BLE001
            append_business_log(
                "system",
                "self_update_cleanup_warning",
                f"{role} 新容器已启动，但旧容器清理失败。",
                level="warning",
                request_id=request_id,
                container_name=container_name,
                backup_container_name=backup_container_name,
                error=_friendly_error_message(exc),
            )
        return {
            "container_name": container_name,
            "old_container_id": old_container_id,
            "replacement_container_id": replacement_container_id,
            "replacement_container_name": container_name,
        }
    except Exception as exc:  # noqa: BLE001
        rollback_error = ""
        if replacement_container_id or old_container_renamed:
            try:
                rollback()
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_error = _friendly_error_message(rollback_exc)
        message = _friendly_error_message(exc)
        if rollback_error:
            message = f"{message}；{role} 自动回滚失败：{rollback_error}"
        raise RuntimeError(message) from exc


def _release_role_label(role: str) -> str:
    labels = {"app": "App", "web": "Web", "worker": "Worker"}
    return labels.get(str(role or "").strip().lower(), str(role or "container"))


def prepare_release_group(
    client: DockerSocketClient,
    *,
    request_id: str,
    roles: list[dict[str, str]],
    runtime_config: dict[str, Any] | None = None,
    target_version: str = "",
) -> dict[str, Any]:
    prepared_roles: list[dict[str, Any]] = []
    pulled_images: set[str] = set()
    for requested_role in roles:
        role = str(requested_role.get("role") or "").strip().lower()
        container_ref = str(requested_role.get("container_ref") or "").strip()
        image_ref = str(requested_role.get("image_ref") or "").strip()
        if role not in {"app", "web", "worker"} or not container_ref or not image_ref:
            raise RuntimeError(f"{_release_role_label(role)} ({role}) 准备更新参数无效。")
        try:
            inspect = client.inspect_container(container_ref)
            old_container_id = str(inspect.get("Id") or container_ref)
            container_name = _container_display_name(inspect, container_ref)
            worker_start_token = uuid.uuid4().hex if role == "worker" else ""
            body = _build_replacement_container_body(
                inspect,
                image_ref,
                runtime_config=runtime_config,
                role=role,
                worker_start_token=worker_start_token,
            )
            prepared_roles.append(
                {
                    "role": role,
                    "container_name": container_name,
                    "old_container_id": old_container_id,
                    "backup_container_name": _backup_container_name(container_name, request_id),
                    "replacement_body": body,
                    "image_ref": image_ref,
                    "worker_start_token": worker_start_token,
                    "candidate_container_id": "",
                    "backup_renamed": False,
                    "old_stopped": False,
                    "candidate_created": False,
                    "candidate_started": False,
                }
            )
            if image_ref not in pulled_images:
                client.pull_image(image_ref)
                pulled_images.add(image_ref)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"{_release_role_label(role)} ({role}) 准备失败：{_friendly_error_message(exc)}"
            ) from exc
    return {
        "request_id": str(request_id or ""),
        "roles": prepared_roles,
        "target_version": _normalize_version_label(target_version),
        "committed": False,
    }


def activate_release_group(client: DockerSocketClient, group: dict[str, Any]) -> None:
    roles = group.get("roles") if isinstance(group.get("roles"), list) else []
    for role in roles:
        role_name = str(role.get("role") or "")
        try:
            client.stop_container(str(role.get("old_container_id") or ""), timeout_seconds=10)
            role["old_stopped"] = True
            client.rename_container(
                str(role.get("old_container_id") or ""),
                name=str(role.get("backup_container_name") or ""),
            )
            role["backup_renamed"] = True
            candidate_id = client.create_container(
                role.get("replacement_body") or {},
                name=str(role.get("container_name") or ""),
            )
            role["candidate_container_id"] = candidate_id
            role["candidate_created"] = True
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"{_release_role_label(role_name)} ({role_name}) 切换失败：{_friendly_error_message(exc)}"
            ) from exc
    for role in roles:
        role_name = str(role.get("role") or "")
        try:
            client.start_container(str(role.get("candidate_container_id") or ""))
            role["candidate_started"] = True
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"{_release_role_label(role_name)} ({role_name}) 启动失败：{_friendly_error_message(exc)}"
            ) from exc


def _probe_http_readiness(
    container_name: str,
    *,
    expected_version: str = "",
    timeout_seconds: int = STARTUP_WAIT_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + max(int(timeout_seconds or 0), 1)
    last_error = ""
    while time.monotonic() < deadline:
        connection = None
        try:
            connection = http.client.HTTPConnection(container_name, 8000, timeout=3)
            connection.request("GET", "/api/public/health/ready")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body) if body else {}
            if response.status != 200 or not isinstance(payload, dict) or not payload.get("ready"):
                last_error = f"HTTP {response.status}"
            elif str(payload.get("role") or "") != "app":
                last_error = "role 不匹配"
            elif expected_version and _normalize_version_label(payload.get("version")) != expected_version:
                last_error = "version 不匹配"
            else:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = _friendly_error_message(exc)
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.5)
    raise RuntimeError(f"HTTP readiness 未通过：{last_error or '无响应'}")


def _probe_worker_readiness(
    *,
    expected_start_token: str,
    expected_version: str = "",
    timeout_seconds: int = STARTUP_WAIT_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + max(int(timeout_seconds or 0), 1)
    last_reason = "unknown"
    while time.monotonic() < deadline:
        readiness = worker_heartbeat_readiness(
            expected_start_token=expected_start_token,
            expected_version=expected_version or None,
        )
        if readiness.get("ready"):
            return
        last_reason = str(readiness.get("reason") or "unknown")
        time.sleep(0.5)
    raise RuntimeError(f"Worker heartbeat 未就绪：{last_reason}")


def _verify_release_role(
    client: DockerSocketClient,
    role: dict[str, Any],
    *,
    target_version: str = "",
) -> None:
    candidate_id = str(role.get("candidate_container_id") or "")
    inspect = _wait_for_replacement_container(client, candidate_id)
    _assert_container_name(inspect, str(role.get("container_name") or ""))
    role_name = str(role.get("role") or "")
    if role_name == "worker":
        _probe_worker_readiness(
            expected_start_token=str(role.get("worker_start_token") or ""),
            expected_version=target_version,
            timeout_seconds=_replacement_startup_timeout_seconds(
                inspect,
                fallback_timeout_seconds=STARTUP_WAIT_TIMEOUT_SECONDS,
            ),
        )
        return
    _probe_http_readiness(
        str(role.get("container_name") or ""),
        expected_version=target_version,
    )


def verify_release_group(client: DockerSocketClient, group: dict[str, Any]) -> None:
    target_version = str(group.get("target_version") or "")
    for role in group.get("roles") or []:
        role_name = str(role.get("role") or "")
        try:
            _verify_release_role(client, role, target_version=target_version)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"{_release_role_label(role_name)} ({role_name}) readiness 失败：{_friendly_error_message(exc)}"
            ) from exc


def commit_release_group(_client: DockerSocketClient, group: dict[str, Any]) -> dict[str, Any]:
    group["committed"] = True
    return group


def rollback_release_group(client: DockerSocketClient, group: dict[str, Any]) -> list[str]:
    rollback_errors: list[str] = []
    for role in reversed(group.get("roles") or []):
        role_name = str(role.get("role") or "")
        candidate_id = str(role.get("candidate_container_id") or "")
        if candidate_id:
            try:
                client.remove_container(candidate_id, force=True)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"remove-{role_name}-candidate:{_friendly_error_message(exc)}")
        old_container_id = str(role.get("old_container_id") or "")
        if role.get("backup_renamed"):
            try:
                client.rename_container(old_container_id, name=str(role.get("container_name") or ""))
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"rename-{role_name}-old:{_friendly_error_message(exc)}")
        if role.get("old_stopped"):
            try:
                client.start_container(old_container_id)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"start-{role_name}-old:{_friendly_error_message(exc)}")
    return rollback_errors


def _cleanup_release_group_backups(client: DockerSocketClient, group: dict[str, Any]) -> None:
    for role in group.get("roles") or []:
        if not role.get("backup_renamed"):
            continue
        try:
            client.remove_container(str(role.get("old_container_id") or ""), force=True)
        except Exception as exc:  # noqa: BLE001
            append_business_log(
                "system",
                "self_update_cleanup_warning",
                f"{_release_role_label(str(role.get('role') or ''))} 新容器已就绪，但旧容器清理失败。",
                level="warning",
                request_id=str(group.get("request_id") or ""),
                container_name=str(role.get("container_name") or ""),
                backup_container_name=str(role.get("backup_container_name") or ""),
                error=_friendly_error_message(exc),
            )


def run_release_group_transaction(
    client: DockerSocketClient,
    *,
    request_id: str,
    roles: list[dict[str, str]],
    runtime_config: dict[str, Any] | None = None,
    target_version: str = "",
) -> dict[str, Any]:
    group: dict[str, Any] = {"request_id": str(request_id or ""), "roles": [], "committed": False}
    try:
        group = prepare_release_group(
            client,
            request_id=request_id,
            roles=roles,
            runtime_config=runtime_config,
            target_version=target_version,
        )
        activate_release_group(client, group)
        verify_release_group(client, group)
        return commit_release_group(client, group)
    except Exception as exc:  # noqa: BLE001
        rollback_errors = rollback_release_group(client, group)
        message = _friendly_error_message(exc)
        if rollback_errors:
            message = f"{message}；整组回滚失败：{'；'.join(rollback_errors)}"
        raise RuntimeError(message) from exc


def run_update_helper(
    *,
    request_id: str,
    container_id: str,
    image_ref: str,
    deployment_mode: str = "",
    web_container_name: str = "",
    web_image_ref: str = "",
    worker_container_name: str = "",
    worker_image_ref: str = "",
) -> int:
    client = DockerSocketClient(timeout=600)
    runtime_config = _runtime_config_from_env()
    current_phase = "preparing"
    group: dict[str, Any] | None = None
    try:
        container_inspect = client.inspect_container(container_id)
        container_name = _container_display_name(container_inspect, str(container_id or ""))
        _update_state_from_helper(
            request_id,
            status="running",
            phase="preparing",
            message="正在准备整组发布镜像，服务稍后会短暂重启。",
            started_at=_now_iso(),
            finished_at="",
            container_name=container_name,
            image_ref=image_ref,
            deployment_mode=str(deployment_mode or ""),
            web_container_name=str(web_container_name or ""),
            web_image_ref=str(web_image_ref or ""),
            worker_container_name=str(worker_container_name or ""),
            worker_image_ref=str(worker_image_ref or ""),
            last_error="",
        )
        append_business_log(
            "system",
            "self_update_helper_started",
            "更新 helper 已开始拉取镜像。",
            request_id=request_id,
            container_name=container_name,
            image_ref=image_ref,
            deployment_mode=str(deployment_mode or ""),
            web_container_name=str(web_container_name or ""),
            web_image_ref=str(web_image_ref or ""),
            worker_container_name=str(worker_container_name or ""),
            worker_image_ref=str(worker_image_ref or ""),
        )

        roles: list[dict[str, str]] = []
        if web_container_name:
            roles.append(
                {
                    "role": "web",
                    "container_ref": web_container_name,
                    "image_ref": web_image_ref or image_ref,
                }
            )
        roles.append({"role": "app", "container_ref": container_id, "image_ref": image_ref})
        if worker_container_name:
            roles.append(
                {
                    "role": "worker",
                    "container_ref": worker_container_name,
                    "image_ref": worker_image_ref or image_ref,
                }
            )
        target_version = _normalize_version_label(_read_update_state().get("target_version"))
        current_phase = "preparing"
        group = prepare_release_group(
            client,
            request_id=request_id,
            roles=roles,
            runtime_config=runtime_config,
            target_version=target_version,
        )
        _update_state_from_helper(
            request_id,
            status="running",
            phase="switching_group",
            message="镜像已准备，正在切换整组服务实例。",
        )
        current_phase = "switching_group"
        activate_release_group(client, group)
        replacement_by_role = {str(item.get("role") or ""): item for item in group.get("roles") or []}
        _update_state_from_helper(
            request_id,
            status="running",
            phase="verifying_group",
            message="所有候选容器已启动，正在验证整组就绪状态。",
            replacement_container_id=str((replacement_by_role.get("app") or {}).get("candidate_container_id") or ""),
            web_replacement_container_id=str((replacement_by_role.get("web") or {}).get("candidate_container_id") or ""),
            worker_replacement_container_id=str((replacement_by_role.get("worker") or {}).get("candidate_container_id") or ""),
        )
        current_phase = "verifying_group"
        verify_release_group(client, group)
        current_phase = "committing_group"
        commit_release_group(client, group)
        _update_state_from_helper(
            request_id,
            status="succeeded",
            phase="completed",
            message="整组服务已通过就绪验证。",
            finished_at=_now_iso(),
            last_error="",
        )
        _cleanup_release_group_backups(client, group)
        _schedule_old_update_image_cleanup(request_id)
        return 0
    except Exception as exc:  # noqa: BLE001
        error_message = _friendly_error_message(exc)
        rollback_errors = rollback_release_group(client, group) if group else []
        if rollback_errors:
            error_message = f"{error_message}；整组回滚失败：{'；'.join(rollback_errors)}"
        _update_state_from_helper(
            request_id,
            status="failed",
            phase=current_phase,
            message=error_message,
            finished_at=_now_iso(),
            last_error=error_message,
        )
        append_business_log(
            "system",
            "self_update_failed",
            error_message,
            level="error",
            request_id=request_id,
            container_name=str(locals().get("container_name") or ""),
            image_ref=image_ref,
            phase=current_phase,
            release_group_roles=[str(item.get("role") or "") for item in (group or {}).get("roles") or []],
            rollback_errors=rollback_errors,
            deployment_mode=str(deployment_mode or ""),
            web_container_name=str(web_container_name or ""),
            web_image_ref=str(web_image_ref or ""),
            worker_container_name=str(worker_container_name or ""),
            worker_image_ref=str(worker_image_ref or ""),
        )
        return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="makerhub self update helper")
    parser.add_argument("--run-helper", action="store_true", help="Run inside helper container")
    parser.add_argument("--request-id", default="", help="Current update request id")
    parser.add_argument("--container-id", default="", help="Current makerhub container id")
    parser.add_argument("--image-ref", default="", help="Target image reference")
    parser.add_argument("--deployment-mode", default="", help="Current deployment mode")
    parser.add_argument("--web-container", default="", help="Optional frontend web container name or id")
    parser.add_argument("--web-image-ref", default="", help="Optional frontend web image reference")
    parser.add_argument("--worker-container", default="", help="Optional background worker container name or id")
    parser.add_argument("--worker-image-ref", default="", help="Optional background worker image reference")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.run_helper:
        return 0
    if not args.request_id or not args.container_id or not args.image_ref:
        raise SystemExit("missing --request-id / --container-id / --image-ref")
    return run_update_helper(
        request_id=str(args.request_id or ""),
        container_id=str(args.container_id or ""),
        image_ref=str(args.image_ref or ""),
        deployment_mode=str(args.deployment_mode or ""),
        web_container_name=str(args.web_container or ""),
        web_image_ref=str(args.web_image_ref or ""),
        worker_container_name=str(args.worker_container or ""),
        worker_image_ref=str(args.worker_image_ref or ""),
    )


if __name__ == "__main__":
    raise SystemExit(main())
