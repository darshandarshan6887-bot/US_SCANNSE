"""Delete the ~3,000 old child-*/ folders (one duplicate HTML page per stock).

They are replaced by the single page stock/index.html?symbol=XXX.
    python cleanup_legacy_child_pages.py          # asks first
    python cleanup_legacy_child_pages.py --yes
Only folders named child-* that contain nothing but index.html are removed.
"""
import argparse
import shutil
import sys

import config

ap = argparse.ArgumentParser()
ap.add_argument("--yes", action="store_true")
a = ap.parse_args()
dirs = [d for d in config.BASE_DIR.glob("child-*") if d.is_dir() and {p.name for p in d.iterdir()} <= {"index.html"}]
print(f"{len(dirs)} legacy child folders found in {config.BASE_DIR}")
if dirs and (a.yes or input("Delete them? [y/N] ").strip().lower() == "y"):
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    print("Deleted.")
