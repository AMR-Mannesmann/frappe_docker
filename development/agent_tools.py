"""Provision optional agent tools and manage T3 in the Bench devcontainer."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
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

T3_PORT = 3773


@dataclass(frozen=True)
class Config:
    codex_home: Path
    t3_home: Path
    bench_dir: Path
    project_dir: Path
    frappe_branch: str = "version-16"

    @property
    def runtime_dir(self):
        identity = hashlib.sha256(str(self.t3_home).encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"frappe-t3-{os.getuid()}-{identity}"

    @property
    def log_path(self):
        return self.t3_home / "devcontainer" / "server.log"


def config_from_environment():
    bench_dir = Path(
        os.getenv("FRAPPE_BENCH_DIR") or "/workspace/development/frappe-bench"
    ).resolve()
    return Config(
        codex_home=Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").resolve(),
        t3_home=Path(os.getenv("T3CODE_HOME") or Path.home() / ".t3").resolve(),
        bench_dir=bench_dir,
        project_dir=Path(os.getenv("FRAPPE_T3_PROJECT_DIR") or bench_dir).resolve(),
        frappe_branch=os.getenv("FRAPPE_BENCH_BRANCH") or "version-16",
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


def ensure_bench(config):
    """Initialize a missing Bench, preserving an existing development workspace."""
    if config.bench_dir.exists():
        required = [
            config.bench_dir / "apps" / "frappe",
            config.bench_dir / "sites" / "apps.txt",
            config.bench_dir / "env" / "bin" / "python",
        ]
        if not all(path.exists() for path in required):
            raise RuntimeError(
                f"{config.bench_dir} exists but is not an initialized Bench; "
                "complete or repair it before running setup"
            )
        return
    config.bench_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "bench",
            "init",
            "--skip-redis-config-generation",
            "--frappe-branch",
            config.frappe_branch,
            str(config.bench_dir),
        ],
        cwd=config.bench_dir.parent,
        check=True,
    )
    for key, value in (
        ("db_host", "mariadb"),
        ("redis_cache", "redis://redis-cache:6379"),
        ("redis_queue", "redis://redis-queue:6379"),
        ("redis_socketio", "redis://redis-queue:6379"),
    ):
        subprocess.run(
            ["bench", "set-config", "-g", key, value],
            cwd=config.bench_dir,
            check=True,
        )
    subprocess.run(
        ["bench", "set-config", "-gp", "developer_mode", "1"],
        cwd=config.bench_dir,
        check=True,
    )


def ensure_tools():
    missing = [name for name in ("codex", "t3") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Agent tools are missing; run agent_tools.py setup first")


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
        ensure_bench(config)
        subprocess.run(
            ["npm", "install", "--global", "@openai/codex", "t3"],
            cwd=config.bench_dir,
            check=True,
        )
        ensure_tools()
        seed_codex_auth(config)
    print("Bench, Codex and T3 are ready")


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
        ensure_tools()
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
        with os.fdopen(descriptor, "ab", buffering=0) as log:
            process = subprocess.Popen(
                [
                    shutil.which("t3"),
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
