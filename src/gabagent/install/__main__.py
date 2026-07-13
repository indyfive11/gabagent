"""`python -m gabagent.install` → the workstation wizard (same entry as the `gabagent-install` script)."""
from __future__ import annotations

import sys

from gabagent.install.workstation import main

if __name__ == "__main__":
    sys.exit(main())
