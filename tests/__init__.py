import os
import sys
import tempfile

# All tests run against a throw-away data folder; the code folder is put on sys.path.
_TMP = tempfile.mkdtemp(prefix="nse_dash_test_")
os.environ["NSE_DASH_HOME"] = _TMP
os.environ["NSE_RETRY_BASE"] = "0"
os.environ.pop("NSE_PIPELINE_LOCKED", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
