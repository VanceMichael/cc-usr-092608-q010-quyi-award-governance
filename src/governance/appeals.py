"""公示与申诉服务。

公示期跨越系统停机时，截止时间按与公示期重叠的停机时长顺延，剩余
期限不得缩短。申诉受理即锁定被质疑的资格/文本/评分证据，锁定范围
限定在所列奖项；无关奖项与候选照常确认。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .records import (
    Appeal,
    AppealStatus,
    AppealTarget,
    Downtime,
    PublicNotice,
)
from .repository import AwardRepository
from .roles import GovernanceError, PermissionDenied, Role


class AppealService:
    def __init__(self, repo: AwardRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------ 公示与停机

    def open_notice(
        self,
        notice_id: str,
        started_at: datetime,
        original_end: datetime,
        actor_role: Role,
    ) -> PublicNotice:
        if actor_role is not Role.ORGANIZING_COMMITTEE:
            raise PermissionDenied("公示由组委会发布")
        if original_end <= started_at:
            raise GovernanceError("公示截止时间必须晚于开始时间")
        notice = PublicNotice(
            id=notice_id,
            started_at=started_at,
            original_end=original_end,
            adjusted_end=original_end,
        )
        self.repo.notices[notice_id] = notice
        return notice

    def register_downtime(
        self, notice_id: str, downtime: Downtime
    ) -> float:
        """登记停机窗口，按与当前公示期的重叠秒数顺延截止时间。

        完全处于公示期之外的停机不产生顺延；多次停机分别累计，
        保证公示剩余期限不被缩短。
        """
        notice = self.repo.notices.get(notice_id)
        if notice is None:
            raise GovernanceError("公示不存在")
        if notice.confirmed:
            raise GovernanceError("公示已确认，不得再登记停机")
        if downtime.end <= downtime.start:
            raise GovernanceError("停机窗口无效")
        overlap_start = max(notice.started_at, downtime.start)
        overlap_end = min(notice.adjusted_end, downtime.end)
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        if overlap_seconds <= 0:
            return 0.0
        notice.adjusted_end = notice.adjusted_end + timedelta(seconds=overlap_seconds)
        notice.extensions.append((downtime, overlap_seconds))
        return overlap_seconds

    # ------------------------------------------------------------ 申诉

    def file_appeal(
        self,
        appeal_id: str,
        target: AppealTarget,
        target_ref: str,
        version_id: str,
        categories: tuple[str, ...],
        locked_evidence: tuple[str, ...],
        filed_at: datetime,
        actor_role: Role,
    ) -> Appeal:
        """提交申诉并立即锁定所列证据。

        锁定范围最小化：仅影响该版本所列奖项；其他版本、其他奖项的
        确认流程照常进行。
        """
        if actor_role is not Role.PARTICIPANT:
            raise PermissionDenied("申诉由参评作者与演员提出")
        if version_id not in self.repo.versions:
            raise GovernanceError("被申诉版本不存在")
        if not categories:
            raise GovernanceError("申诉必须指明受影响奖项范围")
        if not locked_evidence:
            raise GovernanceError("申诉必须指明被质疑并锁定的证据")
        appeal = Appeal(
            id=appeal_id,
            target=target,
            target_ref=target_ref,
            version_id=version_id,
            categories=tuple(categories),
            filed_at=filed_at,
            locked_evidence=tuple(locked_evidence),
        )
        self.repo.appeals[appeal_id] = appeal
        return appeal

    def decide_appeal(
        self,
        appeal_id: str,
        upheld: bool,
        decided_by: str,
        decided_at: datetime,
        actor_role: Role,
        decision: str,
        change_summary: str | None = None,
    ) -> Appeal:
        """裁定申诉。成立时必须说明改变了什么裁定。

        具体勘误（资格复核案件、重复关联确认、评分更正）由相应服务
        另行登记，本裁定只引用结论，原始记录一律保留。
        """
        if actor_role is not Role.SUPERVISOR:
            raise PermissionDenied("申诉由监审资格组裁定")
        appeal = self.repo.appeals.get(appeal_id)
        if appeal is None:
            raise GovernanceError("申诉不存在")
        if appeal.status is not AppealStatus.OPEN:
            raise GovernanceError("申诉已有裁定")
        appeal.status = AppealStatus.UPHELD if upheld else AppealStatus.REJECTED
        appeal.decided_by = decided_by
        appeal.decided_at = decided_at
        appeal.decision = decision
        if upheld and not change_summary:
            raise GovernanceError("申诉成立必须记录裁定改变内容")
        appeal.change_summary = change_summary
        return appeal
