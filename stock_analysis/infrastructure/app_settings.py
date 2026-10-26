"""Application file locations; preserve the original defaults."""
from __future__ import annotations

from stock_analysis.paths import PROJECT_ROOT
from pathlib import Path
from stock_analysis.infrastructure.storage import DEFAULT_DATABASE
from dataclasses import dataclass


@dataclass
class AppSettings:
    ROOT: Path = PROJECT_ROOT
    CACHE: Path = PROJECT_ROOT / 'data' / 'fundamentals'
    VALUATION_CACHE: Path = PROJECT_ROOT / 'data' / 'valuation'
    CHIP_CACHE: Path = PROJECT_ROOT / 'data' / 'chips'
    DATABASE_PATH: Path = DEFAULT_DATABASE
