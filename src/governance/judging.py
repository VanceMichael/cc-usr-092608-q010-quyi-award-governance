"""评委回避与匿名评分服务。

评委分配前按作品、人员、单位三类关系计算冲突并留痕；被回避的评分
拒绝录入。评分只认匿名编号，秘书处只能做材料完整性与编目，不能由
编号反查身份；匿名映射只有监审在评分封闭后可以揭示。
"""

from __future__ import annotations

from datetime import date, datetime

from .records import (
    BlindMapping,
    BlindScore,
    ConflictType,
    Judge,
    JudgeConflict,
    PerformanceRole,
)
from .repository import AwardRepository
from .roles import ConflictOfInterest, GovernanceError, PermissionDenied, Role


class JudgingService:
    def __init__(self, repo: AwardRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------ 评委登记

    def register_judge(
        self,
        judge_id: str,
        name: str,
        org_id: str | None = None,
        worked_version_ids: set[str] | None = None,
    ) -> Judge:
        judge = Judge(
            id=judge_id,
            name=name,
            org_id=org_id,
            worked_version_ids=set(worked_version_ids or ()),
        )
        self.repo.judges[judge_id] = judge
        return judge

    # ------------------------------------------------------------ 回避计算

    def assign(
        self,
        judge_id: str,
        candidates: list[tuple[str, str, str | None]],
        on_date: date,
    ) -> list[JudgeConflict]:
        """对候选序列（版本, 奖项, 人员或None）计算回避记录并返回。

        有冲突的候选不会被分配；回避记录写明关系类型与依据事实。
        """
        judge = self.repo.judges.get(judge_id)
        if judge is None:
            raise GovernanceError("评委不存在")
        conflicts: list[JudgeConflict] = []
        for version_id, category, person_id in candidates:
            conflict = self._detect_conflict(
                judge, version_id, category, person_id, on_date
            )
            if conflict is not None:
                conflicts.append(conflict)
        for conflict in conflicts:
            self.repo.conflicts.append(conflict)
        return conflicts

    def _detect_conflict(
        self,
        judge: Judge,
        version_id: str,
        category: str,
        person_id: str | None,
        on_date: date,
    ) -> JudgeConflict | None:
        version = self.repo.versions[version_id]
        work = self.repo.works[version.work_id]

        # 1) 作品关系：候选版本、同一作品线的版本、经确认关联的沿革作品
        related_version_ids = set(work.version_ids)
        for lineage_work_id, _ in work.lineage:
            lineage_work = self.repo.works.get(lineage_work_id)
            if lineage_work is not None:
                related_version_ids.update(lineage_work.version_ids)
        hit_version = next(
            (vid for vid in judge.worked_version_ids if vid in related_version_ids),
            None,
        )
        if hit_version is not None:
            return JudgeConflict(
                judge_id=judge.id,
                candidate_version_id=version_id,
                category=category,
                person_id=person_id,
                conflict_type=ConflictType.WORK,
                basis=f"评委参与过沿革版本{hit_version}的创作/表演",
            )

        # 2) 人员关系：候选相关人员与评委存在声明关系（亲属、师承等）
        related_person_ids = self._candidate_persons(version_id, work, person_id)
        for pid in related_person_ids:
            person = self.repo.persons.get(pid)
            if person is not None and judge.id in person.declared_relations:
                return JudgeConflict(
                    judge_id=judge.id,
                    candidate_version_id=version_id,
                    category=category,
                    person_id=person_id,
                    conflict_type=ConflictType.PERSON,
                    basis=f"与候选人{pid}存在{person.declared_relations[judge.id]}关系",
                )

        # 3) 单位关系：评委在册单位与报送/参演单位或候选人单位重合
        if judge.org_id is not None:
            org_sources = {version.submitting_org_id}
            org_sources.update(
                p.org_id for p in self.repo.participations_for(version_id)
            )
            if person_id is not None:
                person = self.repo.persons.get(person_id)
                if person is not None:
                    current_org = person.org_on(on_date)
                    if current_org is not None:
                        org_sources.add(current_org)
            if judge.org_id in org_sources:
                return JudgeConflict(
                    judge_id=judge.id,
                    candidate_version_id=version_id,
                    category=category,
                    person_id=person_id,
                    conflict_type=ConflictType.ORG,
                    basis=f"评委与单位{judge.org_id}存在在册任职关系",
                )
        return None

    def _candidate_persons(self, version_id, work, person_id) -> set[str]:
        ids: set[str] = set()
        if person_id is not None:
            ids.add(person_id)
        ids.update(work.authors)
        ids.update(work.rights_holders)
        ids.update(
            p.person_id
            for p in self.repo.participations_for(version_id)
            if p.role in (PerformanceRole.PERFORMER, PerformanceRole.AUTHOR)
        )
        return ids

    def is_recused(
        self, judge_id: str, version_id: str, category: str
    ) -> JudgeConflict | None:
        for conflict in self.repo.conflicts:
            if (
                conflict.judge_id == judge_id
                and conflict.candidate_version_id == version_id
                and conflict.category == category
            ):
                return conflict
        return None

    # ------------------------------------------------------------ 匿名评分

    def issue_blind_code(
        self,
        code: str,
        version_id: str,
        category: str,
        person_id: str | None,
        actor_role: Role,
    ) -> BlindMapping:
        """秘书处负责匿名编目；编目时可以建立映射，之后不可反查。"""
        if actor_role is not Role.SECRETARIAT:
            raise PermissionDenied("匿名编号由秘书处编目")
        if code in self.repo.blind_mappings:
            raise GovernanceError("匿名编号已存在")
        mapping = BlindMapping(
            code=code,
            version_id=version_id,
            person_id=person_id,
            category=category,
        )
        self.repo.blind_mappings[code] = mapping
        return mapping

    def submit_score(
        self,
        code: str,
        judge_id: str,
        value: float,
        basis: str,
        submitted_at: datetime,
    ) -> BlindScore:
        """评委凭匿名编号提交评分；回避或已揭示后均拒绝录入。"""
        mapping = self.repo.blind_mappings.get(code)
        if mapping is None:
            raise GovernanceError("匿名编号不存在")
        if mapping.revealed:
            raise GovernanceError("评分已封闭并揭示身份，不再收分")
        conflict = self.is_recused(judge_id, mapping.version_id, mapping.category)
        if conflict is not None:
            raise ConflictOfInterest(
                f"评委{judge_id}已因{conflict.conflict_type.value}回避该候选，不得评分"
            )
        if not 0 <= value <= 100:
            raise GovernanceError("评分超出0-100范围")
        score = BlindScore(
            code=code,
            judge_id=judge_id,
            round_name="",
            value=value,
            basis=basis,
            submitted_at=submitted_at,
        )
        self.repo.scores.append(score)
        return score

    def completeness_report(self, actor_role: Role) -> dict[str, int]:
        """材料完整性：只返回各匿名编号的评分份数，不暴露身份。

        秘书处用它核对评分是否齐备；任何角色都无法据此反查候选。
        """
        if actor_role not in (Role.SECRETARIAT, Role.SUPERVISOR, Role.ORGANIZING_COMMITTEE):
            raise PermissionDenied("无权查看完整性报告")
        report = {code: 0 for code in self.repo.blind_mappings}
        for score in self.repo.scores:
            report[score.code] = report.get(score.code, 0) + 1
        return report

    def reveal_identity(
        self, code: str, actor_role: Role, revealed_by: str, revealed_at: datetime
    ) -> BlindMapping:
        """揭示匿名映射：评分封闭后仅监审可执行。"""
        if actor_role is not Role.SUPERVISOR:
            raise PermissionDenied("匿名身份只能由监审揭示")
        mapping = self.repo.blind_mappings.get(code)
        if mapping is None:
            raise GovernanceError("匿名编号不存在")
        if mapping.revealed:
            raise GovernanceError("映射已揭示")
        mapping.revealed = True
        mapping.revealed_by = revealed_by
        mapping.revealed_at = revealed_at
        return mapping

    def identity_of(
        self, code: str, actor_role: Role
    ) -> tuple[str, str | None, str]:
        """按编号查身份：秘书处（及其他非监审角色）一律拒绝。"""
        if actor_role is not Role.SUPERVISOR:
            raise PermissionDenied("秘书处等角色不得揭示匿名评分身份")
        mapping = self.repo.blind_mappings[code]
        if not mapping.revealed:
            raise GovernanceError("映射尚未揭示")
        return mapping.version_id, mapping.person_id, mapping.category
