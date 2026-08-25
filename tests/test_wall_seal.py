"""Closes the same hole script-writer/tests/test_wall_seal.py closed there: the Wall's
Code-layer scan (`wall/seal.py`, check 1 - Code stays blind, docs/adr/0003) only ever ran
by hand against the whole estate, so a stray business name landing in this repo's shared
code could sit unseen until a weekly job or a different repo's suite happened to catch it.

This file runs that same scan, scoped to this repo, as part of metrics's OWN suite, so the
next stray house name is caught in the same test run it lands in rather than on whatever
day someone next happens to run the wall by hand. This is the workspace rule stated
plainly: a guard belongs in the suite that runs at the speed of the work, not only on a
calendar.
"""

from __future__ import annotations

import sys
from pathlib import Path

# wall/ is a sibling of metrics/ at the workspace root and is not an installed package, so
# it is never importable by name alone. Put it on sys.path the same way wall/conftest.py
# does for its own suite, so this file can `import seal` (the Wall's own module name)
# without needing PYTHONPATH set for it.
_WALL_DIR = Path(__file__).resolve().parent.parent.parent / "wall"
if str(_WALL_DIR) not in sys.path:
    sys.path.insert(0, str(_WALL_DIR))

import seal  # noqa: E402 -- must follow the sys.path insert above


def test_metrics_names_no_business_over_its_wall_ceiling():
    """Check 1, scoped to just this repo: metrics's burn-down ceiling in
    `wall/code_seal.yaml` (0 - this tool has never legitimately named a business) may
    only fall, never rise, and any business-name mention is a breach. Calls the Wall's
    own scan function directly (no shelling out to `seal.py` as a subprocess) so a broken
    import or a raised `SealError` surfaces as a normal test failure rather than a
    silently-green skipped step."""
    findings = seal.check_code_layer_blind()
    ours = [finding for finding in findings if finding.house == "metrics"]
    assert ours == [], "\n\n".join(finding.message for finding in ours)
