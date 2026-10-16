import os
import sys

if __package__ in (None, ""):  # run as a plain script, e.g. "python hoppla/__main__.py"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hoppla.cli import main
else:
    from .cli import main

sys.exit(main())
