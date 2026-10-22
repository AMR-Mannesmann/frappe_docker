---
title: Use Agent Tools in a Devcontainer
---

# Use T3 Code and Codex in the development container

The development template installs T3 Code, Codex, GitHub CLI (`gh`), GitLab CLI (`glab`), and the Frappe/Frappe UI agent skills through Dev Container lifecycle hooks using the existing Bench image. No custom image build is required. T3 runs inside the same container as Bench, so its agents can use the development environment directly.

You need Docker Compose and either the VS Code Dev Containers extension or the [Dev Container CLI](https://code.visualstudio.com/docs/devcontainers/devcontainer-cli). Start from the repository root. The creation hook runs the existing installer for Bench, applications and site creation. See the [development setup](../05-development/01-development.md) for the installer options.

## Set up the devcontainer

The template already includes the agent Compose configuration and waits for T3 startup. Copying it is enough to enable the workflow; no JSON edits are needed. The defaults create `frappe-bench`, install the applications in `apps-example.json`, create `development.localhost`, and publish T3 on host port `3774`, Frappe web on `8000`, and realtime on `9000`. Start Bench to serve the created site, as shown in step 4.

The steps below include choosing the apps, names and ports for your stack in `.devcontainer/.env`. If your network requires a corporate CA, apply the configuration under [Customize the setup](#customize-the-setup) before the first container creation.

1. Copy the development configuration if you do not already have one:

   ```sh
   [ -d .devcontainer ] || cp -R devcontainer-example .devcontainer
   ```

2. Create `.devcontainer/.env` from the example if it does not already exist, then edit it for your stack:

   ```sh
   [ -f .devcontainer/.env ] || cp .devcontainer/.env.example .devcontainer/.env
   ```

   Choose the Compose project name, host ports, T3 channel, apps JSON, Bench name and site name. For example:

   ```dotenv
   COMPOSE_PROJECT_NAME=frappe-dev-a
   FRAPPE_T3_PORT=3774
   FRAPPE_T3_CHANNEL=latest
   FRAPPE_HTTP_PORT=8000
   FRAPPE_REALTIME_PORT=9000
   FRAPPE_APPS_JSON=../.devcontainer/apps.json
   FRAPPE_BENCH_NAME=my-bench
   FRAPPE_SITE_NAME=my-app.localhost
   ```

   Each parallel stack needs its own project name, source checkout and available host ports. For example, a second stack can use T3 port `3775`, web port `8001` and realtime port `9001`. For an existing stack, retain its project name and T3 port to keep using its state and connection address. Container ports remain `3773`, `8000` and `9000`; all published ports listen on the host's loopback interface.

   Prepare the selected apps JSON before starting. For the custom path above, copy `development/apps-example.json` to `.devcontainer/apps.json` and edit its applications. Paths are container paths relative to `/workspace/development`. You can also keep `FRAPPE_APPS_JSON=apps-example.json` to use the provided application list. Keep defaults for any settings that already suit your project.

3. Create or recreate the environment:

   ```sh
   devcontainer up --workspace-folder . --remove-existing-container
   ```

   In VS Code, use **Dev Containers: Reopen in Container** for a new environment or **Dev Containers: Rebuild Container** when enabling agents in an existing one. Reopening an existing container alone does not apply the new volumes, environment, and published port.

4. Start Frappe from a terminal in the repository root:

   ```sh
   devcontainer exec --workspace-folder . bash -lc 'cd "/workspace/development/$FRAPPE_BENCH_NAME" && bench start'
   ```

   Leave this command running while using the site. In a T3 terminal already at the Bench root, run `bench start` directly. Creation initializes the site; this command starts its web server, realtime server, workers, scheduler and asset watcher. Run it again after recreating or restarting the container.

   Open `http://<FRAPPE_SITE_NAME>:<FRAPPE_HTTP_PORT>`. The defaults use [http://development.localhost:8000](http://development.localhost:8000); the sample `.env` above uses `http://my-app.localhost:8000`. Use the site's `.localhost` hostname so Frappe selects the correct site. Sign in as `Administrator` with the installer password (`admin` by default).

   These are Docker-published ports and work with the CLI and T3. `forwardPorts` in `devcontainer.json` provides additional VS Code forwarding; it is not a replacement for Docker publishing when using the CLI alone.

The ordered setup commands are in `devcontainer-example/devcontainer.json`. `postCreateCommand` runs the existing installer first, then installs pre-commit into that Bench's Python environment, the GitHub/GitLab CLIs from the image's Debian package repositories, and the npm tools:

```sh
cd /workspace/development
python installer.py --apps-json "$FRAPPE_APPS_JSON" --bench-name "$FRAPPE_BENCH_NAME" --site-name "$FRAPPE_SITE_NAME"
cd "$FRAPPE_BENCH_NAME"
bench set-config -gp webserver_port 8000
bench set-config -gp socketio_port 9000
sed -i -E '/^web:[[:space:]]+bench serve([[:space:]]|$)/s/(^|[[:space:]])--port([[:space:]]+|=)[0-9]+/\1--port 8000/' Procfile
bench --site "$FRAPPE_SITE_NAME" set-config -p socketio_port "$FRAPPE_REALTIME_PORT"
uv pip freeze --python env/bin/python --exclude-editable > env/pre-commit-constraints.txt
uv pip install --python env/bin/python --constraint env/pre-commit-constraints.txt pre-commit
uv pip check --python env/bin/python
sudo apt-get update
sudo apt-get install --yes --no-install-recommends gh glab
npm install --global @openai/codex "t3@${FRAPPE_T3_CHANNEL:-latest}"
t3 --version
npx --yes skills add frappe/skills --global --agent codex --skill '*' --yes --copy
npx --yes skills add https://github.com/frappe/frappe-ui/tree/main/skills/frappe-ui --global --agent codex --yes --copy
```

When omitted from `.env`, the three installer settings default to `apps-example.json`, `frappe-bench`, and `development.localhost`. Existing Bench and site directories are retained on recreation. Before reusing a site, the installer checks its database through `bench list-apps` and requires the Bench's applications to be installed. An unusable database, missing applications, or a failed installer command stops the hook before the tool installations; repair an incomplete site before retrying.

The generated constraints file keeps pre-commit's installation from replacing dependencies already installed for Frappe and your apps. It is regenerated from the selected Bench, and the dependency check must pass before npm runs.

The creation hook configures internal ports `8000` and `9000`, including the web command's port argument in the Procfile. Other Procfile entries are retained. The site advertises the published realtime port to the browser, keeping realtime connections working when parallel stacks use different host ports.

The npm packages use current releases, without version pins. T3 defaults to the stable `latest` npm channel. Set `FRAPPE_T3_CHANNEL=nightly` in `.devcontainer/.env` to install `t3@nightly` instead. Each new container resolves the current release on the selected channel and installs it automatically. Recreate the container to apply a channel change. The version check also completes T3's first native-runtime download before the startup hook's readiness deadline.

`postStartCommand` enters the configured Bench, activates its Python environment, registers the Bench with `t3 project add`, and runs `t3 serve`. An already-registered project is retained. The commands run in their own session with `nohup` and `setsid`, using `flock` to keep one server for this environment. This lets the server keep running when the setup terminal closes. Native project registration and headless serving also handle stale runtime records after container recreation. Agents inherit the Bench environment, including pre-commit. The hook waits for runtime metadata and the HTTP endpoint before completing.

Use Dev Containers to create and resume the environment. Plain `docker compose up` or `docker restart` does not execute Dev Container lifecycle hooks.

## Frappe agent skills

Setup automatically installs all skills from [frappe/skills](https://github.com/frappe/skills) and the [Frappe UI skill](https://github.com/frappe/frappe-ui/tree/main/skills/frappe-ui) for Codex. This includes framework development, Frappe code review, code style, technical writing, UI design, and the repository's explicitly invoked workflows.

Skills are available across the Bench and individual app projects through `/home/frappe/.agents/skills`. The complete shared agent directory, including the skills CLI's registry metadata, is stored in the ignored `development/.agent-data` directory on the host. Keep that directory with the checkout when recreating the container. Each stack's own checkout keeps its skills separate.

The creation hook reinstalls the current upstream skills when a container is recreated; ordinary restarts keep the installed copies. Additional skills in other directories are retained. Keep changes to the bundled skills in your own skill repository because reinstalling refreshes their contents. The skill repositories are not copied into the Bench's application list and do not install Frappe apps.

After installation, the skills are available to the agent on its next turn. Their descriptions control automatic use; skills marked for explicit invocation, such as `ship-it` and `deep-app-audit`, still require you to request them.

## Check the environment

```sh
devcontainer exec --workspace-folder . sh -c 'bench --version && codex --version && t3 --version && gh --version && glab --version'
devcontainer exec --workspace-folder . sh -c '"/workspace/development/$FRAPPE_BENCH_NAME/env/bin/pre-commit" --version'
devcontainer exec --workspace-folder . curl --noproxy '*' --fail --silent --output /dev/null http://127.0.0.1:3773/
```

T3 starts with `/workspace/development/<FRAPPE_BENCH_NAME>` as its project directory and uses the Compose project name, such as `frappe-dev-a`, as its initial title. Existing project titles are retained on subsequent starts, including any name you choose in T3. You can add individual application projects as well. Project registration and settings persist with the environment.

To rename an already-registered Bench project without recreating it or its threads:

```sh
devcontainer exec --workspace-folder . bash -ec 't3 project rename "/workspace/development/$FRAPPE_BENCH_NAME" "my-stack"'
```

T3 is started automatically during setup. For the initial browser connection, mint a short-lived pairing link in your own terminal:

```sh
devcontainer exec --workspace-folder . t3 pair
```

`NoRunningServerError` means the server failed to start or has stopped; it is not an additional manual startup step. Check the HTTP endpoint above and the private server log below if it occurs.

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

The example does not mount your host T3 home or share credentials between stacks. Each stack retains its own environment identity and sessions. To initialize preferences from your local machine, see [Reuse local T3 settings](#reuse-local-t3-settings).

## Reuse local T3 settings

New stacks can automatically start with your local T3 preferences. Configure the source before their first startup:

1. Set `FRAPPE_T3_SETTINGS_FILE` in `.devcontainer/.env` to the absolute path of your local T3 environment's `userdata/settings.json`. For a standard Linux installation:

   ```dotenv
   FRAPPE_T3_SETTINGS_FILE=/home/your-user/.t3/userdata/settings.json
   ```

2. Create the devcontainer as usual. The source file must exist on the Docker host. A custom T3 home changes its location; when using WSL, choose the settings of the local environment you actually want to inherit.

No changes to `devcontainer.json` are needed. If the variable is unset, the template uses `.devcontainer/t3-settings.json`, which contains `{}` and leaves T3's defaults in place. You can also put shared preferences in that file and keep it in your own development configuration.

The startup hook reads that file through a read-only mount and imports its preferences before T3 creates the project or starts the server. Defaults for models, provider options, source-control writing, and thread behavior come from the local environment rather than T3's built-in defaults. Reuse this configured `.devcontainer` for subsequent stacks; each new stack reads the current local preferences automatically.

Per-project overrides, host directories, device hosts, external integration settings, and sensitive provider environment variables are omitted. Provider executable and home-directory overrides are reset to use the container's own tools and authentication. T3's secret store and CLI login files are not copied. Custom provider commands and launch arguments must still work inside the container; seeding does not install additional providers. Appearance and other device preferences stay with the T3 client you are already using.

The import runs only while the destination has no `userdata/settings.json`. An existing stack retains its saved settings on restart and recreation, even if the local preferences change. The host file is never modified, and T3 identity, pairing, projects, and threads are never copied. Malformed JSON stops startup rather than installing a partial settings file.

For later changes to existing environments, T3's **Settings → All projects → All environments** controls can apply edits to all connected targets. That is a bulk edit rather than ongoing synchronization. See T3's [settings and project overrides](https://github.com/pingdotgg/t3code/blob/main/docs/user/project-settings.md) for the setting scopes.

## Authenticate GitHub and GitLab

Both CLIs are installed automatically. Sign in to the services you use:

```sh
devcontainer exec --workspace-folder . gh auth login
devcontainer exec --workspace-folder . glab auth login --hostname gitlab.example.com
```

Replace `gitlab.example.com` with your GitLab hostname. CLI configuration and file-based logins persist in project-scoped `github-cli-data` and `gitlab-cli-data` volumes, mounted at `/home/frappe/.config/gh` and `/home/frappe/.config/glab-cli` respectively. These are separate from Codex authentication.

## Customize the setup

Step 2 above covers the manual `.env` edits for your stack. The Compose overlay passes these values into the container and the lifecycle hook passes them to the existing installer:

| Setting             | Installer argument | Default                 |
| ------------------- | ------------------ | ----------------------- |
| `FRAPPE_APPS_JSON`  | `--apps-json`      | `apps-example.json`     |
| `FRAPPE_BENCH_NAME` | `--bench-name`     | `frappe-bench`          |
| `FRAPPE_SITE_NAME`  | `--site-name`      | `development.localhost` |

Keep the Bench name relative to `/workspace/development`. Its value is used by both lifecycle hooks, so changing it does not require editing their directory paths. Other installer options, such as `--frappe-branch` or `--db-type`, can still be added directly to the installer command in `postCreateCommand`.

Apply configuration changes by recreating the container. These are creation inputs: changing the apps JSON does not change an existing Bench's applications, and changing a Bench or site name selects a different directory or site rather than renaming existing data.

T3 uses the Bench directory as its initial project and the Compose project name as the default display title. Rename the project in T3 to change that title; keep `COMPOSE_PROJECT_NAME` stable for an existing stack because it also selects its persistent volumes. For Frappe development, use threads in the project's checkout and give parallel stacks separate checkouts and data.

For a corporate CA, add a private Compose override that mounts your certificate files and arrange for `sudo update-ca-certificates` to run before the network-dependent `postCreateCommand` steps. Configure the Node or Codex CA bundle when required by those clients. Keep company certificates and credentials in your local configuration or private repository.

## Recreate without losing state

```sh
devcontainer up --workspace-folder . --remove-existing-container
```

Compose keeps Codex state in `codex-data` and the complete T3 home in `t3-data`, scoped to the Compose project. This preserves authentication caches, provider sessions, T3 identity, threads, settings, and managed worktrees. The Bench stays in the mounted workspace, and the creation hook reinstalls the npm tools into the new container.

GitHub and GitLab CLI configuration is retained in their own volumes, and the creation hook reinstalls both CLI packages. When updating an existing configuration, recreate the container to apply the new mounts and environment variables.

Keep your workspace checkout and database volume as well. Removing volumes, changing the Compose project name, or deleting the checkout can disconnect existing state from its files. Back up persistent data separately; container recreation is not a backup operation. An active agent turn is interrupted when its container stops.
