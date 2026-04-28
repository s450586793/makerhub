from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.settings import APP_VERSION, STATE_DIR
from app.core.timezone import now_iso as china_now_iso
from app.services.business_logs import append_business_log


DOCKER_SOCKET_PATH = Path("/var/run/docker.sock")
UPDATE_STATE_PATH = STATE_DIR / "system_update.json"
ACTIVE_UPDATE_STATUSES = {"queued", "launching_helper", "running", "pending_startup"}
HELPER_CONTAINER_PREFIX = "makerhub-self-update"
HELPER_LABEL_KEY = "com.makerhub.self_update.role"
HELPER_LABEL_VALUE = "helper"
DEPLOYMENT_MODE_ENV = "MAKERHUB_DEPLOYMENT_MODE"
WEB_CONTAINER_NAME_ENV = "MAKERHUB_WEB_CONTAINER_NAME"
WEB_IMAGE_REF_ENV = "MAKERHUB_WEB_IMAGE_REF"
_CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}")
STARTUP_WAIT_TIMEOUT_SECONDS = 20
STARTUP_WAIT_INTERVAL_SECONDS = 1.0
STARTUP_WAIT_STABLE_POLLS = 3


def _now_iso() -> str:
    return china_now_iso()


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
        "target_version": "",
        "current_version": APP_VERSION,
        "last_error": "",
    }


