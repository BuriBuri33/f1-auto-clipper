import sys
from pathlib import Path

# Tests import the package as `src.*`, so the repo root must be importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
