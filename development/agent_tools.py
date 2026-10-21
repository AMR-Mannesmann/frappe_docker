"""Provision optional agent tools and manage T3 in the Bench devcontainer."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CODEX_VERSION = "0.163.0"
DEFAULT_T3_VERSION = "0.1.1"
T3_PORT = 3773


@dataclass(frozen=True)
class Config:
    codex_home: Path
    t3_home: Path
    bin_dir: Path
    project_dir: Path
    codex_version: str = DEFAULT_CODEX_VERSION
    t3_version: str = DEFAULT_T3_VERSION

    @property
    def runtime_dir(self):
        identity = hashlib.sha256(str(self.t3_home).encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"frappe-t3-{os.getuid()}-{identity}"

    @property
    def log_path(self):
        return self.t3_home / "devcontainer" / "server.log"


def config_from_environment():
    versions = {
        "codex_version": os.getenv("FRAPPE_CODEX_VERSION") or DEFAULT_CODEX_VERSION,
        "t3_version": os.getenv("FRAPPE_T3_VERSION") or DEFAULT_T3_VERSION,
    }
    for name, version in versions.items():
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?", version):
            raise ValueError(
                f"{name} must be an exact release version, got {version!r}"
            )
    return Config(
        codex_home=Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").resolve(),
        t3_home=Path(os.getenv("T3CODE_HOME") or Path.home() / ".t3").resolve(),
        bin_dir=Path.home() / ".local" / "bin",
        project_dir=Path(
            os.getenv("FRAPPE_T3_PROJECT_DIR") or "/workspace/development"
        ).resolve(),
        **versions,
    )


def prepare_directory(path):
    """Make a state directory writable after Docker creates an empty volume."""
    path.mkdir(parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        subprocess.run(
            ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(path)],
            check=True,
        )


@contextlib.contextmanager
def lifecycle_lock(config):
    config.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (config.runtime_dir / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def binary_version(binary):
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    words = result.stdout.strip().split()
    if result.returncode == 0 and words:
        return words[-1].lstrip("v")
    return None


def cached_tool(config, name):
    if name == "t3":
        root = config.t3_home / "runtime" / "versions" / config.t3_version
        required = [
            root / "t3",
            root / "client" / "index.html",
            root / "resource-monitor",
            root / "node_modules",
        ]
        binary = root / "t3"
        version = config.t3_version
    else:
        root = config.codex_home / "packages" / "standalone" / "current"
        required = [
            root / "codex-package.json",
            root / "bin" / "codex",
            root / "bin" / "codex-code-mode-host",
            root / "codex-resources" / "bwrap",
            root / "codex-path" / "rg",
        ]
        binary = root / "bin" / "codex"
        version = config.codex_version
    if all(path.exists() for path in required) and binary_version(binary) == version:
        return binary
    return None


def restore_launcher(config, name, binary):
    """Restore only our launcher, leaving Bench's .local/bin entries intact."""
    config.bin_dir.mkdir(parents=True, exist_ok=True)
    link = config.bin_dir / name
    temporary = config.bin_dir / f".{name}-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(binary)
    temporary.replace(link)


def install_tool(config, name):
    version = config.t3_version if name == "t3" else config.codex_version
    if name == "t3":
        url = f"https://raw.githubusercontent.com/pingdotgg/t3code/v{version}/scripts/install.sh"
    else:
        url = f"https://raw.githubusercontent.com/openai/codex/rust-v{version}/scripts/install/install.sh"
    installers = config.t3_home / "devcontainer" / "installers"
    installers.mkdir(parents=True, exist_ok=True)
    installer = installers / f"{name}-{version}.sh"
    if not installer.exists():
        temporary = installer.with_suffix(".download")
        subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "3",
                "--connect-timeout",
                "10",
                "--max-time",
                "120",
                url,
                "--output",
                str(temporary),
            ],
            check=True,
        )
        temporary.chmod(0o700)
        temporary.replace(installer)
    environment = os.environ.copy()
    environment.update(
        CODEX_HOME=str(config.codex_home),
        CODEX_INSTALL_DIR=str(config.bin_dir),
        CODEX_NON_INTERACTIVE="1",
        T3CODE_HOME=str(config.t3_home),
        T3CODE_INSTALL_BIN_DIR=str(config.bin_dir),
        T3CODE_VERSION=config.t3_version,
    )
    arguments = ["sh", str(installer)]
    if name == "codex":
        arguments.extend(["--release", version])
    else:
        # The upstream installer uses this marker to skip an extracted release.
        # A missing package component must trigger extraction again.
        marker = config.t3_home / "runtime" / "versions" / version / ".install-complete"
        marker.unlink(missing_ok=True)
    subprocess.run(arguments, env=environment, check=True)


