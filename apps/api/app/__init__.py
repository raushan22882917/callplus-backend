import sys
from pathlib import Path

# The shared `packages` package lives at the repo root, one level above apps/api. Make it
# importable however the app is launched (uvicorn --app-dir apps/api, celery, alembic).
_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
