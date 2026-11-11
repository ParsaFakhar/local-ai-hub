"""Local AI Hub test suite (stdlib unittest).

    python3 -m unittest discover -s tests -v
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
