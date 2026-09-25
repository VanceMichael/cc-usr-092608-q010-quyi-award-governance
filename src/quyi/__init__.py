"""曲艺评奖档案与裁定服务。"""

from __future__ import annotations

import importlib.metadata

from .archive_service import ArchiveService
from .appeal_service import AppealService
from .award_service import AwardService
from .jury_service import JuryService
from .round_service import RoundService

__all__ = [
    "ArchiveService",
    "RoundService",
    "JuryService",
    "AppealService",
    "AwardService",
]

try:
    __version__ = importlib.metadata.version("quyi-award-governance")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - 本地源码运行
    __version__ = "0.2.0"
