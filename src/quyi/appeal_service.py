"""申诉服务：申诉受理、证据锁定与裁定。

锁定是精确的：
- 资格类申诉锁定被质疑候选的资格；
- 文本类申诉锁定被质疑的作品版本文本；
- 评分类申诉锁定具体评分证据。
无关奖项与候选不被锁定，照常确认。申诉成立才改变裁定，驳回即解除锁定。
"""

from __future__ import annotations

from .errors import (
    AppealLockError,
    AuthorizationError,
    NotFoundError,
)
from .models import (
    Appeal,
    AppealStatus,
    AppealTarget,
    CandidacyStatus,
    Role,
    utc_now,
)
from .store import AwardStore


class AppealService:
    def __init__(self, store: AwardStore):
        self.store = store

    def _staff(self, actor_id: str, roles: tuple[Role, ...]):
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise AuthorizationError(f"操作者未登记：{actor_id}")
        if actor.role not in roles:
            raise AuthorizationError(f"{actor.name}无权执行该操作")
        return actor

    # -- 受理与锁定 ----------------------------------------------------------

    def file_appeal(
        self,
        actor_id: str,
        target_type: AppealTarget,
        reason: str,
        *,
        candidacy_id: str | None = None,
        work_id: str | None = None,
        version_id: str | None = None,
        score_ids: list[str] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> str:
        """受理申诉并立即锁定被质疑的资格、文本或评分证据。"""
        staff = self._staff(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        if not reason.strip():
            raise ValueError("申诉理由不能为空")

        if candidacy_id:
            self.store.candidacy(candidacy_id)
        if work_id:
            work = self.store.work(work_id)
            if version_id and work.get_version(version_id) is None:
                raise NotFoundError(f"文本版本不存在：{version_id}")
        locked_scores: list[str] = []
        for score_id in score_ids or []:
            score = self.store.scores.get(score_id)
            if score is None:
                raise NotFoundError(f"评分证据不存在：{score_id}")
            if not score.locked_by_appeal:
                score.locked_by_appeal = True
            locked_scores.append(score_id)

        if target_type == AppealTarget.QUALIFICATION and not candidacy_id:
            raise ValueError("资格类申诉必须指明候选")
        if target_type == AppealTarget.TEXT and not (work_id and version_id):
            raise ValueError("文本类申诉必须指明作品版本")
        if target_type == AppealTarget.SCORE and not locked_scores:
            raise ValueError("评分类申诉必须指明被质疑的评分证据")

        appeal_id = f"APL-{len(self.store.appeals) + 1:03d}"
        appeal = Appeal(
            appeal_id=appeal_id,
            target_type=target_type,
            candidacy_id=candidacy_id,
            work_id=work_id,
            version_id=version_id,
            score_ids=locked_scores,
            reason=reason,
            submitted_at=utc_now(),
            evidence_refs=evidence_refs or [],
        )
        self.store.appeals[appeal_id] = appeal

        # 精确上锁
        if candidacy_id:
            candidacy = self.store.candidacy(candidacy_id)
            candidacy.lock_reasons.append(f"{appeal_id}:{target_type.value}")
        self.store.log(
            staff.actor_id, "受理申诉并锁定证据", appeal_id,
            f"{target_type.value} 候选{candidacy_id or '-'} 版本{version_id or '-'} 评分{len(locked_scores)}条",
        )
        return appeal_id

    # -- 裁定 ----------------------------------------------------------------

    def resolve_appeal(
        self,
        actor_id: str,
        appeal_id: str,
        upheld: bool,
        resolution_note: str,
    ) -> Appeal:
        """组委会作出裁定。

        成立：按质疑对象改变裁定（撤销资格/排除评分），并登记受影响候选；
        驳回：解除锁定。被锁评分在裁定前不得修改、删除或用于确认。
        """
        staff = self._staff(actor_id, (Role.ORGANIZER, Role.AUTHORIZED_OFFICER))
        appeal = self.store.appeals.get(appeal_id)
        if appeal is None:
            raise NotFoundError(f"申诉不存在：{appeal_id}")
        if appeal.status != AppealStatus.OPEN:
            raise AppealLockError("申诉已裁定")
        if not resolution_note.strip():
            raise ValueError("裁定意见不能为空")

        appeal.status = AppealStatus.UPHELD if upheld else AppealStatus.REJECTED
        appeal.resolution_note = resolution_note
        appeal.resolved_at = utc_now()

        affected_candidacies: set[str] = set()
        if appeal.candidacy_id:
            affected_candidacies.add(appeal.candidacy_id)

        if upheld:
            if appeal.target_type == AppealTarget.SCORE:
                for score_id in appeal.score_ids:
                    score = self.store.scores[score_id]
                    score.excluded = True
                    score.locked_by_appeal = False
                    # 找出评分对应候选，登记本申诉改变了其评分依据
                    for candidacy in self.store.candidacies.values():
                        if candidacy.work_id == score.work_id:
                            affected_candidacies.add(candidacy.candidacy_id)
            elif appeal.target_type in (AppealTarget.QUALIFICATION, AppealTarget.TEXT):
                # 资格或文本不成立：撤销相关候选资格，奖项确认环节将拒绝其获奖
                if appeal.candidacy_id:
                    candidacy = self.store.candidacy(appeal.candidacy_id)
                    candidacy.status = CandidacyStatus.REVOKED
                else:
                    # 纯文本质疑波及使用同一文本（含现场沿用文本的新版本）的全部候选
                    challenged_work = self.store.works.get(appeal.work_id)
                    challenged_hash = (
                        challenged_work.get_version(appeal.version_id).text_hash
                        if challenged_work and challenged_work.get_version(appeal.version_id)
                        else None
                    )
                    for candidacy in self.store.candidacies.values():
                        if candidacy.work_id != appeal.work_id:
                            continue
                        work = self.store.work(candidacy.work_id)
                        version = work.get_version(
                            candidacy.effective_version_id or candidacy.version_id
                        )
                        same_text = (
                            version is not None
                            and challenged_hash is not None
                            and version.text_hash == challenged_hash
                        )
                        if same_text and candidacy.status != CandidacyStatus.REVOKED:
                            candidacy.status = CandidacyStatus.REVOKED
                            affected_candidacies.add(candidacy.candidacy_id)
                for score_id in appeal.score_ids:
                    self.store.scores[score_id].locked_by_appeal = False
        else:
            for score_id in appeal.score_ids:
                self.store.scores[score_id].locked_by_appeal = False

        appeal.changed_candidacies = sorted(affected_candidacies)
        for candidacy_id in affected_candidacies:
            candidacy = self.store.candidacy(candidacy_id)
            candidacy.effects.append(
                {
                    "appeal_id": appeal_id,
                    "upheld": upheld,
                    "target": appeal.target_type.value,
                    "note": resolution_note,
                }
            )
            if candidacy.lock_reasons:
                candidacy.lock_reasons = [
                    r for r in candidacy.lock_reasons if not r.startswith(f"{appeal_id}:")
                ]

        self.store.log(
            staff.actor_id,
            "申诉成立改变裁定" if upheld else "申诉驳回解除锁定",
            appeal_id,
            f"影响候选：{appeal.changed_candidacies}",
        )
        return appeal

    def withdraw_appeal(self, actor_id: str, appeal_id: str) -> None:
        """撤回申诉，解除其锁定。"""
        self._staff(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        appeal = self.store.appeals.get(appeal_id)
        if appeal is None or appeal.status != AppealStatus.OPEN:
            raise AppealLockError("申诉不存在或已裁定")
        appeal.status = AppealStatus.WITHDRAWN
        appeal.resolved_at = utc_now()
        for score_id in appeal.score_ids:
            self.store.scores[score_id].locked_by_appeal = False
        if appeal.candidacy_id:
            candidacy = self.store.candidacy(appeal.candidacy_id)
            candidacy.lock_reasons = [
                r for r in candidacy.lock_reasons if not r.startswith(f"{appeal_id}:")
            ]
        self.store.log(actor_id, "撤回申诉", appeal_id, "")

    # -- 锁定查询（奖项确认前调用）------------------------------------------

    def candidacy_locked(self, candidacy_id: str) -> list[str]:
        """返回候选当前被受理中申诉锁定的原因；空列表表示未锁定。"""
        candidacy = self.store.candidacy(candidacy_id)
        return list(candidacy.lock_reasons)

    def version_locked(self, version_id: str) -> list[str]:
        """版本文本是否被受理中的文本类申诉锁定。"""
        return [
            a.appeal_id for a in self.store.appeals.values()
            if a.status == AppealStatus.OPEN
            and a.target_type == AppealTarget.TEXT
            and a.version_id == version_id
        ]

    def open_appeals(self) -> list[Appeal]:
        return [a for a in self.store.appeals.values() if a.status == AppealStatus.OPEN]
