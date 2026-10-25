#!/usr/bin/env python3
"""Hand the foreground container process to T3 after lifecycle hooks provision it."""

import json
import os
import re
import sys
import time
from pathlib import Path

READY_FILE = Path("/tmp/frappe-t3-provisioned")


def selected_runtime(home):
    state = json.loads((home / "runtime/service-state.json").read_text())
    version = state.get("activeVersion") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or state.get("protocol") != 3
        or not isinstance(version, str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?", version)
    ):
        raise RuntimeError("Unsupported T3 launcher state; preserve it for repair.")
    executable = home / "runtime/versions" / version / "t3"
    if not os.access(executable, os.X_OK):
        raise RuntimeError(
            "The selected T3 runtime is missing; rerun Dev Container lifecycle hooks."
        )
    return executable


def main(ready_file=READY_FILE):
    if os.environ.get("FRAPPE_DEV_AGENTS") != "1":
        os.execvp("sleep", ["sleep", "infinity"])
    print("Waiting for Dev Container provisioning to prepare T3.", flush=True)
    while not ready_file.exists():
        time.sleep(0.5)
    os.umask(0o077)
    bench = Path("/workspace/development") / os.environ["FRAPPE_BENCH_NAME"]
    environment = bench / "env"
    if not (environment / "bin/python").is_file():
        raise RuntimeError("The Bench environment is missing; rerun provisioning.")
    os.chdir(bench)
    os.environ["VIRTUAL_ENV"] = str(environment)
    os.environ["PATH"] = str(environment / "bin") + os.pathsep + os.environ["PATH"]
    os.environ.pop("PYTHONHOME", None)
    home = Path(os.environ["T3CODE_HOME"])
    executable = selected_runtime(home)
    descriptor = os.open(
        home / "devcontainer/server.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    os.fchmod(descriptor, 0o600)
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    os.close(descriptor)
    os.execv(executable, [str(executable), "__service-launcher"])


if __name__ == "__main__":
    try:
        if len(sys.argv) != 1:
            raise RuntimeError("The foreground entrypoint takes no arguments.")
        main()
    except (RuntimeError, OSError, ValueError) as error:
        print(f"T3 devcontainer: {error}", file=sys.stderr)
        sys.exit(1)
