"""Where the app finds its model, data and login state.

Every path defaults to a folder inside app/, so the app runs from wherever the folder is copied. Environment variables override
them (e.g. a mounted volume in a container):

  TWR_MODEL_DIR      model folder: manifest.json + the files it names           (default app/model)
  TWR_DATA_DIR       catalogue, popularity, training data, self-test bikes       (default app/data)
  TWR_STATE_DIR      users.json and .jwt_secret (created at first use)           (default app/)
  TWR_JWT_SECRET     token signing secret; overrides STATE_DIR/.jwt_secret
  TWR_TOKEN_MINUTES  login lifetime                                              (default 480)
  TWR_ADMIN_USER / TWR_ADMIN_PASSWORD   create this user at start-up when there are no users yet (first deployment)

To serve another model, replace the model folder (scripts/export_app_bundle.py writes it) and restart.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _dir(env: str, default: Path) -> Path:
    v = os.environ.get(env)
    return Path(v).expanduser().resolve() if v else default


MODEL_DIR = _dir("TWR_MODEL_DIR", APP_DIR / "model")
DATA_DIR = _dir("TWR_DATA_DIR", APP_DIR / "data")
STATE_DIR = _dir("TWR_STATE_DIR", APP_DIR)
TOKEN_MINUTES = int(os.environ.get("TWR_TOKEN_MINUTES", "480"))


@lru_cache(maxsize=1)
def manifest() -> dict:
    """The model folder's manifest.json: model name, file names, range entry, price band, data file names."""
    f = MODEL_DIR / "manifest.json"
    if not f.exists():
        raise RuntimeError(f"no model manifest at {f} - run scripts/export_app_bundle.py or set TWR_MODEL_DIR")
    return json.loads(f.read_text(encoding="utf-8"))


def data_file(key: str) -> Path:
    return DATA_DIR / manifest()["data"][key]
