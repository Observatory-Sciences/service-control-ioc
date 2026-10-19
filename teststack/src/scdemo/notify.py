"""Minimal systemd ``sd_notify``: a datagram to ``NOTIFY_SOCKET``."""

import os
import socket
from collections.abc import Mapping


def sd_notify(state: str, environ: Mapping[str, str] = os.environ) -> bool:
    """Send ``state`` to systemd. Return ``False`` when ``NOTIFY_SOCKET`` is unset."""
    address = environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]  # abstract namespace
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(state.encode(), address)
    return True