def _read_update_state() -> dict[str, Any]:
    if not UPDATE_STATE_PATH.exists():
        return _default_update_state()
    try:
        payload = json.loads(UPDATE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_update_state()
    state = _default_update_state()
    if isinstance(payload, dict):
        state.update(payload)
    state["current_version"] = APP_VERSION
    return state


def _write_update_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = _default_update_state()
    state.update(payload)
    state["current_version"] = APP_VERSION
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def _looks_like_container_id(value: str) -> bool:
    return bool(value and _CONTAINER_ID_PATTERN.fullmatch(str(value).strip()))


def _parse_bind_destination(bind_spec: str) -> tuple[str, str, str]:
    parts = str(bind_spec or "").split(":", 2)
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[2]


def _state_mount_spec_from_inspect(container_inspect: dict[str, Any]) -> str:
    state_dir = str(STATE_DIR)
    for bind in container_inspect.get("HostConfig", {}).get("Binds") or []:
        _, destination, _ = _parse_bind_destination(bind)
        if destination == state_dir:
            return str(bind)

    for mount in container_inspect.get("Mounts") or []:
        if str(mount.get("Destination") or "") != state_dir:
            continue
        mount_type = str(mount.get("Type") or "")
        if mount_type == "volume" and mount.get("Name"):
            source = str(mount.get("Name") or "")
        else:
            source = str(mount.get("Source") or "")
        if not source:
            continue
        suffix = ":ro" if mount.get("RW") is False else ""
        return f"{source}:{state_dir}{suffix}"
    return ""


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


def _deployment_mode() -> str:
    value = str(os.getenv(DEPLOYMENT_MODE_ENV, "") or "").strip().lower()
    if value:
        return value
    return "split" if str(os.getenv(WEB_CONTAINER_NAME_ENV, "") or "").strip() else "single"


def _web_update_target(default_image_ref: str = "") -> dict[str, str]:
    container_name = str(os.getenv(WEB_CONTAINER_NAME_ENV, "") or "").strip()
    if not container_name:
        return {"container_name": "", "image_ref": ""}
    image_ref = str(os.getenv(WEB_IMAGE_REF_ENV, "") or "").strip() or str(default_image_ref or "").strip()
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

    def remove_container(self, container_id: str, *, force: bool = False) -> None:
        force_flag = "1" if force else "0"
        self.request(
            "DELETE",
            f"/containers/{quote(container_id, safe='')}" f"?force={force_flag}",
            expected_statuses={204},
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


def _build_replacement_container_body(container_inspect: dict[str, Any], image_ref: str) -> dict[str, Any]:
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

    return body


def _helper_container_name(request_id: str) -> str:
    return f"{HELPER_CONTAINER_PREFIX}-{request_id[:12]}"


def _replacement_container_name(container_name: str, request_id: str) -> str:
    base_name = str(container_name or "makerhub").strip() or "makerhub"
    return f"{base_name}-replacement-{request_id[:8]}"


def _backup_container_name(container_name: str, request_id: str) -> str:
    base_name = str(container_name or "makerhub").strip() or "makerhub"
    return f"{base_name}-backup-{request_id[:8]}"


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

        return {
            "container_id": str(inspect.get("Id") or candidate),
            "container_name": str(inspect.get("Name") or "").lstrip("/"),
            "image_ref": str((inspect.get("Config") or {}).get("Image") or ""),
            "container_image_id": str(inspect.get("Image") or ""),
            "state_mount": state_mount,
            "inspect": inspect,
        }

    raise RuntimeError(last_error or "无法识别当前容器实例。")


def get_update_capability() -> dict[str, Any]:
    payload = {
        "supported": False,
        "support_reason": "",
        "docker_socket_mounted": DOCKER_SOCKET_PATH.exists(),
        "container_name": "",
        "image_ref": "",
        "deployment_mode": _deployment_mode(),
        "web_container_name": "",
        "web_image_ref": "",
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

    web_target = _web_update_target(str(metadata.get("image_ref") or ""))
    if web_target.get("container_name"):
        try:
            web_inspect = client.inspect_container(str(web_target.get("container_name") or ""))
        except Exception as exc:  # noqa: BLE001
            payload["support_reason"] = f"Web 容器无法访问：{_friendly_error_message(exc)}"
            return payload
        payload["web_container_name"] = str(web_inspect.get("Name") or web_target.get("container_name") or "").lstrip("/")
        payload["web_image_ref"] = str(web_target.get("image_ref") or "")

    payload.update(
        {
            "supported": True,
            "support_reason": "",
            "container_name": str(metadata.get("container_name") or ""),
            "image_ref": str(metadata.get("image_ref") or ""),
            "deployment_mode": _deployment_mode(),
        }
    )
    return payload


def get_update_status() -> dict[str, Any]:
    state = _read_update_state()
    capability = get_update_capability()
    return {
        **state,
        "supported": bool(capability.get("supported")),
        "support_reason": str(capability.get("support_reason") or ""),
        "docker_socket_mounted": bool(capability.get("docker_socket_mounted")),
        "container_name": str(capability.get("container_name") or state.get("container_name") or ""),
        "image_ref": str(capability.get("image_ref") or state.get("image_ref") or ""),
        "deployment_mode": str(capability.get("deployment_mode") or state.get("deployment_mode") or ""),
        "web_container_name": str(capability.get("web_container_name") or state.get("web_container_name") or ""),
        "web_image_ref": str(capability.get("web_image_ref") or state.get("web_image_ref") or ""),
    }


def request_system_update(*, requested_by: str = "", target_version: str = "", force: bool = False) -> dict[str, Any]:
    state = _read_update_state()
    if str(state.get("status") or "") in ACTIVE_UPDATE_STATUSES:
        raise RuntimeError("已有系统更新任务在执行中，请稍后再试。")

    if not DOCKER_SOCKET_PATH.exists():
        raise RuntimeError("当前容器没有挂载 /var/run/docker.sock，无法执行网页一键更新。")

    client = DockerSocketClient(timeout=30)
    metadata = _resolve_self_container(client)

    request_id = uuid.uuid4().hex
    helper_name = _helper_container_name(request_id)
    requested_at = _now_iso()
    target_image = str(metadata.get("image_ref") or "")
    helper_image = str(metadata.get("container_image_id") or target_image)
    deployment_mode = _deployment_mode()
    web_target = _web_update_target(target_image)
    if not target_image:
        raise RuntimeError("当前容器缺少镜像引用，无法确定要拉取的目标镜像。")
    if not helper_image:
        raise RuntimeError("当前容器缺少本地镜像 ID，无法启动更新 helper。")
    if web_target.get("container_name"):
        try:
            client.inspect_container(str(web_target.get("container_name") or ""))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Web 容器无法访问：{_friendly_error_message(exc)}") from exc
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
    helper_body = {
        "Image": helper_image,
        "Cmd": helper_cmd,
        "Env": [
            f"MAKERHUB_STATE_DIR={STATE_DIR}",
            "PYTHONUNBUFFERED=1",
        ],
        "Labels": {
            HELPER_LABEL_KEY: HELPER_LABEL_VALUE,
            "com.makerhub.self_update.request_id": request_id,
        },
        "HostConfig": {
            "AutoRemove": True,
            "Binds": [
                f"{DOCKER_SOCKET_PATH}:{DOCKER_SOCKET_PATH}",
                str(metadata.get("state_mount") or ""),
            ],
            "NetworkMode": "none",
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
            "target_version": str(target_version or ""),
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
        target_version=str(target_version or ""),
        force=bool(force),
    )
    return get_update_status()


def mark_update_started_after_restart() -> None:
    state = _read_update_state()
    if str(state.get("status") or "") != "pending_startup":
        return
    finished_at = _now_iso()
    _write_update_state(
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
    deadline = time.monotonic() + max(int(timeout_seconds or 0), 1)
    stable_count = 0
    last_inspect: dict[str, Any] = {}

    while time.monotonic() < deadline:
        inspect = client.inspect_container(container_id)
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

    logs = _container_startup_logs(client, container_id) if last_inspect else "（未读取到容器状态）"
    raise RuntimeError(
        f"等待新容器恢复超时（{_container_state_summary(last_inspect) if last_inspect else 'no-state'}）。"
        f" 启动日志：{logs}"
    )


def _replace_related_container(
    client: DockerSocketClient,
    *,
    request_id: str,
    container_ref: str,
    image_ref: str,
    role: str,
) -> dict[str, str]:
    container_inspect = client.inspect_container(container_ref)
    old_container_id = str(container_inspect.get("Id") or container_ref)
    container_name = str(container_inspect.get("Name") or "").lstrip("/") or str(container_ref or "")
    replacement_container_id = ""
    replacement_container_name = _replacement_container_name(container_name, request_id)
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
        client.pull_image(image_ref)
        replacement_body = _build_replacement_container_body(container_inspect, image_ref)
        replacement_container_id = client.create_container(replacement_body, name=replacement_container_name)
        _update_state_from_helper(
            request_id,
            web_replacement_container_id=replacement_container_id if role == "web" else "",
        )
        client.stop_container(old_container_id, timeout_seconds=10)
        client.rename_container(old_container_id, name=backup_container_name)
        old_container_renamed = True
        client.rename_container(replacement_container_id, name=container_name)
        replacement_container_renamed = True
        client.start_container(replacement_container_id)
        _wait_for_replacement_container(client, replacement_container_id)
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
            "replacement_container_name": replacement_container_name if not replacement_container_renamed else container_name,
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


def run_update_helper(
    *,
    request_id: str,
    container_id: str,
    image_ref: str,
    deployment_mode: str = "",
    web_container_name: str = "",
    web_image_ref: str = "",
) -> int:
    current_phase = "pulling"
    client = DockerSocketClient(timeout=600)
    replacement_container_id = ""
    replacement_container_name = ""
    backup_container_name = ""
    old_container_renamed = False
    replacement_container_renamed = False

    def _attempt_rollback() -> None:
        rollback_errors: list[str] = []

        if replacement_container_id:
            try:
                client.remove_container(replacement_container_id, force=True)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"remove-new:{_friendly_error_message(exc)}")

        if old_container_renamed:
            try:
                client.rename_container(container_id, name=container_name)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"rename-old:{_friendly_error_message(exc)}")
            else:
                try:
                    client.start_container(container_id)
                except Exception as exc:  # noqa: BLE001
                    rollback_errors.append(f"start-old:{_friendly_error_message(exc)}")
        else:
            try:
                client.start_container(container_id)
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"restart-old:{_friendly_error_message(exc)}")

        if rollback_errors:
            append_business_log(
                "system",
                "self_update_rollback_failed",
                "系统更新失败，且自动回滚未完全成功。",
                level="error",
                request_id=request_id,
                errors=rollback_errors,
                container_name=container_name,
                backup_container_name=backup_container_name,
                replacement_container_id=replacement_container_id,
                replacement_container_name=replacement_container_name,
                replacement_container_renamed=replacement_container_renamed,
            )
            raise RuntimeError("；".join(rollback_errors))

    try:
        container_inspect = client.inspect_container(container_id)
        container_name = str(container_inspect.get("Name") or "").lstrip("/")
        _update_state_from_helper(
            request_id,
            status="running",
            phase="pulling",
            message="正在拉取最新镜像，服务稍后会短暂重启。",
            started_at=_now_iso(),
            finished_at="",
            container_name=container_name,
            image_ref=image_ref,
            deployment_mode=str(deployment_mode or ""),
            web_container_name=str(web_container_name or ""),
            web_image_ref=str(web_image_ref or ""),
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
        )

        client.pull_image(image_ref)
        if web_container_name:
            current_phase = "updating_web"
            _update_state_from_helper(
                request_id,
                status="running",
                phase="updating_web",
                message="API 镜像已拉取完成，正在更新 Web 前端容器。",
            )
            web_result = _replace_related_container(
                client,
                request_id=request_id,
                container_ref=web_container_name,
                image_ref=web_image_ref or image_ref,
                role="web",
            )
            _update_state_from_helper(
                request_id,
                status="running",
                phase="web_updated",
                message="Web 前端容器已更新，正在替换 API 容器。",
                web_container_name=str(web_result.get("container_name") or web_container_name),
                web_replacement_container_id=str(web_result.get("replacement_container_id") or ""),
            )
            append_business_log(
                "system",
                "self_update_web_updated",
                "Web 前端容器已更新。",
                request_id=request_id,
                container_name=str(web_result.get("container_name") or web_container_name),
                image_ref=web_image_ref or image_ref,
                replacement_container_id=str(web_result.get("replacement_container_id") or ""),
            )
        current_phase = "recreating"
        _update_state_from_helper(
            request_id,
            status="running",
            phase="recreating",
            message="镜像已拉取完成，正在替换当前容器。",
        )

        replacement_body = _build_replacement_container_body(container_inspect, image_ref)
        replacement_container_name = _replacement_container_name(container_name, request_id)
        backup_container_name = _backup_container_name(container_name, request_id)
        replacement_container_id = client.create_container(replacement_body, name=replacement_container_name)

        current_phase = "switching"
        _update_state_from_helper(
            request_id,
            status="running",
            phase="switching",
            message="新容器已准备，正在切换服务实例。",
            replacement_container_id=replacement_container_id,
        )
        client.stop_container(container_id, timeout_seconds=10)
        client.rename_container(container_id, name=backup_container_name)
        old_container_renamed = True
        client.rename_container(replacement_container_id, name=container_name)
        replacement_container_renamed = True

        current_phase = "starting"
        _update_state_from_helper(
            request_id,
            status="pending_startup",
            phase="starting",
            message="新容器已创建，正在等待应用恢复。",
            replacement_container_id=replacement_container_id,
        )
        client.start_container(replacement_container_id)
        _wait_for_replacement_container(client, replacement_container_id)

        try:
            client.remove_container(container_id, force=True)
        except Exception as exc:  # noqa: BLE001
            append_business_log(
                "system",
                "self_update_cleanup_warning",
                "新容器已启动，但旧容器清理失败。",
                level="warning",
                request_id=request_id,
                container_name=container_name,
                backup_container_name=backup_container_name,
                error=_friendly_error_message(exc),
            )
        return 0
    except Exception as exc:  # noqa: BLE001
        error_message = _friendly_error_message(exc)
        rollback_error = ""
        if replacement_container_id or old_container_renamed:
            try:
                _attempt_rollback()
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_error = _friendly_error_message(rollback_exc)
                error_message = f"{error_message}；自动回滚失败：{rollback_error}"
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
            replacement_container_id=replacement_container_id,
            replacement_container_name=replacement_container_name,
            backup_container_name=backup_container_name,
            rollback_error=rollback_error,
            deployment_mode=str(deployment_mode or ""),
            web_container_name=str(web_container_name or ""),
            web_image_ref=str(web_image_ref or ""),
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
    )


if __name__ == "__main__":
    raise SystemExit(main())
