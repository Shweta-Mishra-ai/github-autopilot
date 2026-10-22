"""Make the harness modules importable, and point them at a subject checkout.

The helpers under test only need the subject's PATTERNS table to import; the
checkout this file sits in is enough for that. A reproduction run pins the
subject to 38b2013 separately (reproduce.sh)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(os.path.dirname(HERE), "benchmarks")
REPAIR = os.path.join(os.path.dirname(HERE), "repair")
REPO = os.path.dirname(os.path.dirname(HERE))

os.environ.setdefault("SUBJECT_REPO", REPO)
sys.path.insert(0, BENCH)
sys.path.insert(0, REPAIR)
