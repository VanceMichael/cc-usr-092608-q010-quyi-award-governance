"""获奖结果全链追溯。

从一项获奖结果汇聚作品沿革、人员资格、轮次封存、评委回避、匿名评分
与揭示、现场事件、资格复核与申诉裁定。追溯只读取既有记录，不做推断；
缺失环节显式标为缺口。
"""

from __future__ import annotations

from .repository import AwardRepository


def build_trace(
    repo: AwardRepository, category: str, version_id: str, person_id: str | None = None
) -> dict:
    version = repo.versions.get(version_id)
    gaps: list[str] = []
    if version is None:
        return {"found": False, "gaps": [f"版本{version_id}不存在"]}

    work = repo.works.get(version.work_id)

    # 1) 作品沿革
    lineage_section = _work_lineage(repo, version_id, gaps)

    # 2) 人员资格
    persons_section = _persons(repo, version_id, category, person_id, gaps)

    # 3) 轮次封存与晋级
    seals_section = _seals(repo, version_id, category, gaps)

    # 4) 评委回避
    conflicts_section = _conflicts(repo, version_id, category)

    # 5) 评分与现场事件
    scoring_section = _scoring(repo, version_id, category, person_id, gaps)
    finals_section = _finals(repo, version_id, category, person_id)

    # 6) 资格复核与申诉
    review_section = _reviews(repo, version_id, category, person_id)
    appeals_section = _appeals(repo, version_id, category)

    return {
        "found": True,
        "award": {
            "category": category,
            "version_id": version_id,
            "person_id": person_id,
        },
        "work_lineage": lineage_section,
        "person_qualification": persons_section,
        "round_seals": seals_section,
        "judge_recusals": conflicts_section,
        "scoring": scoring_section,
        "finals_events": finals_section,
        "review_cases": review_section,
        "appeals": appeals_section,
        "gaps": gaps,
    }


def _work_lineage(repo: AwardRepository, version_id: str, gaps: list[str]) -> dict:
    version = repo.versions[version_id]
    work = repo.works[version.work_id]
    versions = repo.versions_of_work(work.id)
    if len(versions) == 1:
        gaps.append("作品线仅一个版本，无改名再报送记录")
    if work.originality is None:
        gaps.append(f"作品{work.id}缺少原创声明")
    if work.premiere is None:
        gaps.append(f"作品{work.id}缺少首演事实")
    clues = [
        clue for vid in work.version_ids for clue in repo.clues_for_version(vid)
    ]
    return {
        "work_id": work.id,
        "genre": work.genre,
        "title": work.title,
        "kind": work.kind.value,
        "authors": list(work.authors),
        "rights_holders": list(work.rights_holders),
        "originality": work.originality,
        "premiere": work.premiere,
        "lineage_refs": list(work.lineage),
        "versions": [
            {
                "id": v.id,
                "title": v.title,
                "submitting_org_id": v.submitting_org_id,
                "submitted_at": v.submitted_at.isoformat(),
                "parent_version_id": v.parent_version_id,
            }
            for v in versions
        ],
        "duplicate_clues": [
            {
                "id": clue.id,
                "other_version": (
                    clue.version_id_b
                    if clue.version_id_a == version_id
                    else clue.version_id_a
                ),
                "reason": clue.reason,
                "status": clue.status.value,
                "decided_by": clue.decided_by,
                "decided_at": clue.decided_at.isoformat() if clue.decided_at else None,
                "note": clue.decision_note,
            }
            for clue in clues
        ],
    }


def _persons(repo, version_id, category, person_id, gaps) -> dict:
    participations = repo.participations_for(version_id)
    people = {}
    for item in participations:
        person = repo.persons.get(item.person_id)
        if person is None:
            gaps.append(f"演职人员{item.person_id}缺少档案")
            continue
        people[item.person_id] = {
            "name": person.name,
            "role": item.role.value,
            "org_id": item.org_id,
            "affiliations": [
                {
                    "org_id": aff.org_id,
                    "valid_from": aff.valid_from.isoformat(),
                    "valid_to": aff.valid_to.isoformat() if aff.valid_to else None,
                }
                for aff in person.affiliations
            ],
            "age_evidence": (
                {
                    "evidence_id": person.age_evidence.evidence_id,
                    "birth_date": person.age_evidence.birth_date.isoformat(),
                    "verified_at": person.age_evidence.verified_at.isoformat(),
                }
                if person.age_evidence
                else None
            ),
            "past_awards": [
                {
                    "award_name": award.award_name,
                    "year": award.year,
                    "restricted_categories": list(award.restricted_categories),
                }
                for award in person.past_awards
            ],
            "regional_experiences": [
                {
                    "region_id": r.region_id,
                    "version_id": r.version_id,
                    "result": r.result,
                }
                for r in repo.regionals
                if r.person_id == person.id
            ],
        }
    if category in ("表演奖", "新人奖"):
        if person_id is None:
            gaps.append("个人类奖项缺少候选人")
        elif person_id not in people:
            gaps.append(f"候选人{person_id}不在该版本演职名单中")
        elif category == "新人奖" and people[person_id]["age_evidence"] is None:
            gaps.append(f"新人奖候选人{person_id}缺少年龄证明")
    return {"candidate_id": person_id, "people": people}


