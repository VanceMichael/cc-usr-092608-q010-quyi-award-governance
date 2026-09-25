"""曲艺评奖档案与裁定服务的规则测试。"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import date, datetime
from pathlib import Path

from src.governance import (
    AppealService,
    ArchiveService,
    AwardService,
    EligibilityService,
    FinalsService,
    JudgingService,
    AwardRepository,
    RoundService,
    build_trace,
)
from src.governance.archive_loader import load_archive
from src.governance.records import (
    Advancement,
    AppealTarget,
    AwardWinner,
    Downtime,
    FinalsEventKind,
    OriginalityDeclaration,
    PerformanceRole,
    PremiereFact,
    RoundType,
    WorkKind,
)
from src.governance.roles import (
    ConflictOfInterest,
    GovernanceError,
    PermissionDenied,
    Role,
)

FIXTURE = Path("fixtures/award-archive.json")
DEADLINE = date(2026, 1, 10)
T = datetime.fromisoformat


class FixtureReplayTest(unittest.TestCase):
    """合成夹具重放一条完整链路，内含全部预期拒绝与断言。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_archive(FIXTURE)
        cls.repo = cls.bundle["repo"]

    def test_fixture_replays_without_unexpected_rejections(self) -> None:
        # 能完整加载即说明夹具中全部 expect_error/expect_* 断言通过。
        self.assertGreaterEqual(len(self.bundle["rejections"]), 6)

    def test_four_hundred_scale_is_just_more_versions(self) -> None:
        # 服务对作品数量不设上限，档案按作品线组织。
        self.assertGreaterEqual(len(self.repo.works), 5)
        self.assertGreaterEqual(len(self.repo.versions), 6)

    def test_renamed_resubmission_stays_on_one_work_line(self) -> None:
        work = self.repo.works["W-001"]
        self.assertEqual([v.id for v in self.repo.versions_of_work("W-001")],
                         ["V-001", "V-002"])
        titles = {v.title for v in self.repo.versions_of_work("W-001")}
        self.assertEqual(titles, {"古城春晓", "古城新春晓"})

    def test_duplicate_clues_need_authorized_decision(self) -> None:
        rejected = self.repo.duplicate_clues["dup-V-001-V-009"]
        confirmed = self.repo.duplicate_clues["dup-V-001-V-010"]
        self.assertEqual(rejected.status.value, "不构成关联")
        self.assertEqual(rejected.decided_by, "SUP-01")
        self.assertEqual(confirmed.status.value, "已确认关联")
        # 确认关联只建立沿革引用，两条作品线仍然独立存在。
        self.assertIn("W-010", [ref for ref, _ in self.repo.works["W-001"].lineage])
        self.assertIn("W-001", [ref for ref, _ in self.repo.works["W-010"].lineage])
        self.assertNotEqual(
            self.repo.versions["V-001"].work_id,
            self.repo.versions["V-010"].work_id,
        )

    def test_seals_are_immutable_snapshots(self) -> None:
        seal = self.repo.seals["SEAL-PRE"]
        # 华南赛区对 P-006 的封存结论为未晋级，复核通过后也不被改写。
        south = self.repo.seals["SEAL-SOUTH"]
        advancement = next(
            a for a in south.advancements
            if a.version_id == "V-003" and a.category == "新人奖"
        )
        self.assertFalse(advancement.advanced)
        self.assertIn("V-003", seal.material_fingerprints)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            seal.sealed_by = "OTHER"  # type: ignore[misc]

    def test_late_age_evidence_went_through_review_only(self) -> None:
        person = self.repo.persons["P-006"]
        self.assertEqual(person.age_evidence.evidence_id, "E-age-006")
        self.assertIn("REV-006", person.age_evidence.note)
        case = self.repo.review_cases["REV-006"]
        self.assertEqual(case.verdict.value, "复核通过")
        self.assertEqual(case.seal_refs, ["SEAL-PRE", "SEAL-SOUTH"])

    def test_judge_conflicts_cover_three_relation_types(self) -> None:
        pairs = {(c.candidate_version_id, c.conflict_type.value)
                 for c in self.repo.conflicts if c.judge_id == "J-001"}
        self.assertIn(("V-001", "单位关系"), pairs)
        self.assertIn(("V-004", "作品关系"), pairs)
        self.assertIn(("V-003", "人员关系"), pairs)

    def test_recused_judge_score_rejected_and_secretariat_blind(self) -> None:
        # J-001 对 B01 的评分未被接收；只有 J-002/J-003 两份。
        judges = {s.judge_id for s in self.repo.scores if s.code == "B01"}
        self.assertEqual(judges, {"J-002", "J-003"})
        mapping = self.repo.blind_mappings["B01"]
        self.assertTrue(mapping.revealed)
        self.assertEqual(mapping.revealed_by, "SUP-01")

    def test_finals_cast_change_and_reperformance(self) -> None:
        out = self.repo.standings[("V-001", "表演奖", "P-002")]
        inn = self.repo.standings[("V-001", "表演奖", "P-004")]
        self.assertFalse(out.eligible)
        self.assertTrue(inn.eligible)
        newcomer = self.repo.standings[("V-003", "新人奖", "P-006")]
        self.assertEqual(newcomer.scoring_attempt, 2)
        self.assertTrue(newcomer.in_scoring)
        # 原中止尝试作为现场事件仍可查看。
        kinds = [e.kind for e in self.repo.finals_events if e.version_id == "V-003"]
        self.assertEqual(kinds, [FinalsEventKind.SUSPENSION, FinalsEventKind.RE_PERFORMANCE])

    def test_notice_deadline_extended_exactly_by_overlap(self) -> None:
        notice = self.repo.notices["NOTICE-2026"]
        self.assertEqual(notice.original_end, T("2026-06-08T09:00:00"))
        self.assertEqual(notice.adjusted_end, T("2026-06-09T09:00:00"))
        seconds = [s for _, s in notice.extensions]
        self.assertEqual(seconds, [86400.0])

    def test_appeal_lock_scoped_and_unrelated_awards_confirmed(self) -> None:
        # AP-001 在 11:00 前受理中，仅锁定 V-003 新人奖。
        service = self.bundle["services"]["awards"]
        first_batch, second_batch = service.confirmation_log
        first_keys = {(w.version_id, w.category) for w in first_batch[1]}
        second_keys = {(w.version_id, w.category) for w in second_batch[1]}
        # 第一批（申诉受理期间）：无关奖项照常确认，被质疑的新人奖缺席。
        self.assertIn(("V-001", "节目奖"), first_keys)
        self.assertIn(("V-004", "文学奖"), first_keys)
        self.assertIn(("V-001", "表演奖"), first_keys)
        self.assertNotIn(("V-003", "新人奖"), first_keys)
        # 申诉驳回后第二批单独确认新人奖。
        self.assertEqual(second_keys, {("V-003", "新人奖")})
        # 最终全部确认。
        confirmed_keys = {(w.version_id, w.category) for w in service.confirmed_winners}
        self.assertEqual(
            confirmed_keys,
            {
                ("V-001", "节目奖"),
                ("V-004", "文学奖"),
                ("V-001", "表演奖"),
                ("V-003", "新人奖"),
            },
        )

    def test_traces_assemble_full_chain(self) -> None:
        program = self.bundle["traces"]["program"]
        newcomer = self.bundle["traces"]["newcomer"]
        self.assertTrue(program["found"])
        # 作品沿革：改名版本 + 经确认的重复关联。
        self.assertEqual(len(program["work_lineage"]["versions"]), 2)
        statuses = {c["status"] for c in program["work_lineage"]["duplicate_clues"]}
        self.assertEqual(statuses, {"已确认关联", "不构成关联"})
        # 回避可追到评委与原因。
        recusals = program["judge_recusals"]
        self.assertTrue(any(r["judge_id"] == "J-001" for r in recusals))
        self.assertTrue(all(r["basis"] for r in recusals))
        # 申诉改变了哪项裁定。
        appeals = program["appeals"]
        upheld = next(a for a in appeals if a["status"] == "申诉成立")
        self.assertIn("B01", upheld["change_summary"])
        # 新人奖追溯：复核案件 + 两轮封存 + 两次尝试 + 申诉驳回。
        self.assertTrue(newcomer["review_cases"])
        self.assertEqual(len(newcomer["round_seals"]), 2)
        self.assertEqual(
            [e["kind"] for e in newcomer["finals_events"]["events"]],
            ["中止", "重新表演"],
        )
        self.assertEqual(newcomer["appeals"][0]["status"], "申诉驳回")


