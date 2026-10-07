# Creality Agent Service

A persistent local job service with an authenticated REST API and standard MCP tools. The custom app includes a local model conversation workspace; external agents can use the same guarded tools. Jobs, inputs, conversations and alerts survive agent disconnection. Printer/camera endpoints and secrets stay in ignored local configuration.

## Current capability

Implemented: SQLite jobs/events and request replay; local/direct-public model ingestion; STL/OBJ/3MF metadata; variant selection; CLI slicing worker; authenticated REST, stdio MCP and Streamable HTTP MCP; configured Moonraker upload/control/status adapter; restart reconciliation; concurrent printer monitoring; local detector integration and owner-gated resume; local model orchestration; owner-only desktop enrollment and decisions; durable alerts and macOS notification delivery. Camera pixels are never included in tools/API results. No raw G-code execution tool is exposed.

Six fleet entries are initialized (one K1 Max, three K1 SE, two Hi), all unqualified. They are not populated from unverified cache rows. `GET /v1/capabilities` reports implemented and unavailable adapters separately. Existing printer firmware is the baseline.

**Physical automation remains held until exact hardware, profile, native project export/preflight, camera freshness, local detectors and control behavior are qualified.** CFS control, repository search adapters and CAD generation are still implementation work; do not interpret listed tools as live fleet qualification. The primitive local image comparison can identify changes but deliberately cannot claim an empty bed. Use a qualified local detector for clearance/failure decisions.


## Custom app workspace

Build with `CREALITY_LOCAL_AGENT=ON` and bundle identifier `com.seanspiesman.crealityprint.localagent`. The existing chat panel loads bundled `resources/web/local_agent` content and replaces the cloud chat bridge with a finite native owner interface. The page has no network access; native C++ reads `owner.token` and calls loopback operator routes without exposing either service token to JavaScript or the model. Downloaded/model text is rendered as text.

The workspace includes conversations, job history/queue/progress, imported-file candidate selection, holds, operator resume/budget decisions, project inspection, printer/camera/reference/CFS enrollment and local model/profile settings. Polling preserves unsaved form input. Model endpoints may use loopback, private LAN or explicitly owner-configured Tailscale addresses; camera and printer operations remain on the LAN, and camera analysis remains loopback on this Mac.

The native preparation helper uses a separate GUI process and fresh private app data, bypasses single-instance forwarding, and never uses the main app's Plater. macOS preparation and inspection run under a network-denying process sandbox. Preparation accepts one STL/OBJ plus flattened local machine/process/filament profiles and writes a native 3MF with an atomic result manifest. The service independently checks source/profile hashes, mesh geometry and effective settings, then re-opens and re-slices the saved project without external profiles. Unsupported geometry transforms, imported 3MF preparation, overrides, copies and automated repair/orientation/arrangement remain explicit holds. Physical fit/process preflight remains unqualified; no successful export alone enables a print.

Build and package separately without modifying `/Applications/Creality Print.app`:

```sh
DEPS_ENV_DIR=/absolute/path/to/dependency-prefix agent_service/scripts/build_local_agent_app.sh
agent_service/scripts/install_local_agent_app.sh '/absolute/path/Creality Print Local Agent.app'
```

The installer refuses existing destinations and defaults to `~/Applications/Creality Print Local Agent.app`. The custom build uses a separate data version. Configure `gui_helper_binary` and `slicer_binary` in the private service configuration to the separately built executable after native acceptance. See [APP_CONTRACT.md](APP_CONTRACT.md) for the finite owner interface and helper manifests. macOS notifications report accepted delivery requests; permission and actual display require live verification. Durable in-app alert history remains available independently of Notification Center.

## Run

Python 3.11+; dependencies are isolated in this directory.

```sh
UV_CACHE_DIR=/private/tmp/creality-uv-cache uv venv .venv
UV_CACHE_DIR=/private/tmp/creality-uv-cache uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/creality-agent init
.venv/bin/creality-agent serve
```

Default macOS runtime: `~/Library/Application Support/CrealityAgent/runtime`, mode 0700. Its configuration and bearer tokens are mode 0600. Default API: `http://127.0.0.1:18088`; Streamable HTTP MCP: `http://127.0.0.1:18088/mcp/`. The health route is public; every operational route requires a bearer token from `agent.token`. Do not commit or paste tokens in chat, example files or logs. Development fixtures use an explicit ignored `agent_service/.runtime`; do not run a second server against the installed runtime.

Use `CREALITY_AGENT_HOME` or `creality-agent --home <directory>` to choose another runtime. Only one running service can own a runtime. Operator-configured `import_roots` restrict filesystem import. Network downloads reject non-public destinations, embedded credentials, unsafe redirects/archives and excessive resource use. Printer/camera connections are pinned to explicitly configured LAN addresses and never use Creality Cloud.

For authorized LAN clients, configure `bind`, `public_base_url`, and any browser `allowed_origins` deliberately; supply the token through the client secret store. An address reachable by clients belongs in `public_base_url`. Origin validation and MCP Host validation apply. Avoid publicly exposing this physical-control service. Token rotation requires replacing its private token file and restarting the service; individually scoped client tokens are not yet implemented.

