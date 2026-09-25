"""评委服务：回避执行与匿名评分。

两条硬边界：
1. 回避以作品、人员、单位三重关系为依据自动识别，评委只能对无冲突作品评分；
2. 评分通道只认匿名代号。秘书处负责材料完整性，可以看到代号与分值，
   但代号与评委身份的映射只有组委会在正式核验时才能解析。
"""

from __future__ import annotations

from .archive_service import ArchiveService
from .errors import (
    AppealLockError,
    AuthorizationError,
    NotFoundError,
    RecusalError,
    SealedError,
)
from .models import (
    Judge,
    Recusal,
    RecusalReason,
    Role,
    ScoreRecord,
    utc_now,
)
from .store import AwardStore


class JuryService:
    def __init__(self, store: AwardStore, archive: ArchiveService):
        self.store = store
        self.archive = archive

    # -- 授权 ----------------------------------------------------------------

    def _staff(self, actor_id: str, roles: tuple[Role, ...]):
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise AuthorizationError(f"操作者未登记：{actor_id}")
        if actor.role not in roles:
            raise AuthorizationError(f"{actor.name}无权执行该操作")
        return actor

    # -- 评委登记与匿名代号 --------------------------------------------------

    def register_judge(
        self,
        actor_id: str,
        judge_id: str,
        person_id: str,
        name: str,
        org_id: str | None = None,
    ) -> Judge:
        self._staff(actor_id, (Role.ORGANIZER,))
        self.store.person(person_id)
        if judge_id in self.store.judges:
            raise ValueError(f"评委标识已存在：{judge_id}")
        judge = Judge(judge_id=judge_id, person_id=person_id, name=name, org_id=org_id)
        self.store.judges[judge_id] = judge
        self.store.log(actor_id, "登记评委", judge_id, f"{person_id}/{org_id or ''}")
        return judge

    def issue_alias(self, actor_id: str, judge_id: str, round_id: str) -> str:
        """组委会在某一轮次为评委发放匿名代号；映射对秘书处不可见。"""
        self._staff(actor_id, (Role.ORGANIZER,))
        judge = self.store.judges.get(judge_id)
        if judge is None or not judge.active:
            raise NotFoundError(f"评委不存在或已停用：{judge_id}")
        self.store.round(round_id)
        existing = [
            alias for alias, jid in self.store.alias_to_judge.items()
            if jid == judge_id and f"-{round_id}-" in alias
        ]
        if existing:
            return existing[0]
        serial = len([a for a in self.store.alias_to_judge if f"-{round_id}-" in a]) + 1
        alias = f"J-{round_id}-{serial:02d}"
        self.store.alias_to_judge[alias] = judge_id
        self.store.judge_aliases.setdefault(judge_id, []).append(alias)
        self.store.log(actor_id, "发放匿名代号", round_id, f"{judge_id}->{alias}（映射保密）")
        return alias

    def resolve_alias(self, actor_id: str, alias: str) -> str:
        """解析代号身份。仅组委会正式核验可用；秘书处一律拒绝。"""
        actor = self._staff(actor_id, (Role.ORGANIZER,))
        judge_id = self.store.alias_to_judge.get(alias)
        if judge_id is None:
            raise NotFoundError(f"匿名代号不存在：{alias}")
        self.store.log(actor.actor_id, "核验代号身份", alias, judge_id)
        return judge_id

    def declare_relation(
        self, actor_id: str, judge_id: str, related_person_id: str, detail: str
    ) -> None:
        """评委（经工作人员代录）或组委会主动申报利害关系人。"""
        self._staff(actor_id, (Role.ORGANIZER,))
        judge = self.store.judges.get(judge_id)
        if judge is None:
            raise NotFoundError(f"评委不存在：{judge_id}")
        self.store.person(related_person_id)
        judge.related_person_ids[related_person_id] = detail
        self.store.log(actor_id, "评委声明利害关系", judge_id, f"{related_person_id}:{detail}")

    # -- 回避识别 ------------------------------------------------------------

    def evaluate_recusals(self, actor_id: str, round_id: str) -> list[Recusal]:
        """按作品、人员、单位关系为轮次内全部候选执行回避。

        已存在的回避不重复登记。回避依据写入原因与细节，最终可溯源。
        """
        staff = self._staff(actor_id, (Role.ORGANIZER, Role.SECRETARIAT))
        round_record = self.store.round(round_id)
        work_ids = sorted({a.work_id for a in round_record.advancements.values()})
        created: list[Recusal] = []
        for judge in self.store.judges.values():
            if not judge.active:
                continue
            for work_id in work_ids:
                conflict = self._conflict(judge, round_id, work_id)
                if conflict is None:
                    continue
                reason, detail = conflict
                if self._find_recusal(judge.judge_id, round_id, work_id):
                    continue
                recusal = Recusal(
                    judge_id=judge.judge_id,
                    round_id=round_id,
                    work_id=work_id,
                    reason=reason,
                    detail=detail,
                    declared_at=utc_now(),
                )
                self.store.recusals.append(recusal)
                created.append(recusal)
        self.store.log(staff.actor_id, "执行回避识别", round_id, f"新增{len(created)}条回避")
        return created

    def _conflict(self, judge: Judge, round_id: str, work_id: str):
        # 作品关系：评委本人是该作品的作者、演员或权利人
        related_persons = self.archive.work_related_person_ids(work_id)
        if judge.person_id in related_persons:
            return RecusalReason.WORK_RELATION, f"评委本人参与作品{work_id}的创作或表演"
        # 人员关系：评委声明的利害关系人是该作品候选人员或参演人员
        declared = set(judge.related_person_ids)
        if declared & related_persons:
            hit = sorted(declared & related_persons)
            return RecusalReason.PERSON_RELATION, f"与{','.join(hit)}存在申报的利害关系"
        # 单位关系：评委所属院团与候选报送/参演单位相同
        related_orgs = self.archive.work_related_org_ids(work_id)
        if judge.org_id and judge.org_id in related_orgs:
            return RecusalReason.ORG_RELATION, f"评委所属单位与作品关联单位{judge.org_id}相同"
        return None

    def _find_recusal(self, judge_id: str, round_id: str, work_id: str) -> Recusal | None:
        return next(
            (
                r for r in self.store.recusals
                if r.judge_id == judge_id and r.round_id == round_id and r.work_id == work_id
            ),
            None,
        )

    def recusals_for(self, round_id: str, work_id: str | None = None) -> list[Recusal]:
        return [
            r for r in self.store.recusals
            if r.round_id == round_id and (work_id is None or r.work_id == work_id)
        ]

    def is_recused(self, judge_id: str, round_id: str, work_id: str) -> bool:
        return self._find_recusal(judge_id, round_id, work_id) is not None

    def assignable_judges(self, actor_id: str, round_id: str, work_id: str) -> list[str]:
        """返回某作品在该轮次可分配（无需回避）的评委。"""
        self._staff(actor_id, (Role.ORGANIZER, Role.SECRETARIAT))
        self.store.round(round_id)
        self.store.work(work_id)
        return [
            j.judge_id for j in self.store.judges.values()
            if j.active and not self.is_recused(j.judge_id, round_id, work_id)
        ]

    # -- 匿名评分 ------------------------------------------------------------

    def submit_score(
        self, alias: str, round_id: str, work_id: str, version_id: str, value: float
    ) -> str:
        """评委凭匿名代号提交评分。该通道不接受工作人员身份，秘书处无法代为操作。

        以下情况拒绝评分：
        - 代号无效、评委已停用；
        - 评委对该作品应回避；
        - 该评分证据正被申诉锁定；
        - 轮次已封存，或节目版本已被换角/重新表演形成的新版本取代。
        """
        judge_id = self.store.alias_to_judge.get(alias)
        if judge_id is None:
            raise AuthorizationError("匿名代号无效，评分通道拒绝访问")
        judge = self.store.judges[judge_id]
        if not judge.active:
            raise AuthorizationError("评委已停用")
        round_record = self.store.round(round_id)
        if round_record.sealed is not None:
            raise SealedError(f"{round_record.name}已封存，评分通道关闭")
        work = self.store.work(work_id)
        version = work.get_version(version_id)
        if version is None:
            raise NotFoundError(f"节目版本不存在：{version_id}")
        if self._is_superseded(round_id, work_id, version_id):
            raise RecusalError("该节目版本已被现场新事实取代，请对现行版本评分")
        if self.is_recused(judge_id, round_id, work_id):
            recusal = self._find_recusal(judge_id, round_id, work_id)
            raise RecusalError(f"评委对该作品须回避：{recusal.reason.value}")
        if not 0 <= value <= 100:
            raise ValueError("评分须在0到100之间")

        existing = next(
            (
                s for s in self.store.scores.values()
                if s.round_id == round_id
                and s.work_id == work_id
                and s.version_id == version_id
                and s.judge_alias == alias
                and not s.superseded
            ),
            None,
        )
        if existing is not None:
            if existing.locked_by_appeal:
                raise AppealLockError("原评分已被申诉锁定为证据，不能修改")
            existing.value = value
            existing.submitted_at = utc_now()
            self.store.log("alias:" + alias, "更新评分", work_id, f"{version_id}={value}")
            return existing.score_id

        score_id = f"SCR-{len(self.store.scores) + 1:04d}"
        score = ScoreRecord(
            score_id=score_id,
            round_id=round_id,
            work_id=work_id,
            version_id=version_id,
            judge_alias=alias,
            value=value,
            submitted_at=utc_now(),
        )
        self.store.scores[score_id] = score
        self.store.log("alias:" + alias, "提交评分", work_id, f"{version_id}={value}")
        return score_id

    def _is_superseded(self, round_id: str, work_id: str, version_id: str) -> bool:
        """该版本是否已被现场事件（换角/重新表演）产生的新版本取代。"""
        return self._has_newer_live_version(round_id, work_id, version_id)

    def _has_newer_live_version(self, round_id: str, work_id: str, version_id: str) -> bool:
        """现场事件是否为本版本生成了更新版本。"""
        for event in self.store.live_events:
            if (
                event.round_id == round_id
                and event.work_id == work_id
                and event.version_id == version_id
                and event.new_version_id
            ):
                return True
        return False

    def supersede_version_scores(
        self, actor_id: str, round_id: str, work_id: str, old_version_id: str
    ) -> list[str]:
        """现场换角/重新表演后，旧版本评分归档（保留可查，不计入现行评分）。"""
        self._staff(actor_id, (Role.ORGANIZER,))
        changed: list[str] = []
        for score in self.store.scores.values():
            if (
                score.round_id == round_id
                and score.work_id == work_id
                and score.version_id == old_version_id
                and not score.superseded
            ):
                # 被申诉锁定的评分仍是证据：不动数值，仅随版本归档并保留锁定状态
                score.superseded = True
                changed.append(score.score_id)
        self.store.log(actor_id, "旧版本评分归档", work_id, f"{old_version_id}->{len(changed)}条")
        return changed

    # -- 评分查询与身份边界 --------------------------------------------------

    def ledger_for_secretariat(self, actor_id: str, round_id: str) -> list[dict]:
        """秘书处视角：只有代号、节目版本与分值，绝不含评委身份字段。"""
        self._staff(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        rows = []
        for score in self.store.scores.values():
            if score.round_id != round_id:
                continue
            rows.append(
                {
                    "score_id": score.score_id,
                    "work_id": score.work_id,
                    "version_id": score.version_id,
                    "judge_alias": score.judge_alias,
                    "value": score.value,
                    "superseded": score.superseded,
                    "locked_by_appeal": score.locked_by_appeal,
                }
            )
        return sorted(rows, key=lambda r: (r["work_id"], r["version_id"], r["judge_alias"]))

    def current_scores(self, round_id: str, work_id: str, version_id: str) -> list[ScoreRecord]:
        """现行有效评分：排除已归档旧版本、申诉锁定中与被裁定排除的评分。"""
        return [
            s for s in self.store.scores.values()
            if s.round_id == round_id
            and s.work_id == work_id
            and s.version_id == version_id
            and not s.superseded
            and not s.locked_by_appeal
            and not s.excluded
        ]
