"""Generate an inspectable macOS LaunchAgent without embedding credentials."""
from __future__ import annotations

import plistlib
from pathlib import Path

LABEL = "com.seanspiesman.creality-agent"


def write_launch_agent(destination: Path, service_dir: Path, runtime_dir: Path) -> Path:
    service_dir, runtime_dir = service_dir.resolve(), runtime_dir.resolve()
    executable = service_dir / ".venv/bin/creality-agent"
    if not executable.is_file():
        raise ValueError("Install the service's local virtual environment first")
    data = {"Label": LABEL,
            "ProgramArguments": [str(executable), "--home", str(runtime_dir), "serve"],
            "WorkingDirectory": str(service_dir), "RunAtLoad": True,
            "KeepAlive": True, "ThrottleInterval": 10,
            "StandardOutPath": str(runtime_dir / "launch.stdout.log"),
            "StandardErrorPath": str(runtime_dir / "launch.stderr.log"),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and plistlib.loads(destination.read_bytes()) != data:
        raise ValueError("An existing different LaunchAgent must be reviewed before replacement")
    destination.write_bytes(plistlib.dumps(data))
    return destination
