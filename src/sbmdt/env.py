from pathlib import Path
from typing import Final

THIS_FILE: Final[Path] = Path(__file__).resolve()

SBMDT_BASE: Final[Path] = THIS_FILE.parent

SRC_BASE: Final[Path] = SBMDT_BASE.parent

PROJECT_BASE: Final[Path] = SRC_BASE.parent

DOCKERFILES_BASE: Final[Path] = PROJECT_BASE / 'dockerfiles'

__all__ = [
    'DOCKERFILES_BASE',
    'PROJECT_BASE',
]
