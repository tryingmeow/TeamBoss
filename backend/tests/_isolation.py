"""Import this first in every test module.

app.database falls back to backend/data when AUTO_TEAM_DATA_DIR is unset, and
on a deployed box that directory is the live database. Importing this module
points the data dir at a throwaway temp directory so a test run can never
touch it. An explicitly set AUTO_TEAM_DATA_DIR is respected, unless it
resolves to backend/data itself.
"""
import atexit
import os
import shutil
import tempfile

_PROD_DATA = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
)


def _isolate() -> None:
    current = os.environ.get("AUTO_TEAM_DATA_DIR")
    if current and os.path.realpath(current) != _PROD_DATA:
        return
    tmp = tempfile.mkdtemp(prefix="teamboss-test-")
    os.environ["AUTO_TEAM_DATA_DIR"] = tmp
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)


_isolate()
