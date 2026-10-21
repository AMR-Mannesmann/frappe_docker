---
title: Use Agent Tools in a Devcontainer
---

# Use T3 Code and Codex in the development container

Use the existing Bench image with optional T3 Code and Codex tools installed by Dev Container lifecycle hooks. No custom image build is required. T3 runs inside the same container as Bench, so its agents can use the development environment directly.

You need Docker Compose and either the VS Code Dev Containers extension or the [Dev Container CLI](https://code.visualstudio.com/docs/devcontainers/devcontainer-cli). Start from the repository root and follow the [development setup](../05-development/01-development.md) for Bench and site creation.

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

3. Create `.devcontainer/.env` with a unique Compose project name and an available host port:

   ```dotenv
   COMPOSE_PROJECT_NAME=frappe-dev-a
   FRAPPE_T3_PORT=3774
   ```

   Reuse that project name when recreating this stack. Each parallel stack needs its own project name, source checkout, and host port. The container port remains `3773`; the published port listens on the host's loopback interface.

4. Create or recreate the environment:

   ```sh
   devcontainer up --workspace-folder . --remove-existing-container
   ```

   In VS Code, use **Dev Containers: Reopen in Container** for a new environment or **Dev Containers: Rebuild Container** when enabling agents in an existing one. Reopening an existing container alone does not apply the new volumes, environment, and published port.

On creation, the hooks preserve the existing pre-commit installation and install pinned native Codex and T3 distributions. On each Dev Container start, they restore the launchers and start one T3 server. The normal development configuration does not install or start these tools unless the agents overlay is enabled.

Use Dev Containers to create and resume the environment. Plain `docker compose up` or `docker restart` does not execute Dev Container lifecycle hooks.

## Check the environment

```sh
devcontainer exec --workspace-folder . sh -c 'bench --version && codex --version && t3 --version'
devcontainer exec --workspace-folder . python /workspace/development/agent_tools.py status
```

T3 starts with `/workspace/development` as its project directory; you can add your Bench or application projects once they exist. Project registration and settings persist with the environment.

For the initial browser connection, mint a short-lived pairing link in your own terminal:

```sh
devcontainer exec --workspace-folder . t3 pair
```

The printed link uses the container's address and port. Replace its origin with `http://127.0.0.1:3774`, using your selected host port, and preserve the complete `/pair#token=...` suffix. Open that link on the Docker host. When adding this environment to another T3 client, use the host's reachable endpoint and the pairing token. A bare URL has no pairing credential, and the printed container-address QR code does not account for Docker port mapping.

T3 startup output is stored in `/home/frappe/.t3/devcontainer/server.log` with private file permissions. It can contain pairing credentials; read it only in your own terminal when connecting a client.

To restart the managed T3 process without recreating the container:

```sh
devcontainer exec --workspace-folder . python /workspace/development/agent_tools.py stop
devcontainer exec --workspace-folder . python /workspace/development/agent_tools.py start
```

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

## Configure versions and trust

The first version pair is Codex `0.163.0` and T3 `0.1.1`. Override `FRAPPE_CODEX_VERSION` or `FRAPPE_T3_VERSION` in `.devcontainer/.env` with an exact release version, then recreate the devcontainer. The version's official installer must support the native distribution layout. Installers are fetched from the corresponding release tags, and verify their release downloads.

Set `FRAPPE_T3_PROJECT_DIR` to another existing container directory to register that directory on startup. Keep paths stable when recreating a stack.

For a corporate CA, add a private Compose override that mounts your certificate files and arrange for `sudo update-ca-certificates` to run before the network-dependent `postCreateCommand` steps. Configure the Node or Codex CA bundle when required by those clients. Keep company certificates and credentials in your local configuration or private repository.

## Recreate without losing state

```sh
devcontainer up --workspace-folder . --remove-existing-container
```

Compose keeps Codex state in `codex-data` and the complete T3 home in `t3-data`, scoped to the Compose project. This preserves authentication caches, provider sessions, T3 identity, threads, settings, and managed worktrees. The hooks recreate the executable launchers outside those volumes, leaving Bench's existing executables available.

Keep your workspace checkout and database volume as well. Removing volumes, changing the Compose project name, or deleting the checkout can disconnect existing state from its files. Back up persistent data separately; container recreation is not a backup operation. An active agent turn is interrupted when its container stops.
