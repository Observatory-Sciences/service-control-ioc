"""Minimal systemd ``sd_notify``: a datagram to ``NOTIFY_SOCKET``."""

import os
import socket
from collections.abc import Mapping

_SEND_TIMEOUT = 0.5


def sd_notify(state: str, environ: Mapping[str, str] = os.environ) -> bool:
    """Send ``state`` to systemd.

    Return ``False`` when ``NOTIFY_SOCKET`` is unset or the send fails, so an
    unreachable socket never takes the service down.
    """
    address = environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    target: str | bytes = address
    if address.startswith("@"):
        target = b"\0" + address[1:].encode()  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(_SEND_TIMEOUT)  # a full queue must not stall the loop
            sock.sendto(state.encode(), target)
    except OSError:
        return False
    return True
