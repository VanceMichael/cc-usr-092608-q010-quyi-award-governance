"""资格事实快照与校验辅助。

候选建立（轮次结论）与现场事件（换角）都需要冻结同一套资格事实，
集中在此实现，避免服务间循环依赖。
"""

from __future__ import annotations

from .models import (
    AwardCategory,
    CrewStatus,
    WorkRelation,
)
from .store import AwardStore


def build_qualification_snapshot(
    store: AwardStore,
    category: AwardCategory,
    work_id: str,
    version_id: str,
    person_id: str | None,
) -> dict:
    """在候选生成/换角时冻结资格事实，事后事实变更不影响该快照。"""
    work = store.work(work_id)
    version = work.get_version(version_id)
    snapshot: dict = {
        "category": category.value,
        "work_id": work_id,
        "version_id": version_id,
        "title": version.title,
        "text_hash": version.text_hash,
        "genre": work.genre,
        "originality_declared": version.originality_declared,
        "first_performed_at": version.first_performed_at,
        "author_ids": list(work.author_ids),
        "rights_holder_ids": list(work.rights_holder_ids),
        "submitting_org_id": work.submitting_org_id,
    }
    if person_id:
        person = store.person(person_id)
        snapshot["person_id"] = person_id
        snapshot["birth_date"] = person.age_proof.birth_date if person.age_proof else None
        snapshot["active_org_ids"] = sorted(person.active_orgs())
        performing = [
            a for a in person.appearances
            if a.work_id == work_id and a.relation == WorkRelation.PERFORMER
        ]
        snapshot["performed_work"] = bool(performing)
        snapshot["performance_active"] = any(
            a.status == CrewStatus.ACTIVE for a in performing
        )
        snapshot["prior_awards"] = [
            {"award": p.award, "category": p.category.value, "year": p.year}
            for p in person.prior_awards
        ]
        snapshot["regional_histories"] = [
            {"region": h.region, "year": h.year, "advanced": h.advanced}
            for h in person.regional_histories
        ]
    return snapshot
