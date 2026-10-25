"""Stable project locations, independent of domain module depth."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
WEB_ROOT = PROJECT_ROOT / "web"
