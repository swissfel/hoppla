"""Entry script for PyInstaller builds."""
import os
import sys

# Copies of ourselves that we start (background session, askpass via ssh, terminal)
# must unpack on their own instead of reusing this process' temporary directory.
os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

from hoppla.cli import main  # noqa: E402

sys.exit(main())
