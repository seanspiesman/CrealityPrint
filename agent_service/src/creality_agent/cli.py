from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .config import initialize, load_settings
from .store import Store


def default_home() -> Path:
    configured = os.environ.get("CREALITY_AGENT_HOME")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/CrealityAgent/runtime"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "creality-agent"


def main():
    parser = argparse.ArgumentParser(description="Local Creality agent printing service")
    parser.add_argument("--home", type=Path, default=default_home())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create private configuration and tokens; never prints secrets")
    sub.add_parser("serve", help="Run persistent authenticated API and Streamable HTTP MCP")
    approve = sub.add_parser("approve", help="Record an owner decision locally")
    approve.add_argument("job_id")
    approve.add_argument("action", choices=["resume"])
    probe = sub.add_parser("probe", help="Read-only Moonraker probe of one explicitly configured printer")
    probe.add_argument("printer_id")
    sub.add_parser("status", help="Print sanitized local job/qualification summary")
    args = parser.parse_args()
    initialize(args.home)
    if args.command == "init":
        print(f"Private configuration created in {args.home}; set import roots and qualify printers before use.")
    elif args.command == "serve":
        import uvicorn

        from .api import create_app
        settings = load_settings(args.home)
        uvicorn.run(create_app(args.home), host=settings.bind, port=settings.port, access_log=False)
    elif args.command == "approve":
        store = Store(args.home)
        try:
            job = store.get(args.job_id)
            if job["state"] != "paused":
                raise ValueError("Only a paused job may receive resume authorization")
            store.approve(args.job_id, args.action)
            print("Owner decision recorded. Resume still requires fresh observed printer state.")
        finally:
            store.close()
    elif args.command == "probe":
        from .printers import Moonraker

        settings = load_settings(args.home)
        printer = next((p for p in settings.printers if p.id == args.printer_id), None)
        if printer is None or not printer.endpoint:
            raise ValueError("Configure the selected printer endpoint before probing")
        status = asyncio.run(Moonraker(printer, read_only_probe=True).status())
        record = {"printer_id": printer.id, "read_only_protocol_observation": status,
                  "physical_identity_confirmed": printer.identity_confirmed,
                  "camera_cfs_nozzle_qualified": False}
        path = args.home / (printer.id + "-qualification.json")
        path.write_text(json.dumps(record, indent=2) + "\n")
        os.chmod(path, 0o600)
        print("Read-only observation recorded locally; physical identity and control remain unqualified.")
    elif args.command == "status":
        settings = load_settings(args.home)
        store = Store(args.home)
        try:
            print(f"Fleet members: {len(settings.printers)}; control-qualified: "
                  f"{sum(p.control_qualified for p in settings.printers)}; jobs: {len(store.list())}")
        finally:
            store.close()


if __name__ == "__main__":
    main()
