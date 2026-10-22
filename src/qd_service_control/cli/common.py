"""Helpers shared by the command line tools."""

import sys
from pathlib import Path
from typing import Final

from qd_service_control.config.load import ConfigFileError, load_config
from qd_service_control.config.spec import StackSpec

EXIT_BAD_CONFIG: Final = 2


def load_spec(path: Path) -> StackSpec:
    """Load the config, or print its issues to stderr and exit 2."""
    try:
        return load_config(path)
    except ConfigFileError as err:
        for issue in err.issues:
            print(f"{err.file}: {issue}", file=sys.stderr)
        sys.exit(EXIT_BAD_CONFIG)