def ensure_tools(config, install=False):
    for name in ("codex", "t3"):
        binary = cached_tool(config, name)
        if binary is None and install:
            install_tool(config, name)
            binary = cached_tool(config, name)
        if binary is None:
            raise RuntimeError(f"{name} is incomplete; run agent_tools.py setup first")
        restore_launcher(config, name, binary)


def seed_codex_auth(config):
    """Copy an explicitly mounted login once; preserve the writable auth cache."""
    source = os.getenv("FRAPPE_CODEX_AUTH_SOURCE")
    destination = config.codex_home / "auth.json"
    if not source or destination.exists():
        return
    payload = Path(source).read_bytes()
    if not isinstance(json.loads(payload), dict):
        raise ValueError("The mounted Codex auth cache must contain a JSON object")
    with tempfile.NamedTemporaryFile(dir=config.codex_home, delete=False) as auth:
        temporary = Path(auth.name)
        auth.write(payload)
    try:
        os.link(temporary, destination)
    except FileExistsError:
        return
    finally:
        temporary.unlink(missing_ok=True)
    print("Seeded the container's private Codex auth cache")


def setup(config):
    for path in (config.codex_home, config.t3_home):
        prepare_directory(path)
    with lifecycle_lock(config):
        ensure_tools(config, install=True)
        seed_codex_auth(config)
    print(f"Agent tools ready: Codex {config.codex_version}, T3 {config.t3_version}")


def process_start_time(pid):
    """Return Linux process identity, excluding dead or recycled PIDs."""
    try:
        fields = (
            (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        )
        if fields[0] in ("Z", "X"):
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def managed_process(config):
    try:
        record = json.loads((config.runtime_dir / "server.json").read_text())
        pid = record["pid"]
        identity = process_start_time(pid)
        if (
            isinstance(pid, int)
            and pid > 1
            and identity is not None
            and identity == record["start_time"]
        ):
            return pid
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def server_ready(config):
    try:
        runtime = json.loads(
            (config.t3_home / "userdata" / "server-runtime.json").read_text()
        )
        if runtime.get("port") != T3_PORT or not process_start_time(runtime["pid"]):
            return False
        if os.getpgid(runtime["pid"]) != managed_process(config):
            return False
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{T3_PORT}/", timeout=1) as response:
            return response.status == 200
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return False


def terminate_process(pid):
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def start(config):
    with lifecycle_lock(config):
        ensure_tools(config)
        if managed_process(config):
            if not server_ready(config):
                raise RuntimeError(
                    f"The managed T3 process is unhealthy; inspect {config.log_path}"
                )
            print("T3 is already running")
            return
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", T3_PORT)) == 0:
                raise RuntimeError(
                    f"Port {T3_PORT} is already occupied; no additional T3 server started"
                )
        if not config.project_dir.is_dir():
            raise ValueError(
                f"T3 project directory does not exist: {config.project_dir}"
            )
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            config.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        environment = os.environ.copy()
        environment["PATH"] = f"{config.bin_dir}:{environment.get('PATH', '')}"
        with os.fdopen(descriptor, "ab", buffering=0) as log:
            process = subprocess.Popen(
                [
                    str(config.bin_dir / "t3"),
                    "start",
                    "--base-dir",
                    str(config.t3_home),
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(T3_PORT),
                    "--no-browser",
                    "--auto-bootstrap-project-from-cwd",
                    str(config.project_dir),
                ],
                cwd=config.project_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        identity = process_start_time(process.pid)
        record = {"pid": process.pid, "start_time": identity}
        (config.runtime_dir / "server.json").write_text(json.dumps(record))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"T3 exited during startup; inspect {config.log_path}"
                )
            if server_ready(config):
                print(f"T3 is ready on container port {T3_PORT}")
                return
            time.sleep(0.25)
        if managed_process(config):
            terminate_process(process.pid)
        raise RuntimeError(
            f"T3 did not become ready within 30 seconds; inspect {config.log_path}"
        )


def stop(config):
    with lifecycle_lock(config):
        pid = managed_process(config)
        if pid:
            terminate_process(pid)
            deadline = time.monotonic() + 10
            while process_start_time(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if process_start_time(pid):
                raise RuntimeError("T3 did not stop within 10 seconds")
        (config.runtime_dir / "server.json").unlink(missing_ok=True)
    print("T3 is stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "start", "stop", "status"))
    action = parser.parse_args().action
    if action in ("setup", "start") and os.getenv("FRAPPE_DEV_AGENTS") != "1":
        return 0
    config = config_from_environment()
    if action == "status":
        ready = bool(managed_process(config)) and server_ready(config)
        print("T3 is running" if ready else "T3 is not running")
        return 0 if ready else 1
    {"setup": setup, "start": start, "stop": stop}[action](config)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Agent setup: {error}", file=sys.stderr)
        sys.exit(1)
