---
title: Use Agent Tools in a Devcontainer
---

# Use T3 Code and Codex in the development container

The development template installs T3 Code, Codex, GitHub CLI (`gh`), GitLab CLI (`glab`), and the Frappe/Frappe UI agent skills through Dev Container lifecycle hooks using the existing Bench image. No custom image build is required. T3 runs inside the same container as Bench, so its agents can use the development environment directly.

You need Docker Compose and either the VS Code Dev Containers extension or the [Dev Container CLI](https://code.visualstudio.com/docs/devcontainers/devcontainer-cli). Start from the repository root to copy the template, then run subsequent commands from your copied environment directory. The creation hook runs the existing installer for Bench, applications and site creation. See the [development setup](../05-development/01-development.md) for the installer options.

## Set up the devcontainer

The complete template lives in `development/`, including its `.devcontainer/` configuration, installer and apps example. Copy that directory once for each environment. Copies also work outside this repository; no files from the original checkout are mounted or required at runtime. Keep `development/` as the clean template and create Benches in the copies.

The template already includes the agent Compose configuration and waits for T3 startup. No JSON edits are needed to enable the workflow. The defaults create `frappe-bench`, install the applications in `apps-example.json`, create `development.localhost`, and publish T3 on host port `3774`, Frappe web on `8000`, and realtime on `9000`. Start Bench to serve the created site, as shown in step 4.

The steps below include choosing the apps, names and ports for your stack in `.devcontainer/.env`. If your network requires a corporate CA, apply the configuration under [Customize the setup](#customize-the-setup) before the first container creation.

1. Copy the complete development template into a new named environment, then enter it:

   ```sh
   cp -R development environments/task-a
   cd environments/task-a
   ```

   The repository includes `environments/` with a tracked placeholder; everything else under it is ignored by Git, and the directory is excluded from Docker build contexts. Each copy contains its own installer, apps configuration, skills data and Bench workspace. Choose a new destination name when creating another environment.

2. Edit the included `.devcontainer/.env` for your stack:

   Choose the host ports, T3 channel, apps JSON, Bench name and site name. For example:

   ```dotenv
   FRAPPE_T3_PORT=3774
   FRAPPE_T3_CHANNEL=latest
   FRAPPE_HTTP_PORT=8000
   FRAPPE_REALTIME_PORT=9000
   FRAPPE_APPS_JSON=apps.json
   FRAPPE_BENCH_NAME=my-bench
   FRAPPE_SITE_NAME=my-app.localhost
   ```

   Dev Containers derives the Compose project name from the copied folder: `task-a` becomes `task-a_devcontainer`. No `COMPOSE_PROJECT_NAME` entry is needed. Use unique lowercase folder names containing letters, digits, dashes or underscores, beginning with a letter or digit. Keep the folder name and T3 port stable for an existing environment to retain its volumes and connection address.

   Each parallel stack needs its own environment directory and available host ports. For example, a second stack can use T3 port `3775`, web port `8001` and realtime port `9001`. Container ports remain `3773`, `8000` and `9000`; all published ports listen on the host's loopback interface.

   Prepare the selected apps JSON before starting. For the custom path above, copy `apps-example.json` to `apps.json` inside your environment and edit its applications. Paths are container paths relative to `/workspace/development`, which is the copied environment directory. You can also edit the provided application list directly and keep `FRAPPE_APPS_JSON=apps-example.json`. Keep defaults for any settings that already suit your project.

3. Create or recreate the environment:

   ```sh
   devcontainer up --workspace-folder . --remove-existing-container
   ```

   In VS Code, open the copied environment folder, then use **Dev Containers: Reopen in Container** for a new environment or **Dev Containers: Rebuild Container** when updating an existing one. Reopening an existing container alone does not apply new volumes, environment, and published ports.

4. Start Frappe from a host terminal in the copied environment directory:

   ```sh
   devcontainer exec --workspace-folder . bash -lc 'cd "/workspace/development/$FRAPPE_BENCH_NAME" && bench start'
   ```

   Leave this command running while using the site. In a T3 terminal already at the Bench root, run `bench start` directly. Creation initializes the site; this command starts its web server, realtime server, workers, scheduler and asset watcher. Run it again after recreating or restarting the container.

   Open `http://<FRAPPE_SITE_NAME>:<FRAPPE_HTTP_PORT>`. The defaults use [http://development.localhost:8000](http://development.localhost:8000); the sample `.env` above uses `http://my-app.localhost:8000`. Use the site's `.localhost` hostname so Frappe selects the correct site. Sign in as `Administrator` with the installer password (`admin` by default).

   These are Docker-published ports and work with the CLI and T3. `forwardPorts` in `devcontainer.json` provides additional VS Code forwarding; it is not a replacement for Docker publishing when using the CLI alone.

The ordered setup commands are in `development/.devcontainer/devcontainer.json` in the source template, or `.devcontainer/devcontainer.json` in a copied environment. `postCreateCommand` runs the existing installer first, then installs pre-commit into that Bench's Python environment, the GitHub/GitLab CLIs from the image's Debian package repositories, and the npm tools:

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

The npm packages use current releases, without version pins. T3 defaults to the stable `latest` npm channel. Set `FRAPPE_T3_CHANNEL=nightly` in `.devcontainer/.env` to bootstrap a new environment with `t3@nightly`. Creation resolves the current CLI release and completes its native-runtime download. Once initialized, the persistent T3 launcher state selects the running server version; recreation preserves that selection, including updates applied through T3. The CLI version can differ from the running server version.

`postStartCommand` prepares T3's persistent state and signals that provisioning is complete. The container's foreground entrypoint then runs T3's native managed launcher with the Bench's Python environment, including pre-commit. Docker supervises that process with an init process and an `unless-stopped` restart policy. T3 stays available when the setup terminal closes, and Docker restarts it if the launcher fails.

The launcher inherits the Compose service environment and activates the selected Bench's virtual environment. Configure server environment variables through Compose or T3's `service.env`; editor-only `remoteEnv` settings do not configure the container's foreground process.

After the managed server is ready, the hook registers the Bench with `t3 project add`. An already-registered project, including a title changed in T3, is retained. A pending T3 update is recovered before any project command opens its database. Attachment waits for managed-server readiness and project registration.

Use Dev Containers for initial creation and recreation so the installation hooks run. Once a container has been provisioned, ordinary Docker stop/start or restart also starts its launcher again. A per-container readiness marker prevents a newly recreated container from starting T3 before its tools and corporate CA setup are ready.

### Update T3 from a connected client

Use T3's **Update server** or **Update everything at once** action. T3 downloads the target runtime, restarts its server through the managed launcher, and rolls back a failed candidate. The container and the Bench installation are retained. Updates can interrupt agent turns and terminal commands; T3's continuation setting controls supported agent resumption.

The launcher owns `~/.t3/runtime/service-state.json`. Keep the complete `t3-data` volume: it includes the selected runtime, update-recovery state, projects, sessions and secrets. Startup preserves this file rather than resetting it to the npm package version. Runtime caches missing from the volume are restored through npm using the selected version.

This integration uses T3's internal `__service-launcher` entrypoint and launcher protocol 3. It runs T3's native compatibility preflight before initialization and startup, and rejects incompatible state without replacing it. If a future T3 release changes that interface, update the devcontainer integration while retaining its state volume. The foreground entrypoint only handles T3 lifecycle; Bench and npm installation remain in `devcontainer.json`.

## Frappe agent skills

Setup automatically installs all skills from [frappe/skills](https://github.com/frappe/skills) and the [Frappe UI skill](https://github.com/frappe/frappe-ui/tree/main/skills/frappe-ui) for Codex. This includes framework development, Frappe code review, code style, technical writing, UI design, and the repository's explicitly invoked workflows.

Skills are available across the Bench and individual app projects through `/home/frappe/.agents/skills`. The complete shared agent directory, including the skills CLI's registry metadata, is stored in `.agent-data/` inside your environment directory on the host. Keep that directory when recreating the container. Each environment has its own skills data.

The creation hook reinstalls the current upstream skills when a container is recreated; ordinary restarts keep the installed copies. Additional skills in other directories are retained. Keep changes to the bundled skills in your own skill repository because reinstalling refreshes their contents. The skill repositories are not copied into the Bench's application list and do not install Frappe apps.

After installation, the skills are available to the agent on its next turn. Their descriptions control automatic use; skills marked for explicit invocation, such as `ship-it` and `deep-app-audit`, still require you to request them.

## Check the environment

```sh
devcontainer exec --workspace-folder . sh -c 'bench --version && codex --version && t3 --version && gh --version && glab --version'
devcontainer exec --workspace-folder . sh -c '"/workspace/development/$FRAPPE_BENCH_NAME/env/bin/pre-commit" --version'
devcontainer exec --workspace-folder . curl --noproxy '*' --fail --silent --output /dev/null http://127.0.0.1:3773/
devcontainer exec --workspace-folder . python /usr/local/share/frappe-devcontainer/t3-entrypoint.py status
```

T3 starts with `/workspace/development/<FRAPPE_BENCH_NAME>` as its project directory and uses the Compose project name, such as `task-a_devcontainer`, as its initial title. Existing project titles are retained on subsequent starts, including any name you choose in T3. You can add individual application projects as well. Project registration and settings persist with the environment.

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

To restart the managed launcher, restart the devcontainer. Use T3's client controls to update its running server. Recreate the container to reinstall the other tools; its persistent T3 state and selected server version are retained.

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

The startup hook reads that file through a read-only mount and imports its preferences before T3 starts. Defaults for models, provider options, source-control writing, and thread behavior come from the local environment rather than T3's built-in defaults. Configure the source in your private template or preset for subsequent stacks; each new stack reads the current local preferences automatically.

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

T3 uses the Bench directory as its initial project and the Compose project name as the default display title. Rename the project in T3 to change that title; retain the environment folder name because it selects the Compose project and its persistent volumes. For Frappe development, use threads in the project's checkout and give parallel stacks separate environment directories and data.

For a corporate CA, add a private Compose override that mounts your certificate files and arrange for `sudo update-ca-certificates` to run before the network-dependent `postCreateCommand` steps. Configure the Node or Codex CA bundle when required by those clients. Keep company certificates and credentials in your local configuration or private repository.

## Recreate without losing state

```sh
devcontainer up --workspace-folder . --remove-existing-container
```

Compose keeps Codex state in `codex-data` and the complete T3 home in `t3-data`, scoped to the Compose project. This preserves authentication caches, provider sessions, T3 identity, threads, settings, and managed worktrees. The Bench stays in the mounted workspace, and the creation hook reinstalls the npm tools into the new container.

GitHub and GitLab CLI configuration is retained in their own volumes, and the creation hook reinstalls both CLI packages. When updating an existing configuration, recreate the container to apply the new mounts and environment variables.

Keep your environment directory and database volume as well. Removing volumes, renaming the environment folder, or deleting its files can disconnect existing state from its workspace. Back up persistent data separately; container recreation is not a backup operation. An active agent turn is interrupted when its container stops.

## Existing environments and template updates

Copying the template creates an independent environment; it does not move an existing Bench, database or T3 history. Existing devcontainers can continue using their current configuration. Do not copy an initialized environment over `development/`; its generated Bench and agent data would then be copied into every new environment.

Changes to `development/.devcontainer/` apply to future copies. To update an existing environment, apply the relevant configuration changes to its own `.devcontainer/`, retain its private overrides and settings, then recreate it from that environment directory.

When deliberately migrating an older layout, preserve the entire Bench, agent data, database and T3 volumes, keep the container Bench path unchanged, and explicitly retain its original `COMPOSE_PROJECT_NAME` in `.devcontainer/.env`. This prevents the new folder-derived name from selecting fresh volumes. Moving the source template alone does not migrate running environments.
