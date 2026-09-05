import sys
from pathlib import Path

# Tests import `app.*`; make the backend package root importable regardless
# of where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
