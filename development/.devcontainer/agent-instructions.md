<!-- frappe-devcontainer:begin -->

## Frappe development container

`frappectl` (also `fr`) is installed and automatically authenticates to this container's development site as Administrator. Run `frappectl guide` before using it. Use it for inspecting DocTypes, querying or preparing example documents, running reports, and checking HTTP API behavior. For example:

```sh
frappectl doctype show ToDo --json
frappectl doc list ToDo -f status=Open --json
```

Its site URL is `http://$FRAPPE_SITE_NAME:8000`, using the container port. It requires the web server: run `bench start` from `/workspace/development/$FRAPPE_BENCH_NAME` when needed, checking first whether it is already running. Use the same URL in the shared browser to check the UI. `frappectl auth whoami` confirms the target and user; the guide's OAuth login advice is for additional sites, since local access is already configured.

Use Bench and its Python environment for migrations, tests, debugging internals, and complex setup. Administrator access does not demonstrate that another user's permissions work; use explicit credentials or a named CLI profile for those checks. `frappectl -s <profile>` and explicitly supplied `FRAPPE_SITE`, `FRAPPE_API_KEY`, and `FRAPPE_API_SECRET` bypass the automatic local credentials.

The private credential file at `sites/$FRAPPE_SITE_NAME/.frappectl.env` is managed by the creation hook. Do not print, commit, or copy its credentials into commands or logs. No interactive CLI login is needed for the local development site.

<!-- frappe-devcontainer:end -->
