"""Pin explicitly configured printer/camera endpoints to LAN addresses."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from .models import ServiceError

NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]


def pin_lan(url: str, *, allow_tailnet: bool = False) -> tuple[str, str, str]:
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or
                parts.fragment):
            raise ValueError()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname.encode("idna").decode("ascii")
        addresses = {a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        networks = NETWORKS + ([ipaddress.ip_network("100.64.0.0/10")] if allow_tailnet else [])
        if not addresses or any(not any(ipaddress.ip_address(a) in n for n in networks) for a in addresses):
            raise ValueError()
        address = min(addresses)
        bracketed = f"[{address}]" if ":" in address else address
        netloc = f"{bracketed}:{port}"
        pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
        host_header = f"[{host}]" if ":" in host else host
        return pinned, f"{host_header}:{port}", host
    except (OSError, ValueError, UnicodeError):
        raise ServiceError("Configured endpoint must resolve exclusively to a LAN address") from None
