# Implementation contract

Owner authorized implementation on 2026-10-06. Earlier planning-only notes do not prevent implementation, local dependency installation, synthetic-fixture slicing or targeted read-only qualification. Preserve all existing planning artifacts and manual projects. No firmware changes or blind physical starts.

Python package: agent_service/src/creality_agent. Persistent FastAPI service and MCP adapters share one SQLite job engine. Default bind 127.0.0.1:18088; authenticated LAN exposure is configurable. No camera pixels through API/MCP. Actual printer capability/camera/CFS qualification is separate from mocked protocol tests.

Initial modules and write ownership:
- Parent: config.py, models.py, store.py, service.py, api.py, mcp_server.py, cli.py, ingestion.py, printers.py, vision.py, core integration tests.
- Slicer worker: slicer.py and tests/test_slicer.py only. Slicer runs argv subprocess, no shell; output artifact and manifest checked, no print sends.
- Qualification worker: local ignored .runtime/fleet-discovery.json and sanitized .scratch/agent-printing/fleet-discovery-summary.md only; no printer connections, no camera collection or secret output.

Slicer interface: async slice_model(binary: Path, model: Path, output_dir: Path, settings: list[Path], filaments: list[Path], timeout_seconds: float = 300) -> dict. Return JSON-safe metadata including gcode_paths (absolute strings), elapsed_seconds, and warnings. Raise SliceError with sanitized reason on unsupported invocation, timeout, nonzero exit, no outputs or partial/invalid outputs. Reject 3MF plus external settings because CLI prohibits that combination. settings/filaments are service-controlled profile paths, not arbitrary flags. Child-process cancellation must terminate/wait/clean partial outputs. Slicer .3mf export is unsupported unless separately established; do not fake an editable project. Synthetic-fixture tests may use fake executables; any installed-binary probes are help/version or slicing only, no uploading/printing.

Run validation from agent_service with .venv/bin/python -m pytest and .venv/bin/ruff check. No worker stages files, commits, modifies GitHub or edits another owner's modules.