class FreshServiceTest(unittest.TestCase):
    """不依赖夹具的针对性规则测试。"""

    def setUp(self) -> None:
        self.repo = AwardRepository()
        self.archive = ArchiveService(self.repo)
        self.rounds = RoundService(self.repo)
        self.eligibility = EligibilityService(self.repo)
        self.judging = JudgingService(self.repo)
        self.finals = FinalsService(self.repo, self.eligibility)
        self.awards = AwardService(self.repo, self.eligibility)
        self.appeals = AppealService(self.repo)
        self._seed()

    def _seed(self) -> None:
        self.archive.register_organization("O1", "合成院团一")
        self.archive.register_organization("O2", "合成院团二")
        self.archive.register_person("A1", "合成作者", org_id="O1",
                                     valid_from=date(2020, 1, 1))
        self.archive.register_person("A2", "合成青年演员", org_id="O1",
                                     valid_from=date(2020, 1, 1))
        self.archive.register_person("A3", "合成超龄演员", org_id="O2",
                                     valid_from=date(2020, 1, 1))
        self.archive.register_work(
            "W1", "评书", "合成书目", WorkKind.ORIGINAL, ("A1",), ("A1",),
            originality=OriginalityDeclaration(
                "A1", date(2025, 12, 1), "EO1", "原创"),
            premiere=PremiereFact(date(2025, 11, 1), "合成剧场", "EP1"),
        )
        self.archive.submit_version(
            "V1", "W1", "合成书目", "fp1", "O1", date(2026, 1, 5),
            actor_role=Role.SUBMITTING_ORG,
        )
        self.archive.add_participation("A2", "V1", PerformanceRole.PERFORMER, "O1")
        self.archive.add_participation("A3", "V1", PerformanceRole.PERFORMER, "O2")
        self.archive.add_age_evidence(
            "A2", "EA2", date(1995, 6, 1), date(2026, 1, 8))
        self.archive.add_age_evidence(
            "A3", "EA3", date(1975, 6, 1), date(2026, 1, 8))

    def test_similar_name_never_creates_merge_or_blocks_flow(self) -> None:
        # 另一个院团以高度近似名称报送：只生成疑似线索，档案不合并、流程不受阻。
        self.archive.register_work(
            "W2", "评书", "合成书目录", WorkKind.ORIGINAL, ("A1",), ("A1",),
            originality=OriginalityDeclaration(
                "A1", date(2025, 12, 2), "EO2", "原创"),
        )
        self.archive.submit_version(
            "V2", "W2", "合成书目录", "fp2", "O2", date(2026, 1, 6),
            actor_role=Role.SUBMITTING_ORG,
        )
        clues = self.repo.clues_for_version("V2")
        self.assertEqual(len(clues), 1)
        self.assertEqual(clues[0].status.value, "疑似")
        # 未经授权确认：两作品线独立、无沿革引用。
        self.assertFalse(self.repo.works["W1"].lineage)
        # 秘书处无权确认。
        with self.assertRaises(PermissionDenied):
            self.archive.decide_clue(
                clues[0].id, Role.SECRETARIAT, True, "X", T("2026-01-10T00:00:00"))

    def test_secretariat_cannot_reveal_identity_but_sees_completeness(self) -> None:
        self.judging.register_judge("J9", "合成评委")
        self.judging.issue_blind_code(
            "B9", "V1", "节目奖", None, Role.SECRETARIAT)
        self.judging.submit_score("B9", "J9", 88.0, "合成评语",
                                  T("2026-05-01T10:00:00"))
        report = self.judging.completeness_report(Role.SECRETARIAT)
        self.assertEqual(report, {"B9": 1})
        with self.assertRaises(PermissionDenied):
            self.judging.identity_of("B9", Role.SECRETARIAT)
        # 监审在揭示前同样不能查询未揭示映射。
        with self.assertRaises(GovernanceError):
            self.judging.identity_of("B9", Role.SUPERVISOR)

    def test_recused_score_raises_conflict(self) -> None:
        self.judging.register_judge("J8", "本单位评委", org_id="O1")
        conflicts = self.judging.assign(
            "J8", [("V1", "节目奖", None)], date(2026, 5, 1))
        self.assertEqual(len(conflicts), 1)
        self.judging.issue_blind_code(
            "B8", "V1", "节目奖", None, Role.SECRETARIAT)
        with self.assertRaises(ConflictOfInterest):
            self.judging.submit_score("B8", "J8", 90.0, "x",
                                      T("2026-05-01T10:00:00"))

    def test_newcomer_age_and_history_restrictions(self) -> None:
        ok = self.eligibility.qualification("V1", "新人奖", DEADLINE, "A2")
        old = self.eligibility.qualification("V1", "新人奖", DEADLINE, "A3")
        self.assertEqual(ok["status"], "通过")
        self.assertEqual(old["status"], "不通过")
        self.assertTrue(any("年龄限制" in r for r in old["reasons"]))

    def test_seal_then_supplement_does_not_rewrite_round(self) -> None:
        self.rounds.seal_round(
            "S1", RoundType.PRELIMINARY, "初评", "OC", T("2026-03-01T18:00:00"),
            Role.ORGANIZING_COMMITTEE,
            {"V1": "fp1"},
            [Advancement("V1", "新人奖", "A2", False)],
        )
        # 封存后年龄证明不能直接补。
        with self.assertRaises(GovernanceError):
            self.archive.add_age_evidence(
                "A2", "EA2-NEW", date(1995, 6, 1), date(2026, 3, 5))
        case = self.eligibility.open_review_case(
            "R1", "V1", ("新人奖",), "SEC", T("2026-03-03T09:00:00"),
            person_id="A2")
        self.eligibility.supply_evidence(
            "R1", "EX1", date(2026, 3, 4), "合成后补材料")
        # 封存结论保持"未晋级"，复核结论独立存在。
        self.assertFalse(self.repo.is_version_advanced("V1", "新人奖"))
        self.assertEqual(case.seal_refs, ["S1"])

    def test_quota_exceeded_blocks_publication(self) -> None:
        from src.governance.records import AwardCategoryClass

        service_awards = AwardService(
            self.repo,
            self.eligibility,
            quotas={"节目奖": 1, "文学奖": 1, "表演奖": 1, "新人奖": 1},
        )
        # 同一节目奖两名获奖（不同实例）超过 1 名配额。
        winners = [
            AwardWinner("节目奖", "V1", None),
            AwardWinner("节目奖", "V1", None),
        ]
        checks = service_awards.check_before_publication(
            winners, DEADLINE, Role.ORGANIZING_COMMITTEE, T("2026-05-20T09:30:00")
        )
        self.assertFalse(checks[AwardCategoryClass.WORK].passed)
        self.assertTrue(
            any("名额" in v for v in checks[AwardCategoryClass.WORK].violations)
        )

    def test_downtime_partial_overlap_and_outside_window(self) -> None:
        self.appeals.open_notice(
            "N1", T("2026-06-01T09:00:00"), T("2026-06-08T09:00:00"),
            Role.ORGANIZING_COMMITTEE)
        # 停机早于公示开始：不顺延。
        zero = self.appeals.register_downtime(
            "N1", Downtime(T("2026-05-31T00:00:00"), T("2026-06-01T08:00:00")))
        self.assertEqual(zero, 0.0)
        # 停机跨越公示开始，仅重叠 12 小时。
        half = self.appeals.register_downtime(
            "N1", Downtime(T("2026-06-01T00:00:00"), T("2026-06-01T21:00:00")))
        self.assertEqual(half, 12 * 3600.0)
        self.assertEqual(self.repo.notices["N1"].adjusted_end,
                         T("2026-06-08T21:00:00"))

    def test_appeal_locks_only_targeted_evidence(self) -> None:
        self.appeals.open_notice(
            "N2", T("2026-06-01T09:00:00"), T("2026-06-08T09:00:00"),
            Role.ORGANIZING_COMMITTEE)
        self.appeals.file_appeal(
            "AP", AppealTarget.QUALIFICATION, "EA3", "V1", ("新人奖",),
            ("EA3",), T("2026-06-02T10:00:00"), Role.PARTICIPANT)
        self.assertTrue(self.repo.is_evidence_locked("EA3"))
        self.assertFalse(self.repo.is_evidence_locked("EA2"))
        self.assertTrue(self.repo.is_category_blocked("V1", "新人奖"))
        self.assertFalse(self.repo.is_category_blocked("V1", "节目奖"))


if __name__ == "__main__":
    unittest.main()
