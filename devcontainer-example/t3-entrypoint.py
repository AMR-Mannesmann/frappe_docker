#!/usr/bin/env python3
"""Prepare T3 state and hand the container process to its native launcher."""

import argparse
import fcntl
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LAUNCHER_PROTOCOL = 3
READY_FILE = Path("/tmp/frappe-t3-provisioned")
STARTUP_TIMEOUT = 240
NUMBER = r"(?:0|[1-9]\d*)"
PRERELEASE = rf"(?:{NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
BUILD = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
VERSION = re.compile(
    rf"{NUMBER}\.{NUMBER}\.{NUMBER}(?:-{PRERELEASE}(?:\.{PRERELEASE})*)?(?:\+{BUILD})?"
)


class LifecycleError(Exception):
    pass


class T3Lifecycle:
    def __init__(self, ready_file=READY_FILE):
        self.home = Path(os.environ["T3CODE_HOME"])
        self.bench = Path("/workspace/development") / os.environ["FRAPPE_BENCH_NAME"]
        self.state_file = self.home / "runtime/service-state.json"
        self.log = self.home / "devcontainer/server.log"
        self.ready_file = ready_file
        self.port = int(os.environ.get("T3CODE_PORT", "3773"))

    def activate_bench(self):
        environment = self.bench / "env"
        if not (environment / "bin/python").is_file():
            raise LifecycleError(
                "The Bench Python environment is missing; complete postCreateCommand first."
            )
        os.chdir(self.bench)
        os.environ["VIRTUAL_ENV"] = str(environment)
        os.environ["PATH"] = str(environment / "bin") + os.pathsep + os.environ["PATH"]
        os.environ.pop("PYTHONHOME", None)

    def log_output(self, output):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(output)

    def command(self, arguments):
        try:
            result = subprocess.run(
                arguments, capture_output=True, text=True, check=False, timeout=600
            )
        except subprocess.TimeoutExpired as error:
            raise LifecycleError(
                "T3 lifecycle command timed out; retry provisioning when downloads are available."
            ) from error
        if result.returncode:
            self.log_output(result.stdout + result.stderr)
            raise LifecycleError(
                f"T3 lifecycle command failed; inspect the private log at {self.log}."
            )
        return result

    @staticmethod
    def valid_version(version):
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise LifecycleError("Invalid T3 runtime version in launcher state.")
        return version

    def read_state(self):
        state = json.loads(self.state_file.read_text())
        if not isinstance(state, dict) or state.get("protocol") != LAUNCHER_PROTOCOL:
            raise LifecycleError(
                "Unsupported T3 launcher state protocol; update the devcontainer integration."
            )
        self.valid_version(state.get("activeVersion"))
        if "update" in state and (
            not isinstance(state["update"], dict)
            or state["update"].get("status")
            not in ("pending", "committed", "rolled-back", "failed")
        ):
            raise LifecycleError(
                "Invalid T3 update record; preserve the state file for recovery."
            )
        return state

    def bootstrap_version(self):
        prefix = self.command(["npm", "root", "--global"]).stdout.strip()
        package = json.loads((Path(prefix) / "t3/package.json").read_text())
        return self.valid_version(package.get("version"))

    def runtime(self, version, preflight=True):
        version = self.valid_version(version)
        directory = self.home / "runtime/versions" / version
        sentinel = directory / ".install-complete"

        def complete():
            return (
                sentinel.is_file()
                and sentinel.read_text().strip() == version
                and (directory / "t3").is_file()
                and os.access(directory / "t3", os.X_OK)
                and (directory / "client/index.html").is_file()
                and (directory / "resource-monitor").is_dir()
                and (directory / "node_modules").is_dir()
            )

        if not complete():
            if directory.is_symlink():
                raise LifecycleError(
                    "Refusing to repair a symlinked T3 runtime directory."
                )
            # The npm launcher repairs an incomplete cache when its sentinel is absent.
            sentinel.unlink(missing_ok=True)
            print(
                f"Restoring the selected T3 runtime {version} through npm.", flush=True
            )
            self.command(["npx", "--yes", f"t3@{version}", "--version"])
            if not complete():
                raise LifecycleError(
                    "The selected T3 runtime remains incomplete after npm recovery."
                )

        executable = directory / "t3"
        if preflight:
            result = self.command(
                [
                    str(executable),
                    "__service-preflight",
                    "--database-path",
                    str(self.home / "userdata/statev2.sqlite"),
                    "--launcher-protocol",
                    str(LAUNCHER_PROTOCOL),
                ]
            )
            check = json.loads(result.stdout)
            if (
                check.get("status") != "ready"
                or check.get("version") != version
                or check.get("launcherProtocol") != LAUNCHER_PROTOCOL
            ):
                raise LifecycleError(
                    "This T3 runtime is incompatible with the devcontainer launcher integration."
                )
            help_text = self.command(
                [str(executable), "__service-launcher", "--help"]
            ).stdout
            if "__service-launcher" not in help_text:
                raise LifecycleError(
                    "The selected T3 runtime does not provide the expected launcher entrypoint."
                )
        return executable

    @staticmethod
    def write_once(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".t3-init-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            else:
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            os.unlink(temporary)

    def seed_settings(self):
        source = Path("/run/frappe-t3-settings.json")
        destination = self.home / "userdata/settings.json"
        if destination.exists() or not source.is_file():
            return
        settings = json.loads(source.read_text())
        if not isinstance(settings, dict):
            raise LifecycleError("T3 settings must be a JSON object.")
        for key in list(settings):
            if (key.startswith("project") and key.endswith("Overrides")) or key in (
                "projectSettingsFolded",
                "worktreesDirectory",
                "previousWorktreesDirectories",
                "addProjectBaseDirectory",
                "environmentIcon",
                "deviceHosts",
                "easDeviceHosts",
                "sourceControlHosts",
                "usageLimitSources",
                "observability",
            ):
                del settings[key]
        for instance in settings.get("providerInstances", {}).values():
            config = instance.get("config")
            if isinstance(config, dict):
                for key in ("binaryPath", "homePath", "shadowHomePath"):
                    config.pop(key, None)
            if "environment" in instance:
                instance["environment"] = [
                    variable
                    for variable in instance["environment"]
                    if not variable.get("sensitive")
                    and not variable.get("valueRedacted")
                ]
        self.write_once(destination, settings)

    def descriptor(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", "/.well-known/t3/environment")
            response = connection.getresponse()
            if response.status != 200:
                raise LifecycleError("T3's environment descriptor is not ready.")
            return json.loads(response.read())
        finally:
            connection.close()

    def wait_ready(self):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            state = self.read_state()
            try:
                descriptor = self.descriptor()
                if (
                    state.get("update", {}).get("status") != "pending"
                    and descriptor.get("serverVersion") == state["activeVersion"]
                    and descriptor.get("capabilities", {}).get("serverSelfUpdate")
                    == "boot-service"
                ):
                    return descriptor
            except (OSError, ValueError, LifecycleError, http.client.HTTPException):
                pass
            time.sleep(0.5)
        raise LifecycleError(
            f"Managed T3 startup did not complete; inspect the private log at {self.log}."
        )

    def prepare(self):
        self.activate_bench()
        if not self.ready_file.exists():
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with (self.log.parent / "bootstrap.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if self.state_file.exists():
                    state = self.read_state()
                    pending = state.get("update", {}).get("status") == "pending"
                    self.runtime(state["activeVersion"], preflight=not pending)
                else:
                    version = self.bootstrap_version()
                    self.runtime(version)
                    self.write_once(
                        self.state_file,
                        {"protocol": LAUNCHER_PROTOCOL, "activeVersion": version},
                    )
                    state = self.read_state()
                    pending = False
                if not pending:
                    self.seed_settings()
                self.ready_file.touch(mode=0o600)

        # Recovery and migrations must finish before a project command opens the database.
        self.wait_ready()
        state = self.read_state()
        executable = self.runtime(state["activeVersion"])
        result = subprocess.run(
            [
                str(executable),
                "project",
                "add",
                "--base-dir",
                str(self.home),
                "--title",
                os.environ.get("COMPOSE_PROJECT_NAME")
                or os.environ["FRAPPE_BENCH_NAME"],
                str(self.bench),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            result.returncode
            and "ProjectAlreadyExistsError:" not in result.stdout + result.stderr
        ):
            self.log_output(result.stdout + result.stderr)
            raise LifecycleError(
                f"Could not register the Bench project; inspect {self.log}."
            )

    def run(self):
        if os.environ.get("FRAPPE_DEV_AGENTS") != "1":
            os.execvp("sleep", ["sleep", "infinity"])
        print(
            "Waiting for Dev Container provisioning to prepare the T3 launcher.",
            flush=True,
        )
        while not self.ready_file.exists():
            time.sleep(0.5)
        self.activate_bench()
        state = self.read_state()
        pending = state.get("update", {}).get("status") == "pending"
        executable = self.runtime(state["activeVersion"], preflight=not pending)
        descriptor = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
        os.close(descriptor)
        os.execv(executable, [str(executable), "__service-launcher"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "prepare", "status"))
    action = parser.parse_args().action
    os.umask(0o077)
    lifecycle = T3Lifecycle()
    if action == "status":
        descriptor = lifecycle.descriptor()
        print(
            json.dumps(
                {
                    "serverVersion": descriptor["serverVersion"],
                    "serverSelfUpdate": descriptor.get("capabilities", {}).get(
                        "serverSelfUpdate"
                    ),
                }
            )
        )
    else:
        getattr(lifecycle, action)()


if __name__ == "__main__":
    try:
        main()
    except (LifecycleError, OSError, ValueError) as error:
        print(f"T3 devcontainer: {error}", file=sys.stderr)
        sys.exit(1)
