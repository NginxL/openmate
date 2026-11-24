"""The repository's tests/_runtmp.py: every temporary directory of this run in one directory, removed at its end."""

import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, os.pardir, "tests",
                            "_runtmp.py"))
