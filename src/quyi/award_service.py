"""奖项服务：资格校验、名额与兼得规则、获奖确认与全链溯源。

公示前，作品类（节目奖、文学奖）与个人类（表演奖、新人奖）分别校验
名额及兼得规则；获奖一经确认，可沿结果回溯：
作品沿革与重复关联、人员资格事实、各轮封存快照、评委回避原因、
现场事件影响、改变裁定的申诉。
"""

from __future__ import annotations

from datetime import date

from .errors import (
    AppealLockError,
    AuthorizationError,
    NotFoundError,
    QualificationError,
    QuotaError,
)
from .models import (
    AppealStatus,
    AppealTarget,
    AwardCategory,
    Candidacy,
    CandidacyStatus,
    QuotaRule,
    ResultStatus,
    Role,
    utc_now,
)
from .store import AwardStore


def default_quotas() -> dict[AwardCategory, QuotaRule]:
    """本届名额与兼得规则（示例数值，可由组委会重新配置）。

    兼得规则按同一主体判定：
    - 作品类：同一作品不得兼得节目奖与文学奖；
    - 个人类：同一演员不得兼得表演奖与新人奖。
    新人奖另有年龄上限（45 周岁），且曾获新人奖者不再具备新人奖资格。
    """
    return {
        AwardCategory.PROGRAM: QuotaRule(
            AwardCategory.PROGRAM,
            max_winners=10,
            exclusive_with=(AwardCategory.LITERATURE,),
        ),
        AwardCategory.LITERATURE: QuotaRule(
            AwardCategory.LITERATURE,
            max_winners=10,
            exclusive_with=(AwardCategory.PROGRAM,),
        ),
        AwardCategory.PERFORMANCE: QuotaRule(
            AwardCategory.PERFORMANCE,
            max_winners=10,
            exclusive_with=(AwardCategory.NEWCOMER,),
        ),
        AwardCategory.NEWCOMER: QuotaRule(
            AwardCategory.NEWCOMER,
            max_winners=8,
            exclusive_with=(AwardCategory.PERFORMANCE,),
            newcomer_age_limit=45,
            newcomer_exclude_categories=(AwardCategory.NEWCOMER,),
        ),
    }


