"""资格事实与资格复核服务。

文学奖、表演奖、新人奖引用不同资格事实；初评/分赛区封存后到达的证明
只能开立资格复核案件，复核结论只影响后续轮次，不改写封存结论。
"""

from __future__ import annotations

from datetime import date, datetime

from .records import (
    AgeEvidence,
    PerformanceRole,
    ReviewCase,
    ReviewVerdict,
    SuppliedEvidence,
)
from .repository import AwardRepository
from .roles import GovernanceError, PermissionDenied, Role

# 奖项类别
PROGRAM_AWARD = "节目奖"
LITERATURE_AWARD = "文学奖"
PERFORMANCE_AWARD = "表演奖"
NEWCOMER_AWARD = "新人奖"

# 新人奖报送截止日年龄上限（岁）
NEWCOMER_AGE_LIMIT = 45


class EligibilityService:
    def __init__(self, repo: AwardRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------ 复核案件

    def open_review_case(
        self,
        case_id: str,
        target_version_id: str,
        categories: tuple[str, ...],
        opened_by: str,
        opened_at: datetime,
        person_id: str | None = None,
    ) -> ReviewCase:
        """开立资格复核案件，并引用涉及的已封存轮次。"""
        if target_version_id not in self.repo.versions:
            raise GovernanceError("目标版本不存在")
        case = ReviewCase(
            id=case_id,
            target_version_id=target_version_id,
            categories=tuple(categories),
            person_id=person_id,
            opened_at=opened_at,
        )
        case.seal_refs = [
            seal_id
            for seal_id, seal in self.repo.seals.items()
            if target_version_id in seal.material_fingerprints
        ]
        self.repo.review_cases[case_id] = case
        return case

    def supply_evidence(
        self,
        case_id: str,
        evidence_id: str,
        received_at: date,
        content: str,
    ) -> None:
        """后补证明只进入复核案件；原封存指纹与结论保持不变。"""
        case = self.repo.review_cases.get(case_id)
        if case is None:
            raise GovernanceError("复核案件不存在")
        if case.verdict is not ReviewVerdict.PENDING:
            raise GovernanceError("案件已裁定，证据不再接收")
        if self.repo.is_evidence_locked(evidence_id):
            raise GovernanceError("证据被申诉锁定，暂停补入")
        case.evidence.append(
            SuppliedEvidence(
                evidence_id=evidence_id, received_at=received_at, content=content
            )
        )

    def attach_age_evidence_via_case(
        self,
        case_id: str,
        evidence_id: str,
        birth_date: date,
        verified_at: date,
        actor_role: Role,
        note: str = "",
    ) -> ReviewCase:
        """封存轮次之后补来的年龄证明：先入卷复核案件，裁定通过才生效。

        补入时先作为案件证据保存并写入人员档案的"待复核"位置；案件裁定
        前资格结论一律为待复核。裁定不通过时撤回，封存记录始终不触碰。
        """
        if actor_role is not Role.SUPERVISOR:
            raise PermissionDenied("后补证明由监审资格组核验")
        case = self.repo.review_cases.get(case_id)
        if case is None:
            raise GovernanceError("复核案件不存在")
        if case.person_id is None:
            raise GovernanceError("案件未指定候选人，无法补年龄证明")
        case.evidence.append(
            SuppliedEvidence(
                evidence_id=evidence_id,
                received_at=verified_at,
                content=f"年龄证明，出生日期{birth_date.isoformat()}",
            )
        )
        person = self.repo.persons[case.person_id]
        person.age_evidence = AgeEvidence(
            evidence_id=evidence_id,
            birth_date=birth_date,
            verified_at=verified_at,
            note=f"经复核案件{case_id}补入，待裁定。{note}",
        )
        case.effect = f"待裁定：年龄证明{evidence_id}"
        return case

    def decide_review_case(
        self,
        case_id: str,
        verdict: ReviewVerdict,
        decided_by: str,
        decided_at: datetime,
        actor_role: Role,
        effect: str,
        rationale: str = "",
    ) -> ReviewCase:
        if actor_role is not Role.SUPERVISOR:
            raise PermissionDenied("资格复核只能由监审资格组裁定")
        case = self.repo.review_cases.get(case_id)
        if case is None:
            raise GovernanceError("复核案件不存在")
        if case.verdict is not ReviewVerdict.PENDING:
            raise GovernanceError("案件已有裁定")
        case.verdict = verdict
        case.decided_by = decided_by
        case.decided_at = decided_at
        case.rationale = rationale
        case.effect = effect
        # 经本案件补入的年龄证明：不通过即撤回；通过则去掉"待裁定"标注。
        if case.person_id is not None:
            person = self.repo.persons[case.person_id]
            if (
                person.age_evidence is not None
                and person.age_evidence.note.startswith(f"经复核案件{case_id}补入")
            ):
                if verdict is ReviewVerdict.REJECTED:
                    person.age_evidence = None
                else:
                    person.age_evidence.note = (
                        f"经复核案件{case_id}补入并经裁定生效。{rationale}"
                    )
        return case

    # ------------------------------------------------------------ 资格判定

    def review_effects(
        self, version_id: str, category: str, person_id: str | None
    ) -> list[ReviewCase]:
        """针对该候选已经生效的复核结论。"""
        result = []
        for case in self.repo.review_cases.values():
            if case.target_version_id != version_id:
                continue
            if category not in case.categories:
                continue
            if person_id is not None and case.person_id not in (None, person_id):
                continue
            if case.verdict in (ReviewVerdict.PASSED, ReviewVerdict.REJECTED):
                result.append(case)
        return result

    def qualification(
        self,
        version_id: str,
        category: str,
        deadline: date,
        person_id: str | None = None,
    ) -> dict:
        """返回候选在某奖项上的资格结论与依据事实。

        结论只依据当前档案事实与已裁定复核案件；封存轮次本身不被改写。
        若存在受理中的申诉锁定相关证据，结论为"暂停确认"。
        """
        version = self.repo.versions.get(version_id)
        if version is None:
            raise GovernanceError("版本不存在")
        work = self.repo.works[version.work_id]

        if self.repo.is_category_blocked(version_id, category):
            return {
                "status": "暂停确认",
                "reasons": ["存在受理中的申诉，相关证据锁定"],
                "facts": [],
                "review_cases": [],
            }

        facts: list[str] = []
        failures: list[str] = []

        if category == LITERATURE_AWARD:
            self._check_literature(work, person_id, facts, failures)
        elif category == PERFORMANCE_AWARD:
            self._check_performance(version_id, person_id, facts, failures)
        elif category == NEWCOMER_AWARD:
            self._check_newcomer(version_id, person_id, deadline, facts, failures)
        elif category == PROGRAM_AWARD:
            self._check_program(work, facts, failures)
        else:
            raise GovernanceError(f"未知奖项类别：{category}")

        cases = self.review_effects(version_id, category, person_id)
        for case in cases:
            if case.verdict is ReviewVerdict.REJECTED:
                failures.append(f"复核案件{case.id}裁定不通过：{case.effect}")
            elif case.verdict is ReviewVerdict.PASSED:
                facts.append(f"复核案件{case.id}补正：{case.effect}")

        pending = [
            case
            for case in self.repo.review_cases.values()
            if case.target_version_id == version_id
            and category in case.categories
            and (person_id is None or case.person_id in (None, person_id))
            and case.verdict is ReviewVerdict.PENDING
        ]
        if pending:
            return {
                "status": "待复核",
                "reasons": [f"复核案件{case.id}尚未裁定" for case in pending],
                "facts": facts,
                "review_cases": cases,
            }

        return {
            "status": "不通过" if failures else "通过",
            "reasons": failures,
            "facts": facts,
            "review_cases": cases,
        }

    # ------------------------------------------------------------ 各奖项事实

    @staticmethod
    def _check_literature(work, person_id, facts, failures) -> None:
        if not work.authors:
            failures.append("缺少作者登记")
        else:
            facts.append(f"作者：{'、'.join(work.authors)}")
        if work.originality is None:
            failures.append("缺少原创声明")
        else:
            facts.append(
                f"原创声明：{work.originality.claim}（{work.originality.evidence_id}）"
            )
            if work.kind.value == "改编" and "授权" not in work.originality.claim:
                failures.append("改编作品未声明已获授权")
        if work.lineage:
            facts.append(f"文本沿革{len(work.lineage)}条")
        if person_id is not None and work.authors and person_id not in work.authors:
            failures.append("候选人不是登记作者，不适用文学奖资格")

    def _check_performance(self, version_id, person_id, facts, failures) -> None:
        participations = self.repo.participations_for(version_id)
        performers = [p for p in participations if p.role is PerformanceRole.PERFORMER]
        if not performers:
            failures.append("该版本无表演演职关系")
            return
        facts.append(
            "表演：" + "、".join(f"{p.person_id}（随{p.org_id}）" for p in performers)
        )
        if person_id is not None and person_id not in {p.person_id for p in performers}:
            failures.append("候选人未在该版本担任表演")

    def _check_newcomer(self, version_id, person_id, deadline, facts, failures) -> None:
        if person_id is None:
            failures.append("新人奖必须指定候选人")
            return
        person = self.repo.persons.get(person_id)
        if person is None:
            failures.append("人员档案不存在")
            return
        # 先以表演演职关系为前提
        performers = {
            p.person_id
            for p in self.repo.participations_for(version_id)
            if p.role is PerformanceRole.PERFORMER
        }
        if person_id not in performers:
            failures.append("候选人未在该版本担任表演")
        if person.age_evidence is None:
            failures.append("缺少年龄证明")
        else:
            age = deadline.year - person.age_evidence.birth_date.year
            if (deadline.month, deadline.day) < (
                person.age_evidence.birth_date.month,
                person.age_evidence.birth_date.day,
            ):
                age -= 1
            facts.append(
                f"报送截止日{deadline.isoformat()}年龄{age}岁"
                f"（证明{person.age_evidence.evidence_id}）"
            )
            if age > NEWCOMER_AGE_LIMIT:
                failures.append(f"超过新人奖{NEWCOMER_AGE_LIMIT}岁年龄限制")
        restricted = {
            category
            for award in person.past_awards
            for category in award.restricted_categories
        }
        if NEWCOMER_AWARD in restricted:
            names = "、".join(
                f"{award.award_name}（{award.year}）"
                for award in person.past_awards
                if NEWCOMER_AWARD in award.restricted_categories
            )
            failures.append(f"历史获奖限制：{names}")
        elif person.past_awards:
            facts.append("历史获奖不限制本届新人奖")

    @staticmethod
    def _check_program(work, facts, failures) -> None:
        if not work.rights_holders:
            failures.append("缺少权利人")
        else:
            facts.append(f"权利人：{'、'.join(work.rights_holders)}")
        if work.originality is None:
            failures.append("缺少原创声明")
        if work.premiere is None:
            failures.append("缺少首演事实")
        else:
            facts.append(
                f"首演：{work.premiere.premiered_at.isoformat()} {work.premiere.venue}"
            )
