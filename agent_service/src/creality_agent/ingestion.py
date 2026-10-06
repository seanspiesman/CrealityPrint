"""Bounded ingestion. Public downloads pin vetted DNS results to the connection."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import shutil
import socket
import ssl
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

from .config import Policy
from .models import ServiceError

FORMATS = {".stl", ".obj", ".3mf"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def public_target(url: str) -> tuple[str, int, str, str]:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ServiceError("Use a public HTTP(S) model URL without embedded credentials", 422)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname.encode("idna").decode("ascii")
        if port not in {80, 443}:
            raise ValueError()
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = sorted({a[4][0] for a in answers})
        if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
            raise ValueError()
    except (OSError, ValueError, UnicodeError):
        raise ServiceError("Download target is unavailable or is not a public address", 422) from None
    return host, port, addresses[0], parts.scheme


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str):
        super().__init__(host, port, timeout=20, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def download(url: str, destination: Path, policy: Policy) -> str:
    for _ in range(4):
        host, port, address, scheme = public_target(url)
        parts = urlsplit(url)
        connection = PinnedHTTPS(host, port, address) if scheme == "https" else http.client.HTTPConnection(
            address, port, timeout=20)
        try:
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            host_header = f"[{host}]" if ":" in host else host
            connection.request("GET", target, headers={"Host": host_header, "User-Agent": "CrealityAgent/0.1"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ServiceError("Download redirect has no destination", 422)
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ServiceError("Public download unavailable; supply the model file", 422)
            length = response.getheader("Content-Length")
            if length and int(length) > policy.max_download_bytes:
                raise ServiceError("Download exceeds configured size limit", 422)
            total = 0
            with destination.open("xb") as out:
                while block := response.read(64 * 1024):
                    total += len(block)
                    if total > policy.max_download_bytes:
                        raise ServiceError("Download exceeds configured size limit", 422)
                    out.write(block)
            if not total:
                raise ServiceError("Downloaded file is empty", 422)
            suffix = Path(unquote(parts.path)).suffix.lower()
            if suffix not in FORMATS | {".zip"}:
                raise ServiceError("URL must identify STL, OBJ, 3MF or ZIP; otherwise supply a local file", 422)
            return suffix
        except ServiceError:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, http.client.HTTPException):
            destination.unlink(missing_ok=True)
            raise ServiceError("Public download failed; supply the model file", 422) from None
        finally:
            connection.close()
    raise ServiceError("Too many download redirects", 422)


def import_file(path: str, destination: Path, roots: list[str], policy: Policy) -> str:
    try:
        source = Path(path).expanduser().resolve(strict=True)
        allowed = any(source.is_relative_to(Path(root).expanduser().resolve()) for root in roots)
        if not allowed or not source.is_file():
            raise ServiceError("File is outside operator-configured import roots", 422)
        if source.stat().st_size > policy.max_download_bytes:
            raise ServiceError("Model exceeds configured size limit", 422)
        if source.suffix.lower() not in FORMATS | {".zip"}:
            raise ServiceError("Unsupported model format", 422)
        # Opened bytes remain bounded even if the source grows during copying.
        with source.open("rb") as src, destination.open("xb") as out:
            total = 0
            while block := src.read(64 * 1024):
                total += len(block)
                if total > policy.max_download_bytes:
                    raise ServiceError("Model exceeds configured size limit", 422)
                out.write(block)
        if total == 0:
            raise ServiceError("Model is empty", 422)
        return source.suffix.lower()
    except ServiceError:
        destination.unlink(missing_ok=True)
        raise
    except OSError:
        destination.unlink(missing_ok=True)
        raise ServiceError("Local model cannot be imported", 422) from None


def validate_archive(path: Path, policy: Policy):
    try:
        with zipfile.ZipFile(path) as z:
            entries = z.infolist()
            if len(entries) > policy.max_files or sum(e.file_size for e in entries) > policy.max_expanded_bytes:
                raise ServiceError("Archive exceeds file-count or expanded-size limit", 422)
            names = set()
            for e in entries:
                name = PurePosixPath(e.filename)
                if (name.is_absolute() or ".." in name.parts or "\\" in e.filename or
                        "\x00" in e.filename or e.flag_bits & 1 or e.filename in names or
                        (e.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ServiceError("Unsafe or encrypted archive entry", 422)
                names.add(e.filename)
    except zipfile.BadZipFile:
        raise ServiceError("Invalid archive/project", 422) from None


def extract_models(path: Path, output: Path, policy: Policy) -> list[Path]:
    validate_archive(path, policy)
    output.mkdir()
    models = []
    total = 0
    try:
        with zipfile.ZipFile(path) as z:
            for entry in z.infolist():
                if entry.is_dir() or Path(entry.filename).suffix.lower() not in FORMATS:
                    continue
                target = output / entry.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as src, target.open("xb") as dst:
                    while block := src.read(64 * 1024):
                        total += len(block)
                        if total > policy.max_expanded_bytes:
                            raise ServiceError("Expanded files exceed limit", 422)
                        dst.write(block)
                if target.suffix.lower() == ".3mf":
                    validate_archive(target, policy)
                models.append(target)
        if not models:
            raise ServiceError("Archive contains no supported models", 422)
        return models
    except Exception:
        shutil.rmtree(output)
        raise
