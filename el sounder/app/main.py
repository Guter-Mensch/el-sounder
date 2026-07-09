"""
app/main.py  –  el sounder

The one door in. Everything else happens in bootstrap.py.

    python -m app.main [--list-devices] [--device INDEX]
"""

import sys
import os

# Allow running from project root or from app/ subdirectory
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.bootstrap import bootstrap


def main() -> None:
    app, overlay = bootstrap()
    exit_code = app.exec()

    # Cleanup
    capture = getattr(overlay, "_capture", None)
    if capture:
        try:
            capture.close()
        except Exception:
            pass

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
