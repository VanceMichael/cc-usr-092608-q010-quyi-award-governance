"""公示服务：公示期与系统停机顺延。

规则：公示期跨越系统停机时，剩余公示期限不得被缩短——
每一段与公示窗口相交的停机时长，都等额追加到原定截止时间之后。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .errors import (
    AuthorizationError,
    PublicityError,
    TimelineError,
)
from .models import (
    DownTime,
    Publicity,
    PublicityStatus,
    Role,
    utc_now,
)
from .store import AwardStore


class PublicityService:
    def __init__(self, store: AwardStore):
        self.store = store

    def _staff(self, actor_id: str, roles: tuple[Role, ...]):
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise AuthorizationError(f"操作者未登记：{actor_id}")
        if actor.role not in roles:
            raise AuthorizationError(f"{actor.name}无权执行该操作")
        return actor

    def start_publicity(
        self,
        actor_id: str,
        publicity_id: str,
        started_at: datetime,
        planned_deadline: datetime,
    ) -> Publicity:
        """开启公示。原定截止时间不得早于开始时间。"""
        self._staff(actor_id, (Role.ORGANIZER, Role.SECRETARIAT))
        if publicity_id in self.store.publicities:
            raise ValueError(f"公示标识已存在：{publicity_id}")
        if planned_deadline <= started_at:
            raise TimelineError("原定截止时间必须晚于公示开始时间")
        publicity = Publicity(
            publicity_id=publicity_id,
            started_at=started_at,
            planned_deadline=planned_deadline,
            status=PublicityStatus.ONGOING,
        )
        self.store.publicities[publicity_id] = publicity
        self.store.log(
            actor_id, "开始公示", publicity_id,
            f"{started_at:%Y-%m-%d %H:%M}~{planned_deadline:%Y-%m-%d %H:%M}",
        )
        return publicity

    def report_downtime_start(
        self,
        actor_id: str,
        publicity_id: str,
        started_at: datetime,
        reason: str = "",
    ) -> None:
        """登记系统停机开始。停机可在公示进行中随时登记（含事后补报）。"""
        self._staff(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        publicity = self._ongoing(publicity_id)
        if any(d.ended_at is None for d in publicity.downtimes):
            raise TimelineError("已有一段停机尚未登记恢复时间")
        publicity.downtimes.append(DownTime(started_at=started_at, reason=reason))
        self.store.log(actor_id, "系统停机开始", publicity_id, f"{started_at}：{reason}")

    def report_downtime_end(
        self, actor_id: str, publicity_id: str, ended_at: datetime
    ) -> timedelta:
        """登记停机恢复，返回该段停机被计入顺延的时长。"""
        self._staff(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        publicity = self._ongoing(publicity_id)
        open_down = next((d for d in publicity.downtimes if d.ended_at is None), None)
        if open_down is None:
            raise TimelineError("没有进行中的停机记录")
        if ended_at < open_down.started_at:
            raise TimelineError("恢复时间不能早于停机开始时间")
        open_down.ended_at = ended_at
        granted = self._overlap(publicity, open_down.started_at, ended_at)
        self.store.log(
            actor_id, "系统停机恢复", publicity_id,
            f"{ended_at}，计入顺延{granted.total_seconds():.0f}秒",
        )
        return granted

    def _ongoing(self, publicity_id: str) -> Publicity:
        publicity = self.store.publicities.get(publicity_id)
        if publicity is None:
            raise PublicityError(f"公示不存在：{publicity_id}")
        if publicity.status != PublicityStatus.ONGOING:
            raise PublicityError("公示不在进行中，不能登记停机")
        return publicity

    @staticmethod
    def _overlap(publicity: Publicity, start: datetime, end: datetime) -> timedelta:
        """停机区间与原定公示窗口的交集；公示窗口外的停机不顺延。"""
        lo = max(start, publicity.started_at)
        hi = min(end, publicity.planned_deadline)
        return hi - lo if hi > lo else timedelta(0)

    def downtime_extension(self, publicity_id: str) -> timedelta:
        """已登记停机为公示期争取到的总顺延（逐段求和，区间不得重叠）。"""
        publicity = self.store.publicities.get(publicity_id)
        if publicity is None:
            raise PublicityError(f"公示不存在：{publicity_id}")
        finished = [d for d in publicity.downtimes if d.ended_at is not None]
        total = timedelta(0)
        # 区间重叠检查，防止同一停机被重复计入
        ordered = sorted(finished, key=lambda d: d.started_at)
        last_end: datetime | None = None
        for down in ordered:
            if last_end is not None and down.started_at < last_end:
                raise TimelineError("停机区间重叠，顺延时长无法确定")
            total += self._overlap(publicity, down.started_at, down.ended_at)  # type: ignore[arg-type]
            last_end = down.ended_at
        return total

    def effective_deadline(self, publicity_id: str) -> datetime:
        """实际截止时间 = 原定截止 + 停机顺延，公示剩余期限不被缩短。"""
        publicity = self.store.publicities.get(publicity_id)
        if publicity is None:
            raise PublicityError(f"公示不存在：{publicity_id}")
        return publicity.planned_deadline + self.downtime_extension(publicity_id)

    def remaining(self, publicity_id: str, at: datetime | None = None) -> timedelta:
        """按停机顺延后的口径计算剩余公示期限。"""
        publicity = self.store.publicities.get(publicity_id)
        if publicity is None:
            raise PublicityError(f"公示不存在：{publicity_id}")
        at = at or utc_now()
        return self.effective_deadline(publicity_id) - at

    def end_publicity(self, actor_id: str, publicity_id: str, at: datetime | None = None) -> datetime:
        """结束公示。

        不允许提前结束：只有到达顺延后的实际截止时间方可关闭；
        仍有未恢复的停机时也不能结束。返回实际截止时间。
        """
        self._staff(actor_id, (Role.ORGANIZER,))
        publicity = self._ongoing(publicity_id)
        if any(d.ended_at is None for d in publicity.downtimes):
            raise TimelineError("尚有停机未恢复，不能结束公示")
        deadline = self.effective_deadline(publicity_id)
        at = at or utc_now()
        if at < deadline:
            raise TimelineError(
                f"公示剩余期限不得缩短，实际截止时间为 {deadline:%Y-%m-%d %H:%M}"
            )
        publicity.status = PublicityStatus.ENDED
        publicity.ended_at = at
        self.store.log(actor_id, "公示结束", publicity_id, f"截止{deadline}，结束{at}")
        return deadline
