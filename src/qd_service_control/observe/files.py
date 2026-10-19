"""Probe for the files a service is expected to produce."""

import os


def probe_file(path: str, non_empty: bool) -> bool:
    """Return True if ``path`` exists as a file and, if ``non_empty``, has content."""
    try:
        size = os.stat(path).st_size
    except OSError:
        return False
    return size > 0 if non_empty else True