## Connect an MCP agent

Stdio configuration (replace absolute paths for your checkout; the adapter calls the already-running service):

```json
{
  "mcpServers": {
    "creality-print": {
      "command": "/Users/sean/Library/Application Support/CrealityAgent/.venv/bin/creality-agent-mcp",
      "env": {
        "CREALITY_AGENT_HOME": "/Users/sean/Library/Application Support/CrealityAgent/runtime",
        "CREALITY_AGENT_API_URL": "http://127.0.0.1:18088"
      }
    }
  }
}
```

A remote agent can instead use the authenticated `/mcp/` endpoint or the REST/OpenAPI contract. Client compatibility must be checked in the actual owner application. A Qwen model server alone is not an MCP host; the orchestration client calls tools.

Tools: `capabilities`, `list_printers`, `get_printer_status`, `list_profiles`, `list_jobs`, `get_job`, `events`, `request_owner_input`, `list_questions`, `create_job`, `select_model`, `prepare_job`, `queue_job`, `start_job`, `pause_job`, `cancel_job`, `resume_job`. List tools return an `items` array. Every mutation requires an idempotency key; use a new UUID per intended action and reuse it only when retrying identical input.

Create a job with a supplied URL or allowed local path. Poll until acquisition completes, then inspect measured dimensions, unit assumptions and variants. A multiple-file archive holds for selection. Prepare with an operator-verified profile. Setting overrides and multiple copies hold if they cannot be applied faithfully; they are never silently ignored. Imported 3MF settings require native normalization rather than executing embedded G-code.

An API success for a state-changing printer command is not print-state proof. The service persists start intent before upload, checks readiness again after upload and requires an observed matching file/state. Uncertain starts remain held and monitored for reconciliation; retrying cannot send a second start. Never mark an ambiguous physical job canceled locally to discard its ownership record.

## Qualification and local operator decisions

Add only a physically identified printer's LAN origin in `.runtime/config.json`. A read-only protocol observation can then be recorded with:

```sh
.venv/bin/creality-agent probe k1-max-1
```

This sends only documented read-only Moonraker status queries. It does not enable upload/control or certify the camera, CFS, nozzle or physical identity. Stock Creality interfaces require a different adapter if Moonraker is absent. Do not change firmware to make a guessed interface work.

Camera configuration needs a verified printer association, an operator-enrolled local reference/ROI and usable source capture-time or sequence metadata. HTTP response time is not frame capture time. Camera and local detector endpoints are private operator configuration. Failure and clearance detectors must be qualified per view; detector URLs use loopback only. Threshold and test evidence cannot be supplied by an agent declaring its own confidence.

The owner grants resume through a local operator action after examining the problem:

```sh
.venv/bin/creality-agent approve <job-id> resume
```

Model tools and the agent API credential cannot write owner approvals. The native app uses separate loopback-only operator routes and its private owner credential; owner actions are never advertised as model tools. The local approval is consumed by one resume attempt. A CLI invocation is an operator authority boundary, not protection against an agent separately granted unrestricted shell/filesystem access.

The confirmed defaults are 8 hours and 250 grams. Monitoring loss notifies the owner, continues the current print, and holds new starts until monitoring recovers. A validated failure requests a pause; resuming requires a new owner decision. Budget overrides bind the exact artifact hashes and estimates and are consumed by one start attempt. Unknown material/color, insufficient filament, CFS mapping, unresolved slicer warnings or missing native project/preflight also hold. Fixture tests do not qualify physical control or vision. Read-only K1 Max status discovery is recorded separately; no firmware changes or physical operations are part of these checks.

## macOS startup and tests

`launch_agent.write_launch_agent` generates a plist for `com.seanspiesman.creality-agent`; it contains paths, not tokens, and refuses to replace a different existing plist. The owner's login service is installed under `~/Library/Application Support/CrealityAgent` with a non-editable package and separate private runtime. The LaunchAgent lives in `~/Library/LaunchAgents`. A LaunchAgent using the checkout's virtual environment under Documents failed with macOS `Operation not permitted`; the installed package avoids that location. Imports from protected folders may still require macOS access or an owner-supplied file in an accessible import root. Source edits require reinstalling the package and restarting this service; they do not update the running installation automatically. Active preparation/printing prevents idle system sleep through `caffeinate -i`; display sleep is permitted. Login startup is not availability while powered off or logged out.

The included [MCP configuration](examples/mcp-config.json) points to this installation without embedding a token. Runtime dependencies are pinned with hashes in `requirements.lock`.

```sh
.venv/bin/python -m pytest -q tests
.venv/bin/ruff check src tests
```

Tests include fixture parsers, process failures/cancellation, archive/URL boundaries, authentication, idempotent import, restart holds, owner-gated resume, real MCP SDK tool calls and mocked printer transitions. These are not physical-print acceptance. Ignored runtime smoke results record actual local CLI slicing and API/MCP transport checks separately.
