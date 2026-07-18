import sys
from pathlib import Path

# Make live/ importable so `import core` works no matter where pytest runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
