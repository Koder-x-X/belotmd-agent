"""Start the 3-player bot from this folder.

Uses this folder's code, not the 4-player package installed from belotmd.
Run it only when that other bot is stopped: both would log in as the same
account and knock each other off the table.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from belotmd.cli import main

if __name__ == "__main__":
    main()
