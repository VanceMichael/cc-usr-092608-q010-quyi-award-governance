"""轮次服务：初评、分赛区、现场终评的材料登记、晋级结论与封存。

封存规则：
- 初评与分赛区结束时分别封存材料与晋级结论，生成不可变指纹快照；
- 封存后原轮次材料、结论一律不得改写（任何尝试抛出 SealedError）；
- 后补证明只能进入"资格复核"，附在候选资格上供后续轮次使用，
  既不进入原轮次材料清单，也不改变封存指纹与原晋级结论。
"""

from __future__ import annotations

from .errors import (
    AuthorizationError,
    NotFoundError,
    SealedError,
)
from .models import (
    Advancement,
    AwardCategory,
    Candidacy,
    CandidacyStatus,
    Role,
    RoundKind,
    RoundRecord,
    RoundSeal,
    SupplementaryEvidence,
    fingerprint,
    utc_now,
)
from .store import AwardStore


class RoundService:
    def __init__(self, store: AwardStore):
        self.store = store

    def _actor(self, actor_id: str, roles: tuple[Role, ...]):
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise AuthorizationError(f"操作者未登记：{actor_id}")
        if actor.role not in roles:
            raise AuthorizationError(f"{actor.name}无权执行该操作")
        return actor

    def _mutable(self, round_record: RoundRecord) -> None:
        if round_record.sealed is not None:
            raise SealedError(f"{round_record.name}已封存，原轮次材料与结论不得改写")

    # -- 轮次与材料 ----------------------------------------------------------

    def create_round(
        self,
        actor_id: str,
        round_id: str,
        kind: RoundKind,
        name: str,
        region: str | None = None,
        year: int = 0,
    ) -> RoundRecord:
        self._actor(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        if round_id in self.store.rounds:
            raise ValueError(f"轮次标识已存在：{round_id}")
        if kind == RoundKind.REGIONAL and not region:
            raise ValueError("分赛区轮次必须注明赛区")
        record = RoundRecord(
            round_id=round_id, kind=kind, name=name, region=region, year=year,
            started_at=utc_now(),
        )
        self.store.rounds[round_id] = record
        self.store.log(actor_id, "创建轮次", round_id, name)
        return record

    def add_material(
        self,
        actor_id: str,
        round_id: str,
        doc_ref: str,
        content_hash: str,
        category: str,
    ) -> None:
        """登记轮次材料。封存后拒绝追加（后补材料走资格复核）。"""
        self._actor(actor_id, (Role.SECRETARIAT,))
        round_record = self.store.round(round_id)
        self._mutable(round_record)
        if not doc_ref.strip() or not content_hash.strip():
            raise ValueError("材料编号与内容指纹不能为空")
        round_record.materials.append(
            {
                "doc_ref": doc_ref,
                "content_hash": content_hash,
                "category": category,
                "recorded_at": utc_now(),
                "recorded_by": actor_id,
            }
        )
        self.store.log(actor_id, "登记材料", round_id, doc_ref)

    def check_materials_complete(self, actor_id: str, round_id: str) -> list[str]:
        """秘书处的完整性检查：只看材料清单齐不齐，看不到评分身份。

        返回缺失项列表；空列表表示完整。该方法刻意不接触评委匿名映射。
        """
        self._actor(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        round_record = self.store.round(round_id)
        required = {"报名表", "文本", "原创声明", "权利人证明"}
        if round_record.kind in (RoundKind.REGIONAL, RoundKind.FINAL):
            required.add("参赛确认")
        present = {m["category"] for m in round_record.materials}
        missing = sorted(required - present)
        self.store.log(actor_id, "完整性检查", round_id, f"缺失：{missing}")
        return missing

    # -- 晋级结论与候选 ------------------------------------------------------

    def record_advancement(
        self,
        actor_id: str,
        round_id: str,
        candidacy_id: str,
        category: AwardCategory,
        work_id: str,
        version_id: str,
        person_id: str | None,
        advanced: bool,
        note: str = "",
    ) -> Candidacy:
        """登记一条晋级/淘汰结论，并同步建立奖项候选档案。"""
        self._actor(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        round_record = self.store.round(round_id)
        self._mutable(round_record)
        if candidacy_id in round_record.advancements or candidacy_id in self.store.candidacies:
            raise ValueError(f"候选标识已存在：{candidacy_id}")

        work = self.store.work(work_id)
        if work.get_version(version_id) is None:
            raise NotFoundError(f"文本版本不存在：{version_id}")
        if category.is_person_award:
            if person_id is None:
                raise ValueError("个人类奖项候选必须指明人员")
            self.store.person(person_id)

        candidacy = Candidacy(
            candidacy_id=candidacy_id,
            category=category,
            work_id=work_id,
            version_id=version_id,
            person_id=person_id,
            source_round_id=round_id,
            status=CandidacyStatus.PENDING,
            qualification_snapshot=self._snapshot_qualification(
                category, work_id, version_id, person_id
            ),
        )
        self.store.candidacies[candidacy_id] = candidacy
        round_record.advancements[candidacy_id] = Advancement(
            candidacy_id=candidacy_id,
            category=category,
            work_id=work_id,
            version_id=version_id,
            person_id=person_id,
            advanced=advanced,
            note=note,
        )
        self.store.log(
            actor_id, "登记晋级结论", round_id,
            f"{candidacy_id}/{category.value}/{'晋级' if advanced else '淘汰'}",
        )
        return candidacy

    def _snapshot_qualification(
        self,
        category: AwardCategory,
        work_id: str,
        version_id: str,
        person_id: str | None,
    ) -> dict:
        """在结论登记时冻结资格事实，后续修改不影响该快照。"""
        from .qualification import build_qualification_snapshot

        return build_qualification_snapshot(
            self.store, category, work_id, version_id, person_id
        )

    # -- 封存 ----------------------------------------------------------------

    def seal_round(self, actor_id: str, round_id: str) -> RoundSeal:
        """封存轮次：对材料与晋级结论分别取指纹，事后任何改动都会破坏可校验性。"""
        actor = self._actor(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        round_record = self.store.round(round_id)
        if round_record.sealed is not None:
            raise SealedError(f"{round_record.name}已封存")
        missing = self.check_materials_complete(actor_id, round_id)
        if missing:
            raise SealedError(f"材料不完整，不能封存，缺失：{missing}")
        if not round_record.advancements:
            raise SealedError("没有任何晋级结论，不能封存")

        materials_payload = [
            {k: v for k, v in m.items() if k in ("doc_ref", "content_hash", "category")}
            for m in round_record.materials
        ]
        advancement_payload = [
            {
                "candidacy_id": a.candidacy_id,
                "category": a.category.value,
                "work_id": a.work_id,
                "version_id": a.version_id,
                "person_id": a.person_id,
                "advanced": a.advanced,
                "note": a.note,
            }
            for a in sorted(round_record.advancements.values(), key=lambda a: a.candidacy_id)
        ]
        seal = RoundSeal(
            sealed_at=utc_now(),
            sealed_by=actor.actor_id,
            materials_fingerprint=fingerprint(materials_payload),
            advancement_fingerprint=fingerprint(advancement_payload),
            material_refs=[m["doc_ref"] for m in round_record.materials],
            advancement_summary=advancement_payload,
        )
        round_record.sealed = seal
        for candidacy_id in round_record.advancements:
            self.store.candidacy(candidacy_id)  # 候选均已建档
        self.store.log(
            actor_id, "封存轮次", round_id,
            f"材料{seal.materials_fingerprint[:12]}/结论{seal.advancement_fingerprint[:12]}",
        )
        return seal

    def verify_seal(self, round_id: str) -> bool:
        """重新计算指纹并与封存快照比对，发现事后改写返回 False。"""
        round_record = self.store.round(round_id)
        seal = round_record.sealed
        if seal is None:
            raise NotFoundError("轮次尚未封存")
        materials_payload = [
            {k: v for k, v in m.items() if k in ("doc_ref", "content_hash", "category")}
            for m in round_record.materials
        ]
        advancement_payload = [
            {
                "candidacy_id": a.candidacy_id,
                "category": a.category.value,
                "work_id": a.work_id,
                "version_id": a.version_id,
                "person_id": a.person_id,
                "advanced": a.advanced,
                "note": a.note,
            }
            for a in sorted(round_record.advancements.values(), key=lambda a: a.candidacy_id)
        ]
        return (
            fingerprint(materials_payload) == seal.materials_fingerprint
            and fingerprint(advancement_payload) == seal.advancement_fingerprint
            and seal.material_refs == [m["doc_ref"] for m in round_record.materials]
        )

    # -- 后补证明与资格复核 --------------------------------------------------

    def submit_supplement(
        self,
        actor_id: str,
        candidacy_id: str,
        original_round_id: str,
        doc_ref: str,
        fact_type: str,
        content: str,
    ) -> str:
        """后补证明登记。

        只允许在原轮次封存之后提交；证明进入资格复核队列，
        不写入原轮次材料、不改写原晋级结论与封存指纹。
        """
        self._actor(actor_id, (Role.SECRETARIAT, Role.ORGANIZER))
        candidacy = self.store.candidacy(candidacy_id)
        original_round = self.store.round(original_round_id)
        if original_round.sealed is None:
            raise SealedError("原轮次尚未封存，应直接补交材料而非后补证明")
        evidence_id = f"SUP-{len(self.store.supplements) + 1:03d}"
        supplement = SupplementaryEvidence(
            evidence_id=evidence_id,
            candidacy_id=candidacy_id,
            round_id=original_round_id,
            doc_ref=doc_ref,
            fact_type=fact_type,
            content=content,
            submitted_at=utc_now(),
            submitted_by=actor_id,
        )
        self.store.supplements[evidence_id] = supplement
        self.store.log(
            actor_id, "后补证明进入复核", candidacy_id,
            f"{evidence_id}/{fact_type}（原轮次{original_round_id}不改写）",
        )
        return evidence_id

    def review_supplement(
        self,
        actor_id: str,
        evidence_id: str,
        accepted: bool,
        note: str,
        review_round_id: str,
    ) -> None:
        """复核人员对后补证明作出结论。

        采信的证明只追加到候选资格的"复核增补"中供后续轮次与终评使用，
        原轮次封存快照保持原样；不予采信的证明记录理由，同样不改历史。
        """
        self._actor(actor_id, (Role.REVIEWER, Role.ORGANIZER))
        supplement = self.store.supplements.get(evidence_id)
        if supplement is None:
            raise NotFoundError(f"后补证明不存在：{evidence_id}")
        if supplement.accepted is not None:
            raise ValueError("该证明已复核")
        if not review_round_id.strip():
            raise ValueError("必须注明复核所属阶段")
        supplement.accepted = accepted
        supplement.review_note = note
        supplement.review_round_id = review_round_id

        candidacy = self.store.candidacy(supplement.candidacy_id)
        candidacy.qualification_snapshot.setdefault("review_addenda", []).append(
            {
                "evidence_id": evidence_id,
                "fact_type": supplement.fact_type,
                "content": supplement.content,
                "accepted": accepted,
                "note": note,
                "review_round_id": review_round_id,
                "reviewed_by": actor_id,
                "reviewed_at": utc_now(),
            }
        )
        self.store.log(
            actor_id, "资格复核结论", supplement.candidacy_id,
            f"{evidence_id}/{'采信' if accepted else '不予采信'}（原轮次{supplement.round_id}不变）",
        )

    def pending_supplements(self, actor_id: str) -> list[str]:
        self._actor(actor_id, (Role.REVIEWER, Role.ORGANIZER, Role.SECRETARIAT))
        return [eid for eid, e in self.store.supplements.items() if e.accepted is None]
