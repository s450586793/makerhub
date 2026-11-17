#!/usr/bin/env python3
"""以 MakerHub 自身边界监督冻结部署，不依赖其他应用的目录或容器清单。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
from types import ModuleType, SimpleNamespace


PROC_ROOT = Path("/proc")
DEPENDENCY_NAMES = frozenset(("makerhub-postgres", "makerhub-cloakbrowser"))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def absolute_path(value):
    require(isinstance(value, (str, Path)), "Absolute canonical path required")
    path = Path(value)
    require(path.is_absolute() and str(path) == str(value) and ".." not in path.parts,
            "Absolute canonical path required")
    return path


def canonical_path(value):
    path = absolute_path(value)
    require(path.resolve() == path,
            "Absolute canonical path without symlinks required")
    for item in (path, *path.parents):
        require(not item.is_symlink(), "Path contains a symlink")
    return path


def secure_bytes(path, mode=None):
    path = canonical_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and not info.st_mode & 0o022
                and (mode is None or stat.S_IMODE(info.st_mode) == mode),
                "Unsafe monitor authority file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def host_dmi_bytes():
    # sysfs 中 class/dmi/id 本身是内核维护的链接，只读取固定的主机标识。
    return Path("/sys/class/dmi/id/product_uuid").read_bytes()


def validate_config(config):
    require(isinstance(config, dict) and config.get("schema") == 1, "Invalid monitor schema")
    for key in ("legacy_sha256", "intent_sha256", "monitor_sha256", "host_dmi_sha256"):
        require(isinstance(config.get(key), str) and re.fullmatch("[a-f0-9]{64}", config[key]),
                "Invalid monitor source or authority digest")
    canonical_path(config.get("legacy_script"))
    require(isinstance(config.get("unit"), str)
            and re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", config["unit"]), "Invalid monitor unit")
    absolute_path(config.get("docker_root_dir"))
    root = config.get("root_identity")
    binds = config.get("bind_identities")
    require(isinstance(root, dict) and isinstance(binds, list) and len(binds) == 4,
            "Exact MakerHub root and four bind identities required")
    for identity in [root, *binds]:
        require(isinstance(identity, dict) and type(identity.get("dev")) is int
                and type(identity.get("ino")) is int and identity["dev"] >= 0 and identity["ino"] > 0,
                "Invalid MakerHub path identity")
        absolute_path(identity.get("path"))
        filesystem = identity.get("filesystem")
        require(isinstance(filesystem, dict)
                and all(isinstance(filesystem.get(key), str) and filesystem[key]
                        for key in ("uuid", "fstype", "fsroot")), "Stable MakerHub filesystem identity required")
    require(all(item.get("kind") == "directory" for item in binds)
            and len({item["path"] for item in binds}) == 4
            and any(all(item[key] == root[key] for key in ("path", "ino", "filesystem")) for item in binds),
            "Exact MakerHub bind identities required")
    dependencies = config.get("dependencies")
    require(isinstance(dependencies, dict) and set(dependencies) == DEPENDENCY_NAMES,
            "Exact MakerHub dependencies required")
    for item in dependencies.values():
        require(isinstance(item, dict)
                and isinstance(item.get("image_id"), str)
                and re.fullmatch("sha256:[a-f0-9]{64}", item["image_id"]), "Invalid dependency image")
        for field in ("id", "runtime_sha256"):
            require(isinstance(item.get(field), str) and re.fullmatch("[a-f0-9]{64}", item[field]),
                    "Invalid dependency identity")
    require(len({item["id"] for item in dependencies.values()}) == 2,
            "Dependency identities must be distinct")


def load_context(config_path):
    require(os.geteuid() == 0, "Monitor requires root")
    config_path = canonical_path(config_path)
    raw = secure_bytes(config_path, 0o600)
    config = json.loads(raw)
    validate_config(config)
    require(sha(secure_bytes(Path(__file__))) == config["monitor_sha256"], "Monitor source differs")
    legacy_raw = secure_bytes(config["legacy_script"])
    require(sha(legacy_raw) == config["legacy_sha256"], "Legacy source differs")
    # 执行已校验的同一份字节，避免导入器重新读取文件或使用缓存的 pyc。
    legacy = ModuleType("makerhub_frozen_runtime")
    legacy.__file__ = config["legacy_script"]
    exec(compile(legacy_raw, config["legacy_script"], "exec"), legacy.__dict__)
    require(str(legacy.ROOT) == config["root_identity"]["path"], "Legacy MakerHub root differs")
    document = legacy.authority(config["intent_sha256"])
    context = SimpleNamespace(config_path=config_path, config_raw=raw, config=config,
                              legacy=legacy, document=document)
    # 唯一有意替换的是运行时门禁；冻结脚本及 intent 字节和其余安全检查保持原样。
    legacy.frozen_gate = lambda current: scoped_gate(context, current)
    return context


def path_identity(identity, command):
    path = canonical_path(identity["path"])
    info = path.stat()
    # Btrfs 的匿名 st_dev 可在合法重启后改变，持久身份使用文件系统 UUID / 子卷根和 inode。
    require(stat.S_ISDIR(info.st_mode) and info.st_ino == identity["ino"],
            "MakerHub path identity differs")
    value = json.loads(command(["findmnt", "--json", "--target", str(path),
                                "--output", "UUID,FSTYPE,FSROOT,OPTIONS"]))
    rows = value.get("filesystems", [])
    require(len(rows) == 1 and isinstance(rows[0], dict), "Exact MakerHub filesystem required")
    row = rows[0]
    options = (row.get("options") or "").split(",")
    require(all(row.get(key) == expected for key, expected in identity["filesystem"].items())
            and "rw" in options and "ro" not in options, "MakerHub filesystem identity or write mode differs")


def scoped_gate(context, document=None):
    config = context.config
    document = context.document if document is None else document
    require(document == context.document, "MakerHub release authority changed")
    require(secure_bytes(context.config_path, 0o600) == context.config_raw, "Monitor configuration changed")
    require(sha(secure_bytes(Path(__file__))) == config["monitor_sha256"], "Monitor source differs")
    require(sha(secure_bytes(config["legacy_script"])) == config["legacy_sha256"], "Legacy source differs")
    require(sha(host_dmi_bytes()) == config["host_dmi_sha256"], "Monitor host identity differs")
    docker_root = context.legacy.command(["docker", "info", "--format", "{{.DockerRootDir}}"])
    require(docker_root.decode().strip() == config["docker_root_dir"], "Docker root differs")
    for identity in config["bind_identities"]:
        path_identity(identity, context.legacy.command)
    root = Path(config["root_identity"]["path"])
    require(sha(context.legacy.secure_bytes(root / "compose.yaml", require_root=False)) == document["compose_sha256"]
            and sha(context.legacy.secure_bytes(root / ".env", require_root=False)) == document["env_sha256"],
            "Reviewed MakerHub configuration changed")
    docker = context.legacy.DockerCLI()
    # 仅按两个明确的完整 ID 查询，绝不列举 Docker 全局命名空间。
    for name, expected in config["dependencies"].items():
        row = docker.inspect(expected["id"])
        require(row["Id"] == expected["id"] and row["Name"] == "/" + name
                and row["Image"] == expected["image_id"]
                and context.legacy.runtime_sha256(row) == expected["runtime_sha256"]
                and row["State"]["Running"] and row["State"].get("Health", {}).get("Status") == "healthy",
                "MakerHub dependency identity or health differs")


def prepare_runtime(config_path):
    context = load_context(config_path)
    scoped_gate(context)
    context.legacy.exact_rows(context.legacy.DockerCLI(), context.document)
    return context


def run(config_path):
    context = load_context(config_path)
    try:
        context.legacy.supervise(context.config["intent_sha256"])
    except context.legacy.StopRequested:
        pass


def drain_monitor(context, pid):
    require(type(pid) is int and pid > 1 and pid != os.getpid(), "Invalid monitor PID")
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        proc = PROC_ROOT / str(pid)
        command = [b"/usr/bin/python3", b"-B", str(Path(__file__)).encode(), b"run",
                   b"--config", str(context.config_path).encode()]
        groups = [line.split(":", 2)[2] for line in proc.joinpath("cgroup").read_text().splitlines()
                  if len(line.split(":", 2)) == 3]
        require(proc.stat().st_uid == 0
                and proc.joinpath("cmdline").read_bytes() == b"\0".join(command) + b"\0"
                and "/system.slice/" + context.config["unit"] in groups,
                "Monitor process identity differs")
        signal.pidfd_send_signal(descriptor, signal.SIGUSR1)
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)


def stop(config_path, supervisor_pid):
    # 不调用健康门禁；依赖故障或挂载变化时仍能停止精确所属的 App / Worker。
    context = load_context(config_path)
    context.legacy.exact_rows(context.legacy.DockerCLI(), context.document)
    drain_monitor(context, supervisor_pid)
    context.legacy.stop(context.config["intent_sha256"], None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "run", "stop"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--supervisor-pid", type=int)
    args = parser.parse_args(argv)
    try:
        if args.mode == "check":
            context = prepare_runtime(args.config)
            print(json.dumps({"ok": True, "scope": "makerhub", "unit": context.config["unit"],
                              "container_ids": context.document["container_ids"]}), flush=True)
        elif args.mode == "run":
            run(args.config)
        else:
            stop(args.config, args.supervisor_pid)
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__,
                          "message": str(error) if isinstance(error, RuntimeError) else "Private monitor operation failed"}),
              flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
