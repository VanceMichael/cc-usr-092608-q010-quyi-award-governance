"""从 JSON 夹具重放曲艺评奖档案与裁定流程。

夹具以带时间戳的操作流水组织，加载器依次调用各服务；标注
``expect_error`` 的操作要求服务按规则拒绝。这样夹具本身就是一条
完整业务链路的可执行说明。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .appeals import AppealService
from .archive import ArchiveService
from .awards import AwardService, AwardWinner
from .eligibility import EligibilityService
from .finals import FinalsService
from .judging import JudgingService
from .records import (
    Advancement,
    AppealTarget,
    FinalsEventKind,
    OriginalityDeclaration,
    PastAward,
    PerformanceRole,
    PremiereFact,
    ReviewVerdict,
    RoundType,
    WorkKind,
    Downtime,
)
from .repository import AwardRepository
from .roles import GovernanceError, Role
from .rounds import RoundService
from .trace import build_trace

DOMAIN = "quyi-award-governance"

ROLES = {role.value: role for role in Role}
ROUNDS = {"初评": RoundType.PRELIMINARY, "分赛区": RoundType.REGIONAL}
APPEAL_TARGETS = {
    "资格": AppealTarget.QUALIFICATION,
    "文本": AppealTarget.TEXT,
    "评分": AppealTarget.SCORE,
}
FINAL_EVENTS = {
    "换角": FinalsEventKind.CAST_CHANGE,
    "中止": FinalsEventKind.SUSPENSION,
    "重新表演": FinalsEventKind.RE_PERFORMANCE,
}
PERFORMANCE_ROLES = {role.value: role for role in PerformanceRole}
VERDICTS = {
    "通过": ReviewVerdict.PASSED,
    "不通过": ReviewVerdict.REJECTED,
    "维持": ReviewVerdict.UPHELD,
}


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _role(spec: dict[str, Any], key: str = "actor") -> Role:
    return ROLES[spec[key]]


def load_archive(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("domain") != DOMAIN:
        raise ValueError("档案领域标识不一致")
    if not isinstance(raw.get("version"), int) or raw["version"] < 2:
        raise ValueError("档案版本无效")
    commands = raw.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError("档案操作流水为空")

    repo = AwardRepository()
    archive = ArchiveService(repo)
    rounds = RoundService(repo)
    eligibility = EligibilityService(repo)
    judging = JudgingService(repo)
    finals = FinalsService(repo, eligibility)
    awards = AwardService(repo, eligibility)
    appeals = AppealService(repo)

    traces: dict[str, dict] = {}
    rejections: list[str] = []

    services = {
        "archive": archive,
        "rounds": rounds,
        "eligibility": eligibility,
        "judging": judging,
        "finals": finals,
        "awards": awards,
        "appeals": appeals,
    }

    for index, command in enumerate(commands):
        op = command.get("op")
        try:
            _replay(op, command, services, traces, repo)
        except GovernanceError as exc:
            expected = command.get("expect_error")
            if expected is None:
                raise ValueError(f"第{index}步{op}被意外拒绝：{exc}") from exc
            if expected not in str(exc):
                raise ValueError(
                    f"第{index}步{op}拒绝原因不符：期望含{expected!r}，实际{exc!s}"
                ) from exc
            rejections.append(f"{op}: {exc}")
        else:
            if command.get("expect_error") is not None:
                raise ValueError(
                    f"第{index}步{op}本应被拒绝：{command['expect_error']}"
                )

    return {
        "domain": DOMAIN,
        "version": raw["version"],
        "repo": repo,
        "services": services,
        "traces": traces,
        "rejections": rejections,
    }


def _replay(op: str, c: dict[str, Any], services: dict, traces: dict, repo) -> None:
    archive: ArchiveService = services["archive"]
    rounds: RoundService = services["rounds"]
    eligibility: EligibilityService = services["eligibility"]
    judging: JudgingService = services["judging"]
    finals: FinalsService = services["finals"]
    awards: AwardService = services["awards"]
    appeal_service: AppealService = services["appeals"]

    if op == "register_organization":
        archive.register_organization(c["id"], c["name"])

    elif op == "register_person":
        person = archive.register_person(
            c["id"],
            c["name"],
            org_id=c.get("org_id"),
            valid_from=_d(c["valid_from"]) if c.get("valid_from") else None,
        )
        for award in c.get("past_awards", []):
            repo.add_past_award(
                c["id"],
                PastAward(
                    award_name=award["award_name"],
                    year=award["year"],
                    restricted_categories=tuple(award["restricted_categories"]),
                ),
            )
        for relation in c.get("declared_relations", []):
            person.declared_relations[relation["judge_id"]] = relation["relation"]

    elif op == "add_affiliation":
        archive.add_affiliation(
            c["person_id"], c["org_id"], _d(c["valid_from"]),
            _d(c["valid_to"]) if c.get("valid_to") else None,
        )

    elif op == "add_age_evidence":
        archive.add_age_evidence(
            c["person_id"],
            c["evidence_id"],
            _d(c["birth_date"]),
            _d(c["verified_at"]),
            c.get("note", ""),
        )

    elif op == "add_regional_experience":
        archive.add_regional_experience(
            c["person_id"], c["version_id"], c["region_id"], c["result"]
        )

    elif op == "register_work":
        originality = None
        if c.get("originality"):
            spec = c["originality"]
            originality = OriginalityDeclaration(
                declared_by=spec["declared_by"],
                declared_at=_d(spec["declared_at"]),
                evidence_id=spec["evidence_id"],
                claim=spec["claim"],
            )
        premiere = None
        if c.get("premiere"):
            spec = c["premiere"]
            premiere = PremiereFact(
                premiered_at=_d(spec["premiered_at"]),
                venue=spec["venue"],
                evidence_id=spec["evidence_id"],
            )
        kind = WorkKind(c["kind"])
        archive.register_work(
            c["id"],
            c["genre"],
            c["title"],
            kind,
            tuple(c["authors"]),
            tuple(c["rights_holders"]),
            originality=originality,
            premiere=premiere,
            lineage=[tuple(item) for item in c.get("lineage", [])],
        )

    elif op == "submit_version":
        archive.submit_version(
            c["id"],
            c["work_id"],
            c["title"],
            c["text_fingerprint"],
            c["submitting_org_id"],
            _d(c["submitted_at"]),
            parent_version_id=c.get("parent_version_id"),
            note=c.get("note", ""),
            actor_role=_role(c),
        )

    elif op == "add_participation":
        archive.add_participation(
            c["person_id"],
            c["version_id"],
            PERFORMANCE_ROLES[c["role"]],
            c["org_id"],
        )

    elif op == "flag_clue":
        archive.flag_clue(c["id"], c["version_a"], c["version_b"], c["reason"])

    elif op == "decide_clue":
        archive.decide_clue(
            c["id"],
            _role(c),
            c["confirmed"],
            c["decided_by"],
            _dt(c["decided_at"]),
            c.get("note", ""),
        )

    elif op == "seal_round":
        advancements = [
            Advancement(
                version_id=item[0],
                category=item[1],
                person_id=item[2],
                advanced=item[3],
            )
            for item in c["advancements"]
        ]
        rounds.seal_round(
            c["id"],
            ROUNDS[c["round_type"]],
            c["round_name"],
            c["sealed_by"],
            _dt(c["sealed_at"]),
            _role(c),
            dict(c["material_fingerprints"]),
            advancements,
        )

    elif op == "open_review_case":
        eligibility.open_review_case(
            c["id"],
            c["version_id"],
            tuple(c["categories"]),
            c["opened_by"],
            _dt(c["opened_at"]),
            person_id=c.get("person_id"),
        )

    elif op == "supply_evidence":
        eligibility.supply_evidence(
            c["case_id"], c["evidence_id"], _d(c["received_at"]), c["content"]
        )

    elif op == "decide_review_case":
        eligibility.decide_review_case(
            c["case_id"],
            VERDICTS[c["verdict"]],
            c["decided_by"],
            _dt(c["decided_at"]),
            _role(c),
            c["effect"],
            c.get("rationale", ""),
        )

    elif op == "attach_age_evidence_via_case":
        eligibility.attach_age_evidence_via_case(
            c["case_id"],
            c["evidence_id"],
            _d(c["birth_date"]),
            _d(c["verified_at"]),
            _role(c),
            c.get("note", ""),
        )

    elif op == "register_judge":
        judging.register_judge(
            c["id"], c["name"], org_id=c.get("org_id"),
            worked_version_ids=set(c.get("worked_version_ids", [])),
        )

    elif op == "assign":
        judging.assign(
            c["judge_id"],
            [tuple(item) for item in c["candidates"]],
            _d(c["on_date"]),
        )

    elif op == "issue_blind_code":
        judging.issue_blind_code(
            c["code"],
            c["version_id"],
            c["category"],
            c.get("person_id"),
            _role(c),
        )

    elif op == "submit_score":
        judging.submit_score(
            c["code"],
            c["judge_id"],
            c["value"],
            c["basis"],
            _dt(c["submitted_at"]),
        )

    elif op == "reveal_identity":
        judging.reveal_identity(
            c["code"], _role(c), c["revealed_by"], _dt(c["revealed_at"])
        )

    elif op == "identity_of":
        version_id, person_id, category = judging.identity_of(
            c["code"], _role(c)
        )
        if c.get("expect_version") and version_id != c["expect_version"]:
            raise ValueError(
                f"身份揭示版本不符：期望{c['expect_version']}，实际{version_id}"
            )

    elif op == "admit_candidate":
        finals.admit_candidate(
            c["version_id"],
            c["category"],
            c.get("person_id"),
            _role(c),
            _dt(c["admitted_at"]),
        )

    elif op == "assert_qualification":
        result = eligibility.qualification(
            c["version_id"],
            c["category"],
            _d(c["deadline"]),
            c.get("person_id"),
        )
        if result["status"] != c["expect_status"]:
            raise ValueError(
                f"{c['category']}资格结论不符：期望{c['expect_status']}，"
                f"实际{result['status']}（{result['reasons']}）"
            )

    elif op == "assert_standing":
        standing = finals.standing_view(
            c["version_id"], c["category"], c.get("person_id")
        )
        expectations = {
            "eligible": standing.eligible,
            "in_scoring": standing.in_scoring,
            "scoring_attempt": standing.scoring_attempt,
            "suspension_pending": standing.suspension_pending,
        }
        for key, expected in c["expect"].items():
            if expectations[key] != expected:
                raise ValueError(
                    f"候选状态{key}不符：期望{expected}，实际{expectations[key]}"
                )

    elif op == "finals_event":
        finals.record_event(
            c["id"],
            c["session_id"],
            c["version_id"],
            FINAL_EVENTS[c["kind"]],
            _dt(c["occurred_at"]),
            c["attempt"],
            c["detail"],
            _role(c),
            person_out=c.get("person_out"),
            person_in=c.get("person_in"),
            newcomer_deadline=_d(c["newcomer_deadline"])
            if c.get("newcomer_deadline")
            else None,
        )

    elif op == "open_notice":
        appeal_service.open_notice(
            c["id"], _dt(c["started_at"]), _dt(c["original_end"]), _role(c)
        )

    elif op == "register_downtime":
        seconds = appeal_service.register_downtime(
            c["notice_id"],
            Downtime(start=_dt(c["start"]), end=_dt(c["end"])),
        )
        if "expect_overlap_seconds" in c:
            expected = float(c["expect_overlap_seconds"])
            if abs(seconds - expected) > 1e-6:
                raise ValueError(
                    f"停机顺延秒数不符：期望{expected}，实际{seconds}"
                )

    elif op == "quota_check":
        winners = [
            AwardWinner(category=item[1], version_id=item[0], person_id=item[2])
            for item in c["winners"]
        ]
        result = awards.check_before_publication(
            winners, _d(c["newcomer_deadline"]), _role(c), _dt(c["checked_at"])
        )
        if c.get("expect_pass") and not all(check.passed for check in result.values()):
            violations = [
                v
                for check in result.values()
                for v in check.violations
            ]
            raise ValueError(f"名额兼得校验意外失败：{violations}")
        if "expect_class_fail" in c:
            from .records import AwardCategoryClass

            class_name = c["expect_class_fail"]
            target_class = {
                "作品类": AwardCategoryClass.WORK,
                "个人类": AwardCategoryClass.PERSONAL,
            }[class_name]
            check = result[target_class]
            if check.passed:
                raise ValueError(f"{class_name}校验意外通过")
            keyword = c.get("expect_violation_contains")
            if keyword and not any(keyword in v for v in check.violations):
                raise ValueError(
                    f"{class_name}违规项不含{keyword!r}：{list(check.violations)}"
                )

    elif op == "file_appeal":
        appeal_service.file_appeal(
            c["id"],
            APPEAL_TARGETS[c["target"]],
            c["target_ref"],
            c["version_id"],
            tuple(c["categories"]),
            tuple(c["locked_evidence"]),
            _dt(c["filed_at"]),
            _role(c),
        )

    elif op == "decide_appeal":
        appeal_service.decide_appeal(
            c["id"],
            c["upheld"],
            c["decided_by"],
            _dt(c["decided_at"]),
            _role(c),
            c["decision"],
            c.get("change_summary"),
        )

    elif op == "confirm_winners":
        winners = [
            AwardWinner(category=item[1], version_id=item[0], person_id=item[2])
            for item in c["winners"]
        ]
        awards.confirm_winners(
            winners, c["notice_id"], _dt(c["confirmed_at"]), _role(c)
        )

    elif op == "trace":
        traces[c["name"]] = build_trace(
            repo, c["category"], c["version_id"], c.get("person_id")
        )

    else:
        raise ValueError(f"未知操作：{op}")
