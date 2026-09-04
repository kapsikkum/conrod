"""Keep the tests out of the photographer's own Conrod.

conrod.config picks its data directory at import time, so without this
every test that logs a line -- the rate-limit tests, the reader-resilience
tests -- appended to ~/.conrod/conrod.log. That is the file someone reads
when a scan has gone wrong, and it had grown to 81 MB of "Test returned
500" and "RuntimeError: boom" interleaved with the real thing. Diagnosing
an actual failure meant grepping the test suite out of the evidence first.

Set before the first conrod import, because DATA_ROOT is read once.
"""

from __future__ import annotations

import os
import tempfile

if not os.environ.get("CONROD_HOME"):
    # Not a TemporaryDirectory: it would be cleaned up when this module's
    # locals went out of scope, part way through the run. The OS clears it.
    os.environ["CONROD_HOME"] = tempfile.mkdtemp(prefix="conrod-tests-")