class AwardService:
    def __init__(self, store: AwardStore):
        self.store = store
        if not store.quotas:
            store.quotas.update(default_quotas())

    # -- 授权与配置 ----------------------------------------------------------

    def _organizer(self, actor_id: str):
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise AuthorizationError(f"操作者未登记：{actor_id}")
        if actor.role != Role.ORGANIZER:
            raise AuthorizationError(f"{actor.name}无权确认或配置奖项")
        return actor

    def configure_quota(self, actor_id: str, rule: QuotaRule) -> None:
        """组委会配置名额与兼得规则。"""
        self._organizer(actor_id)
        self.store.quotas[rule.category] = rule
        self.store.log(
            actor_id, "配置名额规则", rule.category.value,
            f"名额{rule.max_winners}/兼得{[c.value for c in rule.exclusive_with]}",
        )

    # -- 资格事实（含复核增补的采信覆盖）------------------------------------

    @staticmethod
    def _age_at(birth: date, reference: date) -> int:
        return reference.year - birth.year - (
            (reference.month, reference.day) < (birth.month, birth.day)
        )

    def _effective_facts(self, candidacy: Candidacy) -> dict:
        """合并候选冻结快照与资格复核采信的后补证明。

        后补证明不回写原轮次，只在此处作为资格判定依据叠加。
        """
        facts = dict(candidacy.qualification_snapshot)
        for addendum in facts.get("review_addenda", []):
            if not addendum.get("accepted"):
                continue
            fact_type = addendum.get("fact_type", "")
            content = addendum.get("content", "")
            if fact_type == "年龄证明" and content:
                try:
                    facts["birth_date"] = date.fromisoformat(content)
                except ValueError:
                    pass
            elif fact_type == "原创声明":
                facts["originality_declared"] = True
        return facts

    def validate_qualification(
        self, candidacy_id: str, reference_date: date
    ) -> list[str]:
        """返回资格问题清单；空清单表示通过。不抛异常，便于一次性展示。"""
        candidacy = self.store.candidacy(candidacy_id)
        facts = self._effective_facts(candidacy)
        problems: list[str] = []

        if candidacy.status == CandidacyStatus.REVOKED:
            problems.append("候选资格已被撤销（现场换角或申诉裁定）")
        if candidacy.lock_reasons:
            problems.append(f"候选正被申诉锁定：{candidacy.lock_reasons}")
        if self._version_has_open_text_appeal(candidacy):
            problems.append("所依据的文本版本正被文本类申诉锁定")

        if candidacy.category.is_work_award:
            if not facts.get("originality_declared"):
                problems.append("缺少原创声明")
            if not facts.get("rights_holder_ids"):
                problems.append("缺少权利人证明")
            if candidacy.category == AwardCategory.LITERATURE and not facts.get("author_ids"):
                problems.append("文学奖候选缺少作者")
        else:
            person = self.store.person(candidacy.person_id)
            if not facts.get("performed_work"):
                problems.append("候选人员与该作品无演职关系")
            elif not facts.get("performance_active"):
                problems.append("候选人员在现行节目版本中已退出参演")
            if not person.memberships and not facts.get("active_org_ids"):
                problems.append("缺少所属单位证明")

            rule = self.store.quotas[candidacy.category]
            if candidacy.category == AwardCategory.NEWCOMER:
                birth = facts.get("birth_date")
                if birth is None:
                    problems.append("新人奖缺少年龄证明")
                else:
                    age = self._age_at(birth, reference_date)
                    if rule.newcomer_age_limit is not None and age > rule.newcomer_age_limit:
                        problems.append(
                            f"超出新人奖年龄限制（{age}周岁>{rule.newcomer_age_limit}周岁）"
                        )
                blocked = {
                    p["category"] for p in facts.get("prior_awards", [])
                } & {c.value for c in rule.newcomer_exclude_categories}
                if blocked:
                    problems.append(f"曾获{','.join(sorted(blocked))}，不具备新人奖资格")
        return problems

    def _version_has_open_text_appeal(self, candidacy: Candidacy) -> bool:
        """文本类申诉是否波及候选：按作品与文本指纹判断（现场新版本沿用同一文本亦受影响）。"""
        work = self.store.work(candidacy.work_id)
        effective_id = candidacy.effective_version_id or candidacy.version_id
        effective_version = work.get_version(effective_id)
        effective_hash = effective_version.text_hash if effective_version else None
        for appeal in self.store.appeals.values():
            if not (
                appeal.status == AppealStatus.OPEN
                and appeal.target_type == AppealTarget.TEXT
                and appeal.work_id == candidacy.work_id
                and appeal.version_id
            ):
                continue
            challenged = work.get_version(appeal.version_id)
            if challenged is not None and challenged.text_hash == effective_hash:
                return True
        return False

    # -- 名额与兼得 ----------------------------------------------------------

    def confirmed_results(self, category: AwardCategory | None = None) -> list:
        results = [r for r in self.store.results if r.status == ResultStatus.CONFIRMED]
        if category is not None:
            results = [r for r in results if r.category == category]
        return results

    def quota_status(self) -> dict[str, dict]:
        """作品类与个人类各自的名额占用情况。"""
        status: dict[str, dict] = {}
        for category, rule in self.store.quotas.items():
            used = len(self.confirmed_results(category))
            status[category.value] = {
                "group": "作品类" if category.is_work_award else "个人类",
                "used": used,
                "quota": rule.max_winners,
                "remaining": rule.max_winners - used,
            }
        return status

    def _subject_key(self, candidacy: Candidacy) -> str:
        return candidacy.work_id if candidacy.category.is_work_award else candidacy.person_id

    def _exclusivity_conflicts(self, candidacy: Candidacy) -> list[str]:
        rule = self.store.quotas[candidacy.category]
        subject = self._subject_key(candidacy)
        conflicts: list[str] = []
        for result in self.confirmed_results():
            if result.category not in rule.exclusive_with:
                continue
            other = self.store.candidacy(result.candidacy_id)
            if self._subject_key(other) == subject:
                conflicts.append(
                    f"同一主体已确认{result.category.value}（候选{result.candidacy_id}），不得兼得"
                )
        return conflicts

    def can_confirm(self, candidacy_id: str, reference_date: date) -> list[str]:
        """汇总确认前的全部阻断：资格、锁定、名额、兼得。"""
        candidacy = self.store.candidacy(candidacy_id)
        blockers = self.validate_qualification(candidacy_id, reference_date)
        rule = self.store.quotas[candidacy.category]
        used = len(self.confirmed_results(candidacy.category))
        if used >= rule.max_winners:
            blockers.append(
                f"{candidacy.category.value}名额已满（{used}/{rule.max_winners}）"
            )
        blockers.extend(self._exclusivity_conflicts(candidacy))
        return blockers

    # -- 确认与撤销 ----------------------------------------------------------

    def confirm_award(
        self,
        actor_id: str,
        candidacy_id: str,
        reference_date: date,
        rank: int | None = None,
    ):
        """公示前确认获奖。任何阻断项存在都拒绝确认。"""
        actor = self._organizer(actor_id)
        candidacy = self.store.candidacy(candidacy_id)
        blockers = self.can_confirm(candidacy_id, reference_date)
        if blockers:
            if any("申诉" in b or "锁定" in b for b in blockers):
                raise AppealLockError("；".join(blockers))
            if any("名额" in b or "兼得" in b for b in blockers):
                raise QuotaError("；".join(blockers))
            raise QualificationError("；".join(blockers))
        if rank is None:
            rank = len(self.confirmed_results(candidacy.category)) + 1

        from .models import AwardResult

        result = AwardResult(
            candidacy_id=candidacy_id,
            category=candidacy.category,
            work_id=candidacy.work_id,
            person_id=candidacy.person_id,
            confirmed_at=utc_now(),
            rank=rank,
            appeal_ids=self._appeal_ids_for(candidacy),
        )
        self.store.results.append(result)
        candidacy.status = CandidacyStatus.CONFIRMED
        self.store.log(
            actor_id, "确认获奖", candidacy_id,
            f"{candidacy.category.value}/作品{candidacy.work_id}/人员{candidacy.person_id or '-'}",
        )
        return result

    def invalidate_result(
        self, actor_id: str, candidacy_id: str, reason: str, appeal_id: str | None = None
    ) -> None:
        """撤销已确认结果（如公示期申诉成立）。名额随之释放。"""
        actor = self._organizer(actor_id)
        result = next(
            (r for r in self.store.results if r.candidacy_id == candidacy_id), None
        )
        if result is None:
            raise NotFoundError(f"该候选没有获奖结果：{candidacy_id}")
        if result.status != ResultStatus.CONFIRMED:
            raise QuotaError("结果已被撤销")
        result.status = ResultStatus.INVALIDATED
        result.invalidate_reason = reason
        if appeal_id and appeal_id not in result.appeal_ids:
            result.appeal_ids.append(appeal_id)
        self.store.candidacy(candidacy_id).status = CandidacyStatus.REVOKED
        self.store.log(
            actor_id, "撤销获奖结果", candidacy_id, f"{reason}（申诉{appeal_id or '-'}）"
        )

    def _appeal_ids_for(self, candidacy: Candidacy) -> list[str]:
        ids: list[str] = []
        for appeal in self.store.appeals.values():
            if appeal.candidacy_id == candidacy.candidacy_id:
                ids.append(appeal.appeal_id)
            elif appeal.target_type == AppealTarget.SCORE:
                score_work = {
                    self.store.scores[s].work_id for s in appeal.score_ids
                }
                if candidacy.work_id in score_work:
                    ids.append(appeal.appeal_id)
        return sorted(set(ids))

    # -- 全链溯源 ------------------------------------------------------------

    def trace_result(self, candidacy_id: str) -> dict:
        """从一项获奖结果追溯全部依据：

        作品沿革（含改名、重复关联、原创/首演）、人员资格、封存快照、
        现场事件与现行版本、评分（含归档与排除）、评委回避原因、
        申诉及其中哪一条改变了裁定。
        """
        candidacy = self.store.candidacy(candidacy_id)
        work = self.store.work(candidacy.work_id)
        result = next(
            (r for r in self.store.results if r.candidacy_id == candidacy_id), None
        )

        source_round = self.store.rounds.get(candidacy.source_round_id)
        source_round_brief = {
            "round_id": source_round.round_id,
            "name": source_round.name,
            "sealed": source_round.sealed is not None,
        } if source_round else None
        # 汇总涉及该候选（同作品；个人候选再要求同人员）的全部已封存轮次，
        # 因此即便结果由现场终评产生，也能追到初评、分赛区的封存结论
        sealed_rounds: list[dict] = []
        for round_record in self.store.rounds.values():
            if round_record.sealed is None:
                continue
            hits = [
                a for a in round_record.advancements.values()
                if a.work_id == candidacy.work_id
                and (candidacy.person_id is None or a.person_id == candidacy.person_id)
            ]
            if not hits:
                continue
            sealed_rounds.append(
                {
                    "round_id": round_record.round_id,
                    "kind": round_record.kind.value,
                    "name": round_record.name,
                    "region": round_record.region,
                    "sealed_at": round_record.sealed.sealed_at,
                    "sealed_by": round_record.sealed.sealed_by,
                    "materials_fingerprint": round_record.sealed.materials_fingerprint,
                    "advancement_fingerprint": round_record.sealed.advancement_fingerprint,
                    "candidacy_ids": [a.candidacy_id for a in hits],
                }
            )

        person_trace = None
        if candidacy.person_id:
            person = self.store.person(candidacy.person_id)
            person_trace = {
                "person_id": person.person_id,
                "name": person.name,
                "age_proof": (
                    {
                        "proof_id": person.age_proof.proof_id,
                        "birth_date": person.age_proof.birth_date,
                        "doc_ref": person.age_proof.doc_ref,
                    }
                    if person.age_proof else None
                ),
                "memberships": [
                    {
                        "org_id": m.org_id, "org_name": m.org_name,
                        "from": m.started_from, "to": m.ended_at,
                    }
                    for m in person.memberships
                ],
                "appearances": [
                    {
                        "work_id": a.work_id, "version_id": a.version_id,
                        "relation": a.relation.value, "org_id": a.org_id,
                        "status": a.status.value, "detail": a.detail,
                    }
                    for a in person.appearances
                ],
                "regional_histories": [
                    {"region": h.region, "year": h.year, "advanced": h.advanced}
                    for h in person.regional_histories
                ],
                "prior_awards": [
                    {"award": p.award, "category": p.category.value, "year": p.year}
                    for p in person.prior_awards
                ],
            }

        recusals = []
        for recusal in self.store.recusals:
            if recusal.work_id == candidacy.work_id:
                judge = self.store.judges.get(recusal.judge_id)
                recusals.append(
                    {
                        "round_id": recusal.round_id,
                        "judge_id": recusal.judge_id,
                        "judge_name": judge.name if judge else recusal.judge_id,
                        "reason": recusal.reason.value,
                        "detail": recusal.detail,
                    }
                )

        scores = []
        for score in self.store.scores.values():
            if score.work_id != candidacy.work_id:
                continue
            scores.append(
                {
                    "score_id": score.score_id,
                    "round_id": score.round_id,
                    "version_id": score.version_id,
                    "judge_alias": score.judge_alias,
                    "value": score.value,
                    "state": (
                        "已被申诉排除" if score.excluded
                        else "证据锁定中" if score.locked_by_appeal
                        else "旧版本归档" if score.superseded
                        else "现行有效"
                    ),
                }
            )

        appeals = []
        for appeal in self.store.appeals.values():
            touches = (
                appeal.candidacy_id == candidacy_id
                or (appeal.work_id == candidacy.work_id)
                or any(
                    self.store.scores[s].work_id == candidacy.work_id
                    for s in appeal.score_ids
                )
            )
            if not touches:
                continue
            changed = (
                appeal.status == AppealStatus.UPHELD
                and candidacy_id in appeal.changed_candidacies
            )
            appeals.append(
                {
                    "appeal_id": appeal.appeal_id,
                    "target": appeal.target_type.value,
                    "status": appeal.status.value,
                    "reason": appeal.reason,
                    "resolution": appeal.resolution_note,
                    "changed_this_ruling": changed,
                    "changed_candidacies": appeal.changed_candidacies,
                }
            )

        live_events = []
        for event in self.store.live_events:
            if event.work_id != candidacy.work_id:
                continue
            live_events.append(
                {
                    "event_id": event.event_id,
                    "round_id": event.round_id,
                    "kind": event.kind.value,
                    "from_version": event.version_id,
                    "to_version": event.new_version_id,
                    "detail": event.detail,
                    "effects": event.effects,
                }
            )

        supplements = [
            {
                "evidence_id": eid,
                "fact_type": e.fact_type,
                "accepted": e.accepted,
                "review_note": e.review_note,
                "original_round_id": e.round_id,
                "review_round_id": e.review_round_id,
            }
            for eid, e in self.store.supplements.items()
            if e.candidacy_id == candidacy_id
        ]

        return {
            "result": (
                {
                    "category": result.category.value,
                    "status": result.status.value,
                    "confirmed_at": result.confirmed_at,
                    "rank": result.rank,
                    "invalidate_reason": result.invalidate_reason,
                }
                if result else None
            ),
            "candidacy": {
                "id": candidacy.candidacy_id,
                "category": candidacy.category.value,
                "source_round_id": candidacy.source_round_id,
                "status": candidacy.status.value,
                "effective_version_id": candidacy.effective_version_id or candidacy.version_id,
                "effects": candidacy.effects,
            },
            "work_lineage": {
                "work_id": work.work_id,
                "genre": work.genre,
                "current_title": work.current_title,
                "submitting_org_id": work.submitting_org_id,
                "duplicate_group_id": work.duplicate_group_id,
                "duplicate_links": work.duplicate_links,
                "author_ids": work.author_ids,
                "rights_holder_ids": work.rights_holder_ids,
                "versions": [
                    {
                        "version_id": v.version_id,
                        "title": v.title,
                        "text_hash": v.text_hash,
                        "note": v.note,
                        "first_performed_at": v.first_performed_at,
                        "originality_declared": v.originality_declared,
                        "originality_note": v.originality_note,
                        "recorded_at": v.recorded_at,
                    }
                    for v in work.versions
                ],
            },
            "person_qualification": person_trace,
            "qualification_snapshot": candidacy.qualification_snapshot,
            "supplementary_reviews": supplements,
            "source_round": source_round_brief,
            "sealed_rounds": sealed_rounds,
            "live_events": live_events,
            "scores": sorted(scores, key=lambda s: (s["round_id"], s["version_id"])),
            "recusals": recusals,
            "appeals": appeals,
        }
