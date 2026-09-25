"""记录仓储：集中登记全部档案与裁定记录。

仓储不做权限判断；它负责保存不可变历史，并回答"某证据/候选是否被
封存或被申诉锁定"这类事实查询。
"""

from __future__ import annotations

from .records import (
    Affiliation,
    Appeal,
    AppealStatus,
    CandidateStanding,
    DuplicateClue,
    Organization,
    Participation,
    PastAward,
    Person,
    RegionalExperience,
    ReviewCase,
    RoundSeal,
    Work,
    WorkVersion,
)


class AwardRepository:
    def __init__(self) -> None:
        self.organizations: dict[str, Organization] = {}
        self.persons: dict[str, Person] = {}
        self.works: dict[str, Work] = {}
        self.versions: dict[str, WorkVersion] = {}
        self.participations: list[Participation] = []
        self.regionals: list[RegionalExperience] = []
        self.duplicate_clues: dict[str, DuplicateClue] = {}
        self.seals: dict[str, RoundSeal] = {}
        self.review_cases: dict[str, ReviewCase] = {}
        self.judges: dict[str, Judge] = {}
        self.conflicts: list[JudgeConflict] = []
        self.scores: list[BlindScore] = []
        self.blind_mappings: dict[str, BlindMapping] = {}
        self.finals_events: list[FinalsEvent] = []
        self.standings: dict[tuple[str, str, str | None], CandidateStanding] = {}
        self.quota_checks: list[QuotaCheck] = []
        self.notices: dict[str, PublicNotice] = {}
        self.appeals: dict[str, Appeal] = {}

    # ------------------------------------------------------------ 基础写入

    def add_organization(self, org: Organization) -> None:
        self.organizations[org.id] = org

    def add_person(self, person: Person) -> None:
        self.persons[person.id] = person

    def add_work(self, work: Work) -> None:
        self.works[work.id] = work

    def add_version(self, version: WorkVersion) -> None:
        self.versions[version.id] = version
        self.works[version.work_id].version_ids.append(version.id)

    def add_participation(self, item: Participation) -> None:
        self.participations.append(item)

    def add_regional(self, item: RegionalExperience) -> None:
        self.regionals.append(item)

    def add_past_award(self, person_id: str, award: PastAward) -> None:
        self.persons[person_id].past_awards.append(award)

    def add_affiliation(self, person_id: str, item: Affiliation) -> None:
        """追加单位区间，不覆盖历史。"""
        self.persons[person_id].affiliations.append(item)

    # ------------------------------------------------------------ 查询

    def versions_of_work(self, work_id: str) -> list[WorkVersion]:
        return [self.versions[vid] for vid in self.works[work_id].version_ids]

    def participations_for(self, version_id: str) -> list[Participation]:
        return [p for p in self.participations if p.version_id == version_id]

    def participations_of_person(self, person_id: str) -> list[Participation]:
        return [p for p in self.participations if p.person_id == person_id]

    def regionals_for(self, version_id: str) -> list[RegionalExperience]:
        return [r for r in self.regionals if r.version_id == version_id]

    def clues_for_version(self, version_id: str) -> list[DuplicateClue]:
        return [
            clue
            for clue in self.duplicate_clues.values()
            if version_id in (clue.version_id_a, clue.version_id_b)
        ]

    def standing(
        self, version_id: str, category: str, person_id: str | None
    ) -> CandidateStanding:
        key = (version_id, category, person_id)
        if key not in self.standings:
            self.standings[key] = CandidateStanding(
                version_id=version_id, category=category, person_id=person_id
            )
        return self.standings[key]

    def winners_confirmed(self) -> bool:
        return any(notice.confirmed for notice in self.notices.values())

    # ------------------------------------------------------------ 封存与锁定

    def sealed_version_ids(self) -> set[str]:
        sealed: set[str] = set()
        for seal in self.seals.values():
            sealed.update(seal.material_fingerprints)
        return sealed

    def is_version_sealed(self, version_id: str) -> bool:
        return any(
            version_id in seal.material_fingerprints for seal in self.seals.values()
        )

    def is_version_advanced(self, version_id: str, category: str) -> bool:
        for seal in self.seals.values():
            for item in seal.advancements:
                if item.version_id == version_id and item.category == category:
                    return item.advanced
        return False

    def open_appeals_for(self, version_id: str, category: str | None = None) -> list[Appeal]:
        """针对某版本（可限定奖项）的受理中申诉。"""
        result = []
        for appeal in self.appeals.values():
            if appeal.status != AppealStatus.OPEN:
                continue
            if appeal.version_id != version_id:
                continue
            if category is not None and category not in appeal.categories:
                continue
            result.append(appeal)
        return result

    def locked_evidence(self) -> set[str]:
        """被受理中申诉锁定的全部证据编号。"""
        locked: set[str] = set()
        for appeal in self.appeals.values():
            if appeal.status == AppealStatus.OPEN:
                locked.update(appeal.locked_evidence)
        return locked

    def is_evidence_locked(self, evidence_id: str) -> bool:
        return evidence_id in self.locked_evidence()

    def blocked_categories(self, version_id: str) -> set[str]:
        """因受理中申诉而暂停确认的（版本, 奖项）范围。"""
        blocked: set[str] = set()
        for appeal in self.appeals.values():
            if appeal.status == AppealStatus.OPEN and appeal.version_id == version_id:
                blocked.update(appeal.categories)
        return blocked

    def is_category_blocked(self, version_id: str, category: str) -> bool:
        return category in self.blocked_categories(version_id)
