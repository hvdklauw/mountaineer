from importlib.resources import path
from pathlib import Path


def get_static_path(asset_name: str) -> Path:
    with path(__name__, "") as asset_path:
        return Path(asset_path) / asset_name.lstrip("/")
