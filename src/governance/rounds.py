"""轮次封存服务。

初评与分赛区结束时封存材料指纹集与晋级结论。封存记录为不可变快照，
任何后续证明都只能进入资格复核，不能回写封存。
"""

from __future__ import annotations

from datetime import datetime

from .records import Advancement, RoundSeal, RoundType
from .repository import AwardRepository
from .roles import GovernanceError, PermissionDenied, Role


class RoundService:
    def __init__(self, repo: AwardRepository) -> None:
        self.repo = repo

    def seal_round(
        self,
        seal_id: str,
        round_type: RoundType,
        round_name: str,
        sealed_by: str,
        sealed_at: datetime,
        actor_role: Role,
        material_fingerprints: dict[str, str],
        advancements: list[Advancement],
    ) -> RoundSeal:
        if actor_role is not Role.ORGANIZING_COMMITTEE:
            raise PermissionDenied("只有组委会可封存轮次")
        if seal_id in self.repo.seals:
            raise GovernanceError("封存记录已存在，轮次不得重复封存")
        if not material_fingerprints:
            raise GovernanceError("封存材料指纹集为空")
        for version_id in material_fingerprints:
            if version_id not in self.repo.versions:
                raise GovernanceError(f"封存包含未登记版本：{version_id}")
        names = {(seal.round_type, seal.round_name) for seal in self.repo.seals.values()}
        if (round_type, round_name) in names:
            raise GovernanceError("同一轮次不得二次封存")
        seal = RoundSeal(
            id=seal_id,
            round_type=round_type,
            round_name=round_name,
            sealed_at=sealed_at,
            sealed_by=sealed_by,
            material_fingerprints=dict(material_fingerprints),
            advancements=tuple(advancements),
        )
        self.repo.seals[seal_id] = seal
        return seal

    def advancement_records(self, round_type: RoundType | None = None) -> list[Advancement]:
        result: list[Advancement] = []
        for seal in self.repo.seals.values():
            if round_type is not None and seal.round_type is not round_type:
                continue
            result.extend(seal.advancements)
        return result
