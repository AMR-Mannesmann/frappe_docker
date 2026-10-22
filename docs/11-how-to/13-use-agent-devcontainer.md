---
title: Use Agent Tools in a Devcontainer
---

# Use T3 Code and Codex in the development container

Use the existing Bench image with optional T3 Code and Codex tools installed by Dev Container lifecycle hooks. No custom image build is required. T3 runs inside the same container as Bench, so its agents can use the development environment directly.

You need Docker Compose and either the VS Code Dev Containers extension or the [Dev Container CLI](https://code.visualstudio.com/docs/devcontainers/devcontainer-cli). Start from the repository root. The creation hook runs the existing installer for Bench, applications and site creation. See the [development setup](../05-development/01-development.md) for the installer options.

## Enable agent tools

1. Copy the development configuration if you do not already have one:

   ```sh
   cp -R devcontainer-example .devcontainer
   ```

2. In `.devcontainer/devcontainer.json`, replace `dockerComposeFile` and add `waitFor`:

   ```json
   {
     "dockerComposeFile": ["docker-compose.yml", "docker-compose.agents.yml"],
     "waitFor": "postStartCommand"
   }
   ```

   Keep the other properties, including `postCreateCommand` and `postStartCommand`. If you have an older copy, bring these properties and the additional Compose files across from `devcontainer-example`.

3. Create `.devcontainer/.env` from the example if it does not already exist:

   ```sh
   [ -f .devcontainer/.env ] || cp .devcontainer/.env.example .devcontainer/.env
   ```

   Edit the settings below. For an existing stack, retain its Compose project name and host port. For a new stack, choose a unique project name and an available port:

   ```dotenv
   COMPOSE_PROJECT_NAME=frappe-dev-a
   FRAPPE_T3_PORT=3774
   FRAPPE_APPS_JSON=../.devcontainer/apps.json
   FRAPPE_BENCH_NAME=my-bench
   FRAPPE_SITE_NAME=my-app.localhost
   ```

   Create the selected apps JSON before starting the container. For example, copy `development/apps-example.json` to `.devcontainer/apps.json` and edit its applications. Paths are container paths relative to `/workspace/development`; the example above selects the file in `.devcontainer` at the repository root.

   Reuse that project name when recreating this stack. Each parallel stack needs its own project name, source checkout, and host port. The container port remains `3773`; the published port listens on the host's loopback interface.

4. Create or recreate the environment:

   ```sh
   devcontainer up --workspace-folder . --remove-existing-container
   ```

   In VS Code, use **Dev Containers: Reopen in Container** for a new environment or **Dev Containers: Rebuild Container** when enabling agents in an existing one. Reopening an existing container alone does not apply the new volumes, environment, and published port.

The ordered setup commands are in `devcontainer-example/devcontainer.json`. `postCreateCommand` runs the existing installer first, then installs pre-commit into that Bench's Python environment and installs the npm tools:

```sh
cd /workspace/development
python installer.py --apps-json "$FRAPPE_APPS_JSON" --bench-name "$FRAPPE_BENCH_NAME" --site-name "$FRAPPE_SITE_NAME"
cd "$FRAPPE_BENCH_NAME"
uv pip freeze --python env/bin/python --exclude-editable > env/pre-commit-constraints.txt
uv pip install --python env/bin/python --constraint env/pre-commit-constraints.txt pre-commit
uv pip check --python env/bin/python
npm install --global @openai/codex t3
```

When omitted from `.env`, the three installer settings default to `apps-example.json`, `frappe-bench`, and `development.localhost`. Existing Bench and site directories are retained on recreation. Before reusing a site, the installer checks its database through `bench list-apps` and requires the Bench's applications to be installed. An unusable database, missing applications, or a failed installer command stops the hook before the tool installations; repair an incomplete site before retrying.

The generated constraints file keeps pre-commit's installation from replacing dependencies already installed for Frappe and your apps. It is regenerated from the selected Bench, and the dependency check must pass before npm runs.

The npm packages use current releases, without version pins. Each new container installs them again automatically. The normal development configuration does not run the installer or install agent tools unless the agents overlay is enabled.

`postStartCommand` enters the configured Bench, activates its Python environment and starts T3 directly with `nohup`, using `flock` to keep one server for this environment. Agents inherit the Bench environment, including pre-commit. The hook waits for the HTTP endpoint before completing.

Use Dev Containers to create and resume the environment. Plain `docker compose up` or `docker restart` does not execute Dev Container lifecycle hooks.

## Check the environment

```sh
devcontainer exec --workspace-folder . sh -c 'bench --version && codex --version && t3 --version'
devcontainer exec --workspace-folder . sh -c '"/workspace/development/$FRAPPE_BENCH_NAME/env/bin/pre-commit" --version'
devcontainer exec --workspace-folder . curl --noproxy '*' --fail --silent --output /dev/null http://127.0.0.1:3773/
```

T3 starts with `/workspace/development/<FRAPPE_BENCH_NAME>` as its project directory. You can add individual application projects as well. Project registration and settings persist with the environment.

For the initial browser connection, mint a short-lived pairing link in your own terminal:

```sh
devcontainer exec --workspace-folder . t3 pair
```

The printed link uses the container's address and port. Replace its origin with `http://127.0.0.1:3774`, using your selected host port, and preserve the complete `/pair#token=...` suffix. Open that link on the Docker host. When adding this environment to another T3 client, use the host's reachable endpoint and the pairing token. A bare URL has no pairing credential, and the printed container-address QR code does not account for Docker port mapping.

T3 startup output is stored in `/home/frappe/.t3/devcontainer/server.log` with private file permissions. It can contain pairing credentials; read it only in your own terminal when connecting a client.

To restart T3, restart the devcontainer through Dev Containers. To reinstall or update the npm tools, recreate the container; its persistent data is retained.

## Authenticate Codex

Sign in once inside the environment using device authentication:

```sh
devcontainer exec --workspace-folder . codex login --device-auth
```

Alternatively, seed the container from an existing file-based host login:

1. Add `docker-compose.codex-auth.yml` after the agents overlay in `dockerComposeFile`.
2. Set `FRAPPE_CODEX_AUTH_FILE` in `.devcontainer/.env` to the absolute path of your existing host `auth.json`. This file must already exist on the Docker host.
3. Recreate the devcontainer so the file is mounted.

The source is mounted read-only and copied only when the container has no `auth.json`. The container's private copy remains writable for token refresh; subsequent setup preserves it. A host OS keyring is not exposed by mounting a file. If a copied login stops working after another client refreshes or revokes it, sign in separately or explicitly renew the seed. See [Codex authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices).

The example does not mount your host T3 home, transfer host settings, or share credentials between stacks. Each stack retains its own environment identity and sessions.

## Customize the setup

Set these values in `.devcontainer/.env`; the Compose overlay passes them into the container and the lifecycle hook passes them to the existing installer:

| Setting             | Installer argument | Default                 |
| ------------------- | ------------------ | ----------------------- |
| `FRAPPE_APPS_JSON`  | `--apps-json`      | `apps-example.json`     |
| `FRAPPE_BENCH_NAME` | `--bench-name`     | `frappe-bench`          |
| `FRAPPE_SITE_NAME`  | `--site-name`      | `development.localhost` |

Keep the Bench name relative to `/workspace/development`. Its value is used by both lifecycle hooks, so changing it does not require editing their directory paths. Other installer options, such as `--frappe-branch` or `--db-type`, can still be added directly to the installer command in `postCreateCommand`.

Apply configuration changes by recreating the container. These are creation inputs: changing the apps JSON does not change an existing Bench's applications, and changing a Bench or site name selects a different directory or site rather than renaming existing data.

T3 uses the Bench directory as its initial project. For Frappe development, use threads in the project's checkout and give parallel stacks separate checkouts and data.

For a corporate CA, add a private Compose override that mounts your certificate files and arrange for `sudo update-ca-certificates` to run before the network-dependent `postCreateCommand` steps. Configure the Node or Codex CA bundle when required by those clients. Keep company certificates and credentials in your local configuration or private repository.

## Recreate without losing state

```sh
devcontainer up --workspace-folder . --remove-existing-container
```

Compose keeps Codex state in `codex-data` and the complete T3 home in `t3-data`, scoped to the Compose project. This preserves authentication caches, provider sessions, T3 identity, threads, settings, and managed worktrees. The Bench stays in the mounted workspace, and the creation hook reinstalls the npm tools into the new container.

Keep your workspace checkout and database volume as well. Removing volumes, changing the Compose project name, or deleting the checkout can disconnect existing state from its files. Back up persistent data separately; container recreation is not a backup operation. An active agent turn is interrupted when its container stops.
