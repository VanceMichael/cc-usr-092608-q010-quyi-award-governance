"""测试用服务装配与流程便捷函数。"""

from __future__ import annotations

from types import SimpleNamespace
from datetime import date

from src.quyi.archive_service import ArchiveService
from src.quyi.appeal_service import AppealService
from src.quyi.award_service import AwardService
from src.quyi.jury_service import JuryService
from src.quyi.live_event_service import LiveEventService
from src.quyi.models import AwardCategory, RoundKind
from src.quyi.publicity_service import PublicityService
from src.quyi.round_service import RoundService
from src.quyi.store import seed_store

SEC = "sec-zhang"
ORG = "org-chen"
REVIEWER = "reviewer-zhao"
OFFICER = "officer-li"

REQUIRED_MATERIALS = ("报名表", "文本", "原创声明", "权利人证明")
REGION_MATERIALS = REQUIRED_MATERIALS + ("参赛确认",)


def build_services() -> SimpleNamespace:
    store = seed_store()
    archive = ArchiveService(store)
    rounds = RoundService(store)
    jury = JuryService(store, archive)
    live = LiveEventService(store, archive, jury)
    appeals = AppealService(store)
    awards = AwardService(store)
    publicity = PublicityService(store)
    return SimpleNamespace(
        store=store,
        archive=archive,
        rounds=rounds,
        jury=jury,
        live=live,
        appeals=appeals,
        awards=awards,
        publicity=publicity,
    )


def add_standard_materials(rounds, round_id: str, categories=REQUIRED_MATERIALS) -> None:
    for index, category in enumerate(categories):
        rounds.add_material(
            SEC, round_id, f"{round_id}-doc-{index}", f"{round_id}-hash-{index}", category
        )


def create_sealed_round(
    svc: SimpleNamespace,
    round_id: str,
    kind: RoundKind,
    name: str,
    *,
    region: str | None = None,
    candidacy_specs: list[tuple] | None = None,
) -> str:
    """创建并封存一个轮次。

    candidacy_specs 形如：
    (candidacy_id, AwardCategory, work_id, version_id, person_id|None, advanced)
    """
    svc.rounds.create_round(ORG, round_id, kind, name, region=region, year=2026)
    categories = REGION_MATERIALS if kind == RoundKind.REGIONAL else REQUIRED_MATERIALS
    add_standard_materials(svc.rounds, round_id, categories)
    for cid, category, work_id, version_id, person_id, advanced in (
        candidacy_specs or []
    ):
        svc.rounds.record_advancement(
            SEC, round_id, cid, category, work_id, version_id, person_id, advanced
        )
    svc.rounds.seal_round(SEC, round_id)
    return round_id


def register_judge(
    svc: SimpleNamespace,
    judge_id: str,
    person_id: str,
    name: str,
    *,
    org_id: str | None = None,
    round_id: str | None = None,
) -> str:
    svc.jury.register_judge(ORG, judge_id, person_id, name, org_id=org_id)
    if round_id:
        return svc.jury.issue_alias(ORG, judge_id, round_id)
    return ""


def final_candidacies(svc: SimpleNamespace, round_id: str = "R-FIN") -> None:
    """W-001 进入现场终评的三类候选（需已有封存轮次）。"""
    svc.rounds.create_round(ORG, round_id, RoundKind.FINAL, "现场终评", year=2026)
    svc.rounds.record_advancement(
        SEC, round_id, "C-FIN-PERF", AwardCategory.PERFORMANCE,
        "W-001", "W-001-V1", "P-002", True,
    )
    svc.rounds.record_advancement(
        SEC, round_id, "C-FIN-NEW", AwardCategory.NEWCOMER,
        "W-001", "W-001-V1", "P-003", True,
    )
    svc.rounds.record_advancement(
        SEC, round_id, "C-FIN-PROG", AwardCategory.PROGRAM,
        "W-001", "W-001-V1", None, True,
    )


REFERENCE_DATE = date(2026, 9, 25)
