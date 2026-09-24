"""Helper for the instance-collision tests.

Opens a workspace in this process and holds it, so the test can prove what a
*second copy of Grande* sees. DuckDB's file lock is held per process, so this
cannot be demonstrated from within the test's own process.

Usage: python -m tests._hold_workspace <workspace-dir> <ready-file>
"""

import pathlib
import sys
import time

from grande.engine.session import Workspace


def main() -> int:
    directory, ready = sys.argv[1], sys.argv[2]
    workspace = Workspace(directory)
    try:
        pathlib.Path(ready).write_text("1", encoding="utf-8")
        time.sleep(60)          # the parent kills us when it is done
    finally:
        workspace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
