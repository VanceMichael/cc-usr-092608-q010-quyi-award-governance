"""现场终评服务。

换角、中止、重新表演均作为只追加的现场事件登记，并更新对应候选的资格
与计分状态：换角取消被替演员的表演资格、替演须先合格；中止不计分；
重新表演按新尝试计分。原节目版本与既往评分始终保留可查看。
"""

from __future__ import annotations

from datetime import date, datetime

from .eligibility import EligibilityService, NEWCOMER_AWARD, PERFORMANCE_AWARD
from .records import (
    CandidateStanding,
    FinalsEvent,
    FinalsEventKind,
    PerformanceRole,
)
from .repository import AwardRepository
from .roles import GovernanceError, PermissionDenied, Role


class FinalsService:
    def __init__(self, repo: AwardRepository, eligibility: EligibilityService) -> None:
        self.repo = repo
        self.eligibility = eligibility

    def admit_candidate(
        self,
        version_id: str,
        category: str,
        person_id: str | None,
        actor_role: Role,
        admitted_at: datetime,
    ) -> CandidateStanding:
        """候选进入现场终评时建立计分状态。"""
        if actor_role not in (Role.ORGANIZING_COMMITTEE, Role.SUPERVISOR):
            raise PermissionDenied("候选入场由组委会登记")
        if version_id not in self.repo.versions:
            raise GovernanceError("版本不存在")
        standing = self.repo.standing(version_id, category, person_id)
        standing.history.append(
            f"{admitted_at.isoformat()} 进入现场终评（{category}）"
        )
        return standing

    def record_event(
        self,
        event_id: str,
        session_id: str,
        version_id: str,
        kind: FinalsEventKind,
        occurred_at: datetime,
        attempt: int,
        detail: str,
        actor_role: Role,
        person_out: str | None = None,
        person_in: str | None = None,
        newcomer_deadline: date | None = None,
    ) -> FinalsEvent:
        if actor_role is not Role.ORGANIZING_COMMITTEE:
            raise PermissionDenied("现场事实由组委会登记")
        if version_id not in self.repo.versions:
            raise GovernanceError("版本不存在")
        event = FinalsEvent(
            id=event_id,
            session_id=session_id,
            version_id=version_id,
            kind=kind,
            occurred_at=occurred_at,
            attempt=attempt,
            detail=detail,
            person_out=person_out,
            person_in=person_in,
        )
        if kind is FinalsEventKind.SUSPENSION:
            event.scored = False
        self.repo.finals_events.append(event)
        self._apply_event(event, newcomer_deadline)
        return event

    def _apply_event(self, event: FinalsEvent, newcomer_deadline: date | None) -> None:
        if event.kind is FinalsEventKind.CAST_CHANGE:
            self._apply_cast_change(event, newcomer_deadline)
        elif event.kind is FinalsEventKind.SUSPENSION:
            self._apply_suspension(event)
        elif event.kind is FinalsEventKind.RE_PERFORMANCE:
            self._apply_reperformance(event)

    def _apply_cast_change(
        self, event: FinalsEvent, newcomer_deadline: date | None
    ) -> None:
        if not event.person_out or not event.person_in:
            raise GovernanceError("换角事件须登记被替与替演人员")
        performers = {
            p.person_id
            for p in self.repo.participations_for(event.version_id)
            if p.role is PerformanceRole.PERFORMER
        }
        if event.person_out not in performers:
            raise GovernanceError("被替演员不在该版本的表演名单中")
        if event.person_in not in performers:
            raise GovernanceError("替演人员未在该版本登记演职关系")

        # 被替演员失去该候选的表演奖与新人奖资格。
        for category, pid in (
            (PERFORMANCE_AWARD, event.person_out),
            (NEWCOMER_AWARD, event.person_out),
        ):
            standing = self.repo.standing(event.version_id, category, pid)
            standing.eligible = False
            standing.history.append(
                f"{event.occurred_at.isoformat()} 换角：被{event.person_in}替换，"
                f"失去{category}候选资格（事件{event.id}）"
            )

        # 替演人员取得计分资格前必须通过对应奖项的资格事实校验。
        deadline = newcomer_deadline or event.occurred_at.date()
        for category in (PERFORMANCE_AWARD, NEWCOMER_AWARD):
            result = self.eligibility.qualification(
                event.version_id, category, deadline, event.person_in
            )
            standing = self.repo.standing(event.version_id, category, event.person_in)
            if result["status"] == "通过":
                standing.eligible = True
                standing.history.append(
                    f"{event.occurred_at.isoformat()} 换角替演，{category}资格核验通过"
                    f"（事件{event.id}）"
                )
            else:
                standing.eligible = False
                standing.note = "替演资格未取得"
                standing.history.append(
                    f"{event.occurred_at.isoformat()} 换角替演，{category}资格结论："
                    f"{result['status']}（事件{event.id}）"
                )

    def _apply_suspension(self, event: FinalsEvent) -> None:
        # 中止的当次尝试不计分；该版本已进入终评的各候选进入待决。
        for standing in self._existing_standings(event.version_id):
            standing.suspension_pending = True
            standing.note = "表演中止，待裁定"
            standing.history.append(
                f"{event.occurred_at.isoformat()} 表演中止，第{event.attempt}次尝试不计分"
                f"（事件{event.id}）"
            )

    def _apply_reperformance(self, event: FinalsEvent) -> None:
        # 重新表演：新尝试计分；此前的中止待决状态解除。
        for standing in self._existing_standings(event.version_id):
            standing.scoring_attempt = event.attempt
            standing.note = None
            standing.suspension_pending = False
            standing.history.append(
                f"{event.occurred_at.isoformat()} 第{event.attempt}次重新表演并计分"
                f"（事件{event.id}）"
            )

    def _existing_standings(self, version_id: str) -> list[CandidateStanding]:
        return [
            standing
            for standing in self.repo.standings.values()
            if standing.version_id == version_id
        ]

    def events_for(self, version_id: str) -> list[FinalsEvent]:
        """按时间返回现场事件；原节目版本与历次尝试都可回看。"""
        return sorted(
            (e for e in self.repo.finals_events if e.version_id == version_id),
            key=lambda e: (e.occurred_at, e.attempt),
        )

    def standing_view(
        self, version_id: str, category: str, person_id: str | None
    ) -> CandidateStanding:
        return self.repo.standing(version_id, category, person_id)
