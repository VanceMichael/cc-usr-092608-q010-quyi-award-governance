"""作品与人员档案服务。

负责作品登记、版本（含改名再报送）、疑似重复线索、演职关系、单位
区间、年龄证明与赛区经历。疑似重复只能由授权人员确认关联，系统绝不
依据名称相似度自动合并。
"""

from __future__ import annotations

from datetime import date, datetime
from difflib import SequenceMatcher

from .records import (
    Affiliation,
    AgeEvidence,
    DuplicateClue,
    DuplicateStatus,
    Organization,
    Participation,
    PerformanceRole,
    Person,
    RegionalExperience,
    Work,
    WorkKind,
    WorkVersion,
)
from .repository import AwardRepository
from .roles import GovernanceError, PermissionDenied, Role

NAME_SIMILARITY_THRESHOLD = 0.8


class ArchiveService:
    def __init__(self, repo: AwardRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------ 单位/人员

    def register_organization(self, org_id: str, name: str) -> Organization:
        org = Organization(id=org_id, name=name)
        self.repo.add_organization(org)
        return org

    def register_person(
        self,
        person_id: str,
        name: str,
        org_id: str | None = None,
        valid_from: date | None = None,
    ) -> Person:
        person = Person(id=person_id, name=name)
        if org_id is not None:
            if valid_from is None:
                raise GovernanceError("单位关系需要生效日期")
            person.affiliations.append(Affiliation(org_id=org_id, valid_from=valid_from))
        self.repo.add_person(person)
        return person

    def add_affiliation(
        self, person_id: str, org_id: str, valid_from: date, valid_to: date | None = None
    ) -> None:
        """调动只追加新区间，历史区间保留。"""
        self.repo.add_affiliation(
            person_id, Affiliation(org_id=org_id, valid_from=valid_from, valid_to=valid_to)
        )

    def add_age_evidence(
        self,
        person_id: str,
        evidence_id: str,
        birth_date: date,
        verified_at: date,
        note: str = "",
    ) -> None:
        """登记年龄证明。该人员已参演的版本一旦封存，证明只能走资格复核。"""
        if self.repo.is_evidence_locked(evidence_id):
            raise GovernanceError("年龄证明被申诉锁定，不得改写")
        for participation in self.repo.participations_of_person(person_id):
            if self.repo.is_version_sealed(participation.version_id):
                raise GovernanceError("原轮次已封存，后补证明须进入资格复核")
        self.repo.persons[person_id].age_evidence = AgeEvidence(
            evidence_id=evidence_id,
            birth_date=birth_date,
            verified_at=verified_at,
            note=note,
        )

    def add_regional_experience(
        self, person_id: str, version_id: str, region_id: str, result: str
    ) -> None:
        self.repo.add_regional(
            RegionalExperience(
                person_id=person_id,
                version_id=version_id,
                region_id=region_id,
                result=result,
            )
        )

    # ------------------------------------------------------------ 作品/版本

    def register_work(
        self,
        work_id: str,
        genre: str,
        title: str,
        kind: WorkKind,
        authors: tuple[str, ...],
        rights_holders: tuple[str, ...],
        originality=None,
        premiere=None,
        lineage: list[tuple[str, str]] | None = None,
    ) -> Work:
        if not authors:
            raise GovernanceError("作品必须登记作者")
        if not rights_holders:
            raise GovernanceError("作品必须登记权利人")
        work = Work(
            id=work_id,
            genre=genre,
            title=title,
            kind=kind,
            authors=tuple(authors),
            rights_holders=tuple(rights_holders),
            originality=originality,
            premiere=premiere,
            lineage=list(lineage or []),
        )
        self.repo.add_work(work)
        return work

    def submit_version(
        self,
        version_id: str,
        work_id: str,
        title: str,
        text_fingerprint: str,
        submitting_org_id: str,
        submitted_at: date,
        parent_version_id: str | None = None,
        note: str = "",
        actor_role: Role = Role.SUBMITTING_ORG,
    ) -> WorkVersion:
        """报送一个作品版本；改名再报送只是新版本，不产生新作品线。

        与既有版本指纹相同、或同名作品题名高度相似的，只生成疑似线索，
        绝不自动合并。
        """
        if actor_role not in (Role.SUBMITTING_ORG, Role.SECRETARIAT):
            raise PermissionDenied("只有报送院团或秘书处可报送版本")
        if work_id not in self.repo.works:
            raise GovernanceError("作品不存在")
        if parent_version_id is not None and parent_version_id not in self.repo.versions:
            raise GovernanceError("所声明的沿革父版本不存在")
        version = WorkVersion(
            id=version_id,
            work_id=work_id,
            title=title,
            text_fingerprint=text_fingerprint,
            submitting_org_id=submitting_org_id,
            submitted_at=submitted_at,
            parent_version_id=parent_version_id,
            note=note,
        )
        self.repo.add_version(version)
        self._detect_duplicate_clues(version)
        return version

    def _detect_duplicate_clues(self, version: WorkVersion) -> None:
        work = self.repo.works[version.work_id]
        for other in list(self.repo.versions.values()):
            if other.id == version.id:
                continue
            # 同一作品线内的版本是已声明沿革（含改名再报送），不是疑似重复。
            if other.work_id == version.work_id:
                continue
            # 疑似是作品线之间的关系：两条作品线已有线索则不再按版本重复生成。
            if self._line_pair_has_clue(version.work_id, other.work_id):
                continue
            if other.text_fingerprint == version.text_fingerprint:
                self._raise_clue(version, other, "文本指纹相同")
            elif (
                self.repo.works[other.work_id].genre == work.genre
                and self._title_similar(version.title, other.title)
            ):
                # 名称相似只作为弱线索，等待授权人员人工判断。
                self._raise_clue(version, other, "名称相似（不得据此自动合并）")

    def _line_pair_has_clue(self, work_id_a: str, work_id_b: str) -> bool:
        for clue in self.repo.duplicate_clues.values():
            line_a = self.repo.versions[clue.version_id_a].work_id
            line_b = self.repo.versions[clue.version_id_b].work_id
            if {line_a, line_b} == {work_id_a, work_id_b}:
                return True
        return False

    @staticmethod
    def _title_similar(left: str, right: str) -> bool:
        return (
            SequenceMatcher(None, left, right).ratio() >= NAME_SIMILARITY_THRESHOLD
        )

    def _raise_clue(
        self, version: WorkVersion, other: WorkVersion, reason: str
    ) -> None:
        clue_id = f"dup-{min(version.id, other.id)}-{max(version.id, other.id)}"
        self.repo.duplicate_clues[clue_id] = DuplicateClue(
            id=clue_id,
            version_id_a=other.id,
            version_id_b=version.id,
            reason=reason,
        )

    def flag_clue(
        self, clue_id: str, version_a: str, version_b: str, reason: str
    ) -> DuplicateClue:
        """人工补充疑似线索（如审查中发现的文本套用）。"""
        clue = DuplicateClue(
            id=clue_id, version_id_a=version_a, version_id_b=version_b, reason=reason
        )
        self.repo.duplicate_clues[clue_id] = clue
        return clue

    def decide_clue(
        self,
        clue_id: str,
        actor_role: Role,
        confirmed: bool,
        decided_by: str,
        decided_at: datetime,
        note: str = "",
    ) -> DuplicateClue:
        """确认或驳回疑似关联。仅监审资格组有权；确认只建立关联不改并档案。"""
        if actor_role is not Role.SUPERVISOR:
            raise PermissionDenied("疑似重复关联只能由授权人员确认")
        clue = self.repo.duplicate_clues.get(clue_id)
        if clue is None:
            raise GovernanceError("疑似线索不存在")
        if clue.status is not DuplicateStatus.SUSPECTED:
            raise GovernanceError("线索已有结论，不得重复裁定")
        clue.status = (
            DuplicateStatus.CONFIRMED if confirmed else DuplicateStatus.REJECTED
        )
        clue.decided_by = decided_by
        clue.decided_at = decided_at
        clue.decision_note = note
        if confirmed:
            work_a = self.repo.versions[clue.version_id_a].work_id
            work_b = self.repo.versions[clue.version_id_b].work_id
            if work_a != work_b:
                # 双向建立沿革引用；关联不合并档案，只记录"同一文本"事实。
                description = f"疑似重复关联经{decided_by}确认：{note}"
                self.repo.works[work_a].lineage.append((work_b, description))
                self.repo.works[work_b].lineage.append((work_a, description))
        return clue

    # ------------------------------------------------------------ 演职关系

    def add_participation(
        self,
        person_id: str,
        version_id: str,
        role: PerformanceRole,
        org_id: str,
    ) -> Participation:
        """同一人可随不同院团参演不同版本，逐条记录，不做唯一归属推断。"""
        item = Participation(
            person_id=person_id,
            version_id=version_id,
            role=role,
            org_id=org_id,
        )
        self.repo.add_participation(item)
        return item

    # ------------------------------------------------------------ 沿革查询

    def work_lineage(self, work_id: str) -> dict:
        """汇聚作品线沿革：版本链、文本沿革、经确认的跨作品关联与线索。"""
        work = self.repo.works[work_id]
        return {
            "work": work,
            "versions": self.repo.versions_of_work(work_id),
            "lineage": list(work.lineage),
            "clues": [
                clue
                for vid in work.version_ids
                for clue in self.repo.clues_for_version(vid)
            ],
        }
