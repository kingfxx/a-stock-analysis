"""Explicit local asset locations; browser URLs remain stable."""
import json
from stock_analysis.paths import WEB_ROOT

ASSET_PATHS = json.loads((WEB_ROOT / "asset-map.json").read_text(encoding="utf-8"))


def asset_path(name):
    """Resolve a registered logical name; never accept an arbitrary disk path."""
    return WEB_ROOT / ASSET_PATHS[name]
