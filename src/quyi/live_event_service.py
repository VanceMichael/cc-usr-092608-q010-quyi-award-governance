"""现场终评事件服务：换角、中止、重新表演。

新增的现场事实会：
- 在作品下生成新的节目版本（文本谱系中旧版本原样保留、仍可查看）；
- 归档旧版本评分（不计入现行评分，但证据保留）；
- 更新相关候选的现行版本与资格（如换角后原表演候选资格丧失、新演员建立候选）。
"""

from __future__ import annotations

from datetime import datetime

from .archive_service import ArchiveService
from .errors import AuthorizationError
from .jury_service import JuryService
from .models import (
    AwardCategory,
    Candidacy,
    CandidacyStatus,
    CastAppearance,
    CrewStatus,
    EventType,
    LiveEvent,
    Role,
    TextVersion,
    WorkRelation,
    utc_now,
)
from .qualification import build_qualification_snapshot
from .store import AwardStore


class LiveEventService:
    def __init__(self, store: AwardStore, archive: ArchiveService, jury: JuryService):
        self.store = store
        self.archive = archive
        self.jury = jury

    def _organizer(self, actor_id: str):
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise AuthorizationError(f"操作者未登记：{actor_id}")
        if actor.role not in (Role.ORGANIZER, Role.AUTHORIZED_OFFICER):
            raise AuthorizationError(f"{actor.name}无权登记现场事件")
        return actor

    def _new_performance_version(
        self, actor_id: str, work_id: str, kind: EventType, detail: str
    ) -> TextVersion:
        """现场事实产生新节目版本：沿用现行文本指纹（换文本须另走文本沿革）。"""
        work = self.store.work(work_id)
        base = work.versions[-1]
        version = TextVersion(
            version_id=f"{work_id}-V{len(work.versions) + 1}",
            title=base.title,
            text_hash=base.text_hash,
            note=f"现场{kind.value}产生的新版本：{detail}",
            recorded_at=utc_now(),
            recorded_by=actor_id,
            first_performed_at=base.first_performed_at,
            originality_declared=base.originality_declared,
            originality_note=base.originality_note,
        )
        work.versions.append(version)
        return version

    def _candidacies_for(self, round_id: str, work_id: str) -> list[Candidacy]:
        return [
            c for c in self.store.candidacies.values()
            if c.source_round_id == round_id and c.work_id == work_id
        ]

    def _reached_through_sealed_round(self, work_id: str) -> bool:
        """该作品至少有一条候选来自已封存轮次（即通过初评/分赛区结论进入终评）。"""
        for candidacy in self.store.candidacies.values():
            if candidacy.work_id != work_id:
                continue
            source = self.store.rounds.get(candidacy.source_round_id)
            if source is not None and source.sealed is not None:
                return True
        return False

    def record_event(
        self,
        actor_id: str,
        round_id: str,
        work_id: str,
        kind: EventType,
        detail: str,
        *,
        occurred_at: datetime | None = None,
        outgoing_person_id: str | None = None,
        incoming_person_id: str | None = None,
    ) -> LiveEvent:
        """登记现场终评事件并立即施加资格与评分影响。"""
        actor = self._organizer(actor_id)
        round_record = self.store.round(round_id)
        if not self._reached_through_sealed_round(work_id):
            raise AuthorizationError("该作品未经已封存轮次的晋级结论进入终评，不能登记现场事件")
        if not self._candidacies_for(round_id, work_id):
            raise AuthorizationError("该作品在本轮次没有候选档案，不能登记现场事件")
        work = self.store.work(work_id)
        old_version_id = work.versions[-1].version_id
        occurred_at = occurred_at or utc_now()

        # 中止：不产生新版本，暂停该节目评分，等待后续"重新表演"
        if kind == EventType.SUSPENSION:
            event = LiveEvent(
                event_id=f"LIVE-{len(self.store.live_events) + 1:03d}",
                round_id=round_id,
                work_id=work_id,
                version_id=old_version_id,
                kind=kind,
                occurred_at=occurred_at,
                detail=detail,
            )
            for candidacy in self._candidacies_for(round_id, work_id):
                candidacy.effects.append(
                    {"event_id": event.event_id, "effect": "评分中止暂停", "at": occurred_at}
                )
            self.store.live_events.append(event)
            self.store.log(actor_id, "现场中止", work_id, detail)
            return event

        new_version = self._new_performance_version(actor_id, work_id, kind, detail)
        event = LiveEvent(
            event_id=f"LIVE-{len(self.store.live_events) + 1:03d}",
            round_id=round_id,
            work_id=work_id,
            version_id=old_version_id,
            kind=kind,
            occurred_at=occurred_at,
            detail=detail,
            new_version_id=new_version.version_id,
        )

        if kind == EventType.CAST_CHANGE:
            if not outgoing_person_id or not incoming_person_id:
                raise ValueError("换角必须指明退出与接替演员")
            self._apply_cast_change(
                actor.actor_id, round_id, work_id, old_version_id, new_version.version_id,
                outgoing_person_id, incoming_person_id, event,
            )
        elif kind == EventType.REPERFORMANCE:
            self._apply_reperformance(
                actor.actor_id, round_id, work_id, new_version.version_id, event
            )

        self.store.live_events.append(event)
        self.store.log(
            actor_id, f"现场{kind.value}", work_id,
            f"{old_version_id}->{new_version.version_id}：{detail}",
        )
        return event

    def _apply_cast_change(
        self,
        actor_id: str,
        round_id: str,
        work_id: str,
        old_version_id: str,
        new_version_id: str,
        outgoing_person_id: str,
        incoming_person_id: str,
        event: LiveEvent,
    ) -> None:
        outgoing = self.store.person(outgoing_person_id)
        incoming = self.store.person(incoming_person_id)

        # 演职关系随版本更新：旧版本演员标记退出，新版本登记接替演员
        for appearance in outgoing.appearances:
            if appearance.work_id == work_id and appearance.relation == WorkRelation.PERFORMER:
                appearance.status = CrewStatus.WITHDRAWN
        incoming_org = incoming.org_at(event.occurred_at.date())
        incoming.appearances.append(
            CastAppearance(
                work_id=work_id,
                version_id=new_version_id,
                relation=WorkRelation.PERFORMER,
                org_id=incoming_org,
                status=CrewStatus.ACTIVE,
                detail=f"换角接替{outgoing_person_id}（事件{event.event_id}）",
            )
        )

        # 旧版本评分归档；换角后以新版本重新评分
        self.jury.supersede_version_scores(actor_id, round_id, work_id, old_version_id)

        replaced_categories: set[AwardCategory] = set()
        for candidacy in self._candidacies_for(round_id, work_id):
            candidacy.effective_version_id = new_version_id
            effect = {
                "event_id": event.event_id,
                "old_version_id": old_version_id,
                "new_version_id": new_version_id,
            }
            if candidacy.person_id == outgoing_person_id and candidacy.category.is_person_award:
                # 原演员不再参演该节目版本，个人奖项候选资格丧失
                candidacy.status = CandidacyStatus.REVOKED
                effect["effect"] = "换角：原演员候选资格丧失"
                replaced_categories.add(candidacy.category)
            else:
                effect["effect"] = "换角：现行版本切换"
            candidacy.effects.append(effect)

        # 为接替演员建立同类个人候选（资格细则由奖项校验环节统一判定）
        for category in sorted(replaced_categories, key=lambda c: c.value):
            new_id = f"C-{len(self.store.candidacies) + 1:04d}"
            self.store.candidacies[new_id] = Candidacy(
                candidacy_id=new_id,
                category=category,
                work_id=work_id,
                version_id=new_version_id,
                person_id=incoming_person_id,
                source_round_id=round_id,
                effective_version_id=new_version_id,
                qualification_snapshot=build_qualification_snapshot(
                    self.store, category, work_id, new_version_id, incoming_person_id
                ),
            )
            event.effects.append(f"新建候选{new_id}（接替演员{incoming_person_id}）")

    def _apply_reperformance(
        self,
        actor_id: str,
        round_id: str,
        work_id: str,
        new_version_id: str,
        event: LiveEvent,
    ) -> None:
        old_version_id = event.version_id
        # 中止后重新表演：旧版本评分全部归档，对新版本重新打分
        self.jury.supersede_version_scores(actor_id, round_id, work_id, old_version_id)
        for candidacy in self._candidacies_for(round_id, work_id):
            candidacy.effective_version_id = new_version_id
            candidacy.effects.append(
                {
                    "event_id": event.event_id,
                    "effect": "重新表演：原评分归档，以新版本重新评分",
                    "old_version_id": old_version_id,
                    "new_version_id": new_version_id,
                }
            )
        event.effects.append(f"版本{old_version_id}评分归档，改按{new_version_id}评分")

    # -- 查询 ----------------------------------------------------------------

    def events_for(self, work_id: str) -> list[LiveEvent]:
        return [e for e in self.store.live_events if e.work_id == work_id]

    def is_suspended(self, round_id: str, work_id: str, at: datetime | None = None) -> bool:
        """某节目在终评中是否处于中止未恢复状态（最近相关事件为中止）。"""
        events = [
            e for e in self.store.live_events
            if e.round_id == round_id and e.work_id == work_id
            and (at is None or e.occurred_at <= at)
        ]
        if not events:
            return False
        return events[-1].kind == EventType.SUSPENSION

    def effective_version(self, candidacy: Candidacy) -> str:
        return candidacy.effective_version_id or candidacy.version_id
