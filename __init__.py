"""The modules import each other by plain name, as when run from inside this folder (uvicorn main:app). This lets the folder
also run as a package from its parent (python -m uvicorn app.main:app, python -m app.selftest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