def _seals(repo, version_id, category, gaps) -> list[dict]:
    rows = []
    for seal in repo.seals.values():
        fingerprint = seal.material_fingerprints.get(version_id)
        advancement = next(
            (
                a
                for a in seal.advancements
                if a.version_id == version_id and a.category == category
            ),
            None,
        )
        if fingerprint is None and advancement is None:
            continue
        rows.append(
            {
                "seal_id": seal.id,
                "round": f"{seal.round_type.value}·{seal.round_name}",
                "sealed_at": seal.sealed_at.isoformat(),
                "sealed_by": seal.sealed_by,
                "material_fingerprint": fingerprint,
                "advancement": (
                    {
                        "advanced": advancement.advanced,
                        "person_id": advancement.person_id,
                    }
                    if advancement
                    else None
                ),
                "immutable": True,
            }
        )
    if not rows:
        gaps.append(f"版本{version_id}在{category}上没有任何封存记录")
    return rows


def _conflicts(repo, version_id, category) -> list[dict]:
    rows = []
    for conflict in repo.conflicts:
        if (
            conflict.candidate_version_id == version_id
            and conflict.category == category
        ):
            judge = repo.judges.get(conflict.judge_id)
            rows.append(
                {
                    "judge_id": conflict.judge_id,
                    "judge_name": judge.name if judge else None,
                    "conflict_type": conflict.conflict_type.value,
                    "basis": conflict.basis,
                }
            )
    return rows


def _scoring(repo, version_id, category, person_id, gaps) -> dict:
    codes = [
        code
        for code, mapping in repo.blind_mappings.items()
        if mapping.version_id == version_id and mapping.category == category
        and mapping.person_id == person_id
    ]
    scores = []
    revealed_any = False
    for code in codes:
        mapping = repo.blind_mappings[code]
        revealed_any = revealed_any or mapping.revealed
        for score in repo.scores:
            if score.code != code:
                continue
            scores.append(
                {
                    "blind_code": code,
                    "judge_id": score.judge_id if mapping.revealed else "（匿名）",
                    "value": score.value,
                    "basis": score.basis if mapping.revealed else "（封闭中不可见）",
                }
            )
    if not codes:
        gaps.append("该候选没有匿名评分记录")
    elif not revealed_any:
        gaps.append("匿名映射尚未揭示，评分身份不可追溯")
    return {"blind_codes": codes, "revealed": revealed_any, "scores": scores}


def _finals(repo, version_id, category, person_id) -> dict:
    events = [
        {
            "id": event.id,
            "session_id": event.session_id,
            "kind": event.kind.value,
            "attempt": event.attempt,
            "scored": event.scored,
            "detail": event.detail,
            "person_out": event.person_out,
            "person_in": event.person_in,
            "occurred_at": event.occurred_at.isoformat(),
        }
        for event in sorted(
            repo.finals_events, key=lambda e: (e.occurred_at, e.attempt)
        )
        if event.version_id == version_id
    ]
    standing = repo.standings.get((version_id, category, person_id))
    return {
        "events": events,
        "standing": (
            {
                "eligible": standing.eligible,
                "in_scoring": standing.in_scoring,
                "scoring_attempt": standing.scoring_attempt,
                "note": standing.note,
                "history": list(standing.history),
            }
            if standing
            else None
        ),
    }


def _reviews(repo, version_id, category, person_id) -> list[dict]:
    rows = []
    for case in repo.review_cases.values():
        if case.target_version_id != version_id or category not in case.categories:
            continue
        if person_id is not None and case.person_id not in (None, person_id):
            continue
        rows.append(
            {
                "case_id": case.id,
                "verdict": case.verdict.value,
                "person_id": case.person_id,
                "evidence": [e.evidence_id for e in case.evidence],
                "seal_refs": list(case.seal_refs),
                "decided_by": case.decided_by,
                "decided_at": case.decided_at.isoformat() if case.decided_at else None,
                "effect": case.effect,
                "original_round_unchanged": True,
            }
        )
    return rows


def _appeals(repo, version_id, category) -> list[dict]:
    rows = []
    for appeal in repo.appeals.values():
        if appeal.version_id != version_id or category not in appeal.categories:
            continue
        rows.append(
            {
                "appeal_id": appeal.id,
                "target": appeal.target.value,
                "target_ref": appeal.target_ref,
                "status": appeal.status.value,
                "locked_evidence": list(appeal.locked_evidence),
                "decided_by": appeal.decided_by,
                "decided_at": appeal.decided_at.isoformat() if appeal.decided_at else None,
                "decision": appeal.decision,
                "change_summary": appeal.change_summary,
            }
        )
    return rows
