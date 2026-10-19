"""Stand-in for ``journalctl``: ignores its arguments and replays a file.

Env: ``FAKE_JOURNAL`` file of JSON lines to print, ``FAKE_RC`` exit code,
``FAKE_STDERR`` text for stderr, ``FAKE_ARGS`` file to append argv to.
"""

import os
import sys
from pathlib import Path

args_file = os.environ.get("FAKE_ARGS")
if args_file:
    with Path(args_file).open("a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")
journal = os.environ.get("FAKE_JOURNAL")
if journal:
    sys.stdout.write(Path(journal).read_text())
    sys.stdout.flush()
stderr = os.environ.get("FAKE_STDERR")
if stderr:
    sys.stderr.write(stderr + "\n")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
