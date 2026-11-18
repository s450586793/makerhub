from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "makerhub_monitor.py"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def stat_with(info, **changes):
    # 保留完整 stat_result，避免权限模拟破坏文件读取、pytest 捕获及临时目录管理。
    fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
    fields.update(changes)
    names = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid",
             "st_gid", "st_size", "st_atime", "st_mtime", "st_ctime")
    return os.stat_result([fields[name] for name in names], fields)


@pytest.fixture
def monitor():
    assert SCRIPT.exists(), "The scoped MakerHub monitor has not been implemented"
    spec = importlib.util.spec_from_file_location("makerhub_monitor_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(tmp_path, monkeypatch, monitor):
    # 模拟仅主机权限；所有业务文件、目录及内容核验仍使用真实临时文件。
    monkeypatch.setattr(monitor, "os", SimpleNamespace(**vars(os)))
    original_fstat = os.fstat

    def root_fstat(fd):
        value = original_fstat(fd)
        return stat_with(value, st_uid=0)

    monkeypatch.setattr(monitor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(monitor.os, "fstat", root_fstat)
    root = tmp_path / "makerhub"
    root.mkdir()
    (root / "compose.yaml").write_bytes(b"approved-compose")
    (root / ".env").write_bytes(b"private-config")
    legacy_path = tmp_path / "legacy.py"
    legacy_path.write_text(
        "import hashlib\nimport json\nfrom pathlib import Path\n"
        f"ROOT = Path({str(root)!r})\n"
        "NAMES = ('makerhub-app', 'makerhub-worker')\n"
        "class StopRequested(BaseException): pass\n"
        "def authority(expected):\n"
        "    return {'container_ids': {'makerhub-app': 'a'*64, 'makerhub-worker': 'b'*64},\n"
        "        'compose_sha256': hashlib.sha256(b'approved-compose').hexdigest(),\n"
        "        'env_sha256': hashlib.sha256(b'private-config').hexdigest()}\n"
        "def runtime_sha256(row): return row['runtime_sha256']\n"
        "def secure_bytes(path, require_root=False): return path.read_bytes()\n"
        "def exact_rows(docker, document): return {}\n"
        "def command(arguments):\n"
        "    if arguments[0] == 'docker': return b'/vol2/docker\\n'\n"
        "    return json.dumps({'filesystems': [{'uuid': 'stable-uuid', 'fstype': 'btrfs', 'fsroot': '/makerhub', 'options': 'rw,relatime'}]}).encode()\n"
        "def frozen_gate(document): raise AssertionError('Legacy global gate must not run')\n"
        "def stop(expected, supervisor_pid=None): raise AssertionError('Unexpected stop')\n"
        "def supervise(expected): raise AssertionError('Unexpected supervise')\n"
        "class DockerCLI: pass\n"
    )
    legacy_path.chmod(0o600)
    identities = []
    for path in (root, root / "postgres", root / "browser", tmp_path / "library"):
        path.mkdir(exist_ok=True)
        info = path.stat()
        identities.append({"path": str(path), "dev": info.st_dev, "ino": info.st_ino, "kind": "directory",
                           "filesystem": {"uuid": "stable-uuid", "fstype": "btrfs", "fsroot": "/makerhub"}})
    names = ("makerhub-postgres", "makerhub-cloakbrowser")
    dependencies = {name: {"id": str(i + 1) * 64, "image_id": "sha256:" + str(i + 3) * 64,
                          "runtime_sha256": str(i + 5) * 64} for i, name in enumerate(names)}
    config = {"schema": 1, "legacy_script": str(legacy_path), "legacy_sha256": digest(legacy_path.read_bytes()),
              "intent_sha256": "c" * 64, "monitor_sha256": digest(SCRIPT.read_bytes()),
              "unit": "makerhub-scoped-test.service", "root_identity": {key: identities[0][key] for key in ("path", "dev", "ino", "filesystem")},
              "bind_identities": identities, "dependencies": dependencies,
              "host_dmi_sha256": digest(b"approved-host\n"), "docker_root_dir": "/vol2/docker"}
    config_path = tmp_path / "monitor.json"

    def save():
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        config_path.chmod(0o600)

    save()
    monkeypatch.setattr(monitor, "host_dmi_bytes", lambda: b"approved-host\n")
    context = monitor.load_context(config_path)
    rows = {item["id"]: {"Id": item["id"], "Name": "/" + name, "Image": item["image_id"],
                        "runtime_sha256": item["runtime_sha256"],
                        "State": {"Running": True, "Health": {"Status": "healthy"}}}
            for name, item in dependencies.items()}
    docker = SimpleNamespace(inspect=Mock(side_effect=lambda identifier: copy.deepcopy(rows[identifier])))
    context.legacy.DockerCLI = lambda: docker
    context.legacy.stop = Mock()
    return SimpleNamespace(context=context, config=config, save=save, rows=rows, docker=docker,
                           path=config_path, root=root, tmp=tmp_path)


def test_runtime_permission_mock_preserves_file_metadata(monitor, runtime):
    assert monitor.os is not os
    with runtime.path.open("rb") as source:
        real = os.fstat(source.fileno())
        mocked = monitor.os.fstat(source.fileno())
        assert isinstance(mocked, os.stat_result)
        assert mocked.st_uid == 0
        assert mocked.st_size == real.st_size
        assert mocked.st_mtime_ns == real.st_mtime_ns
        assert tuple(mocked)[4] == 0
        assert source.read() == runtime.path.read_bytes()


def test_unrelated_directory_and_container_changes_do_not_stop_makerhub(monitor, runtime):
    monitor.scoped_gate(runtime.context)
    unrelated = runtime.tmp / "printlab-staging"
    unrelated.mkdir()
    (unrelated / "new-file").write_text("unrelated changes")
    runtime.rows["unrelated-container"] = {"Name": "/unrelated", "State": {"Running": False}}
    monitor.scoped_gate(runtime.context)
    (unrelated / "new-file").unlink()
    unrelated.rmdir()
    del runtime.rows["unrelated-container"]
    monitor.scoped_gate(runtime.context)
    assert {call.args[0] for call in runtime.docker.inspect.call_args_list} == {
        value["id"] for value in runtime.config["dependencies"].values()
    }
    runtime.context.legacy.stop.assert_not_called()


@pytest.mark.parametrize("filename", ["compose.yaml", ".env"])
def test_own_configuration_change_rejects_gate(monitor, runtime, filename):
    (runtime.root / filename).write_bytes(b"unapproved")
    with pytest.raises(RuntimeError, match="configuration"):
        monitor.scoped_gate(runtime.context)


def test_own_root_replacement_rejects_gate(monitor, runtime):
    runtime.root.rename(runtime.tmp / "previous-root")
    runtime.root.mkdir()
    with pytest.raises(RuntimeError, match="identity"):
        monitor.scoped_gate(runtime.context)


def test_own_bind_replacement_rejects_gate(monitor, runtime):
    path = runtime.root / "postgres"
    path.rename(runtime.root / "previous-postgres")
    path.mkdir()
    with pytest.raises(RuntimeError, match="identity"):
        monitor.scoped_gate(runtime.context)


@pytest.mark.parametrize("field,value", [("Id", "f" * 64), ("Name", "/replacement"),
                                         ("Image", "sha256:" + "f" * 64), ("runtime_sha256", "f" * 64),
                                         ("State", {"Running": True, "Health": {"Status": "unhealthy"}})])
def test_dependency_runtime_or_health_change_rejects_gate(monitor, runtime, field, value):
    row = next(iter(runtime.rows.values()))
    row[field] = value
    with pytest.raises(RuntimeError, match="dependency"):
        monitor.scoped_gate(runtime.context)


def test_host_change_rejects_gate(monitor, runtime, monkeypatch):
    monkeypatch.setattr(monitor, "host_dmi_bytes", lambda: b"another-host\n")
    with pytest.raises(RuntimeError, match="host"):
        monitor.scoped_gate(runtime.context)


def test_docker_root_change_rejects_gate(monitor, runtime):
    runtime.context.legacy.command = lambda arguments: b"/another/docker\n"
    with pytest.raises(RuntimeError, match="Docker root"):
        monitor.scoped_gate(runtime.context)


def test_reboot_device_number_change_keeps_stable_filesystem_identity(monitor, runtime, monkeypatch):
    original_stat = Path.stat

    def changed_device(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        return stat_with(info, st_dev=info.st_dev + 100)

    monkeypatch.setattr(Path, "stat", changed_device)
    monitor.scoped_gate(runtime.context)


@pytest.mark.parametrize("field,value", [("uuid", "another-uuid"), ("fstype", "ext4"),
                                         ("fsroot", "/another-subvolume"), ("options", "ro,relatime")])
def test_filesystem_replacement_or_readonly_mount_rejects_gate(monitor, runtime, field, value):
    original_command = runtime.context.legacy.command

    def command(arguments):
        result = original_command(arguments)
        if arguments[0] == "findmnt":
            payload = json.loads(result)
            payload["filesystems"][0][field] = value
            return json.dumps(payload).encode()
        return result

    runtime.context.legacy.command = command
    with pytest.raises(RuntimeError, match="filesystem identity or write mode"):
        monitor.scoped_gate(runtime.context)


def test_monitor_configuration_change_rejects_gate(monitor, runtime):
    runtime.config["unit"] = "other.service"
    runtime.save()
    with pytest.raises(RuntimeError, match="Monitor configuration"):
        monitor.scoped_gate(runtime.context)


@pytest.mark.parametrize("field", ["monitor_sha256", "legacy_sha256"])
def test_source_hash_mismatch_rejects_loading(monitor, runtime, field):
    runtime.config[field] = "f" * 64
    runtime.save()
    with pytest.raises(RuntimeError, match="source"):
        monitor.load_context(runtime.path)


def test_unsafe_config_permissions_and_symlink_rejected(monitor, runtime):
    runtime.path.chmod(0o644)
    with pytest.raises(RuntimeError, match="Unsafe"):
        monitor.load_context(runtime.path)
    runtime.path.chmod(0o600)
    link = runtime.tmp / "config-link.json"
    link.symlink_to(runtime.path)
    with pytest.raises(RuntimeError, match="symlink"):
        monitor.load_context(link)


def test_prepare_runtime_is_read_only_and_replaces_only_frozen_gate(monitor, runtime, monkeypatch):
    context = runtime.context
    monkeypatch.setattr(monitor, "load_context", lambda path: context)
    exact_rows = Mock()
    context.legacy.exact_rows = exact_rows
    assert monitor.prepare_runtime(runtime.path) is context
    exact_rows.assert_called_once_with(runtime.docker, context.document)
    context.legacy.stop.assert_not_called()


def test_run_reuses_original_supervise_with_scoped_gate(monitor, runtime, monkeypatch):
    context = runtime.context
    monkeypatch.setattr(monitor, "load_context", lambda path: context)
    context.legacy.supervise = Mock(side_effect=lambda expected: context.legacy.frozen_gate(context.document))
    monitor.run(runtime.path)
    context.legacy.supervise.assert_called_once_with(runtime.config["intent_sha256"])


@pytest.fixture
def process(monitor, runtime, monkeypatch):
    proc = runtime.tmp / "proc" / "1234"
    proc.mkdir(parents=True)
    command = [b"/usr/bin/python3", b"-B", str(SCRIPT).encode(), b"run", b"--config", str(runtime.path).encode()]
    (proc / "cmdline").write_bytes(b"\0".join(command) + b"\0")
    (proc / "cgroup").write_text("0::/system.slice/makerhub-scoped-test.service\n")
    monkeypatch.setattr(monitor, "PROC_ROOT", runtime.tmp / "proc")
    original_stat = Path.stat

    def root_stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        return stat_with(info, st_uid=0) if path == proc else info

    monkeypatch.setattr(Path, "stat", root_stat)
    monkeypatch.setattr(monitor.os, "pidfd_open", Mock(return_value=777), raising=False)
    monkeypatch.setattr(monitor.os, "close", Mock())
    monkeypatch.setattr(monitor, "signal", SimpleNamespace(**vars(signal)))
    signal_send = Mock()
    monkeypatch.setattr(monitor.signal, "pidfd_send_signal", signal_send, raising=False)
    monkeypatch.setattr(monitor, "load_context", lambda path: runtime.context)
    return SimpleNamespace(path=proc, signal_send=signal_send)


@pytest.mark.parametrize("file,value", [("cmdline", b"/usr/bin/python3\0other.py\0"),
                                        ("cgroup", b"0::/system.slice/makerhub-scoped-test.service-extra\n")])
def test_stop_rejects_wrong_pid_without_signal_or_container_stop(monitor, runtime, process, file, value):
    (process.path / file).write_bytes(value)
    with pytest.raises(RuntimeError, match="process identity"):
        monitor.stop(runtime.path, 1234)
    process.signal_send.assert_not_called()
    runtime.context.legacy.stop.assert_not_called()


def test_stop_signals_verified_monitor_then_reuses_exact_legacy_stop(monitor, runtime, process):
    monitor.stop(runtime.path, 1234)
    process.signal_send.assert_called_once_with(777, signal.SIGUSR1)
    runtime.context.legacy.stop.assert_called_once_with(runtime.config["intent_sha256"], None)


def test_stop_invalid_pid_does_not_stop_containers(monitor, runtime, process):
    with pytest.raises(RuntimeError, match="PID"):
        monitor.stop(runtime.path, 0)
    process.signal_send.assert_not_called()
    runtime.context.legacy.stop.assert_not_called()


def test_stop_ignores_dependency_health_and_configuration_gate(monitor, runtime, process):
    next(iter(runtime.rows.values()))["State"]["Health"]["Status"] = "unhealthy"
    (runtime.root / ".env").write_bytes(b"changed")
    monitor.stop(runtime.path, 1234)
    runtime.docker.inspect.assert_not_called()
    runtime.context.legacy.stop.assert_called_once_with(runtime.config["intent_sha256"], None)


def test_stop_context_can_load_when_own_bind_gate_would_reject_symlink(monitor, runtime):
    path = runtime.root / "postgres"
    path.rename(runtime.root / "old-postgres")
    path.symlink_to(runtime.root / "old-postgres", target_is_directory=True)
    context = monitor.load_context(runtime.path)
    with pytest.raises(RuntimeError, match="symlink"):
        monitor.scoped_gate(context)


def test_stop_runtime_identity_failure_never_signals_or_stops(monitor, runtime, process):
    runtime.context.legacy.exact_rows = Mock(side_effect=RuntimeError("Exact release runtime differs"))
    with pytest.raises(RuntimeError, match="runtime differs"):
        monitor.stop(runtime.path, 1234)
    process.signal_send.assert_not_called()
    runtime.context.legacy.stop.assert_not_called()


def test_stop_exited_process_does_not_signal_and_still_stops_exact_pair(monitor, runtime, process, monkeypatch):
    monkeypatch.setattr(monitor.os, "pidfd_open", Mock(side_effect=ProcessLookupError))
    monitor.stop(runtime.path, 1234)
    process.signal_send.assert_not_called()
    runtime.context.legacy.stop.assert_called_once_with(runtime.config["intent_sha256"], None)


@pytest.mark.parametrize("kind", ["legacy", "monitor"])
def test_gate_rechecks_source_bytes_after_load(monitor, runtime, monkeypatch, kind):
    if kind == "legacy":
        path = Path(runtime.config["legacy_script"])
    else:
        path = runtime.tmp / "monitor-copy.py"
        path.write_bytes(SCRIPT.read_bytes())
        path.chmod(0o600)
        monkeypatch.setattr(monitor, "__file__", str(path))
    path.write_bytes(path.read_bytes() + b"\n# unapproved edit\n")
    with pytest.raises(RuntimeError, match="source differs"):
        monitor.scoped_gate(runtime.context)


def test_run_handles_original_clean_exit(monitor, runtime, monkeypatch):
    monkeypatch.setattr(monitor, "load_context", lambda path: runtime.context)
    runtime.context.legacy.supervise = Mock(side_effect=runtime.context.legacy.StopRequested)
    monitor.run(runtime.path)


def test_check_cli_reports_only_whitelisted_success_fields(monitor, runtime, monkeypatch, capsys):
    monkeypatch.setattr(monitor, "prepare_runtime", lambda path: runtime.context)
    assert monitor.main(["check", "--config", str(runtime.path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "scope": "makerhub", "unit": runtime.config["unit"],
                       "container_ids": runtime.context.document["container_ids"]}


def test_cli_sanitizes_unexpected_exception(monitor, monkeypatch, capsys):
    monkeypatch.setattr(monitor, "prepare_runtime", Mock(side_effect=ValueError("private-config-secret")))
    assert monitor.main(["check", "--config", "/monitor.json"]) == 1
    output = capsys.readouterr().out
    assert "private-config-secret" not in output
    assert json.loads(output)["message"] == "Private monitor operation failed"
