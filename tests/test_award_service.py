"""奖项服务测试：资格、名额、兼得、确认与溯源。"""

from __future__ import annotations

import unittest
from datetime import date

from src.quyi.errors import (
    AppealLockError,
    AuthorizationError,
    QualificationError,
    QuotaError,
)
from src.quyi.models import (
    AppealTarget,
    AwardCategory,
    QuotaRule,
    ResultStatus,
    RoundKind,
)

from .domain_support import (
    OFFICER,
    ORG,
    REFERENCE_DATE,
    SEC,
    build_services,
    create_sealed_round,
    final_candidacies,
    register_judge,
)


class AwardFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        create_sealed_round(
            self.svc, "R-REG", RoundKind.REGIONAL, "华东赛区", region="华东",
            candidacy_specs=[
                ("C-R1", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True),
                ("C-R2", AwardCategory.NEWCOMER, "W-001", "W-001-V1", "P-003", True),
                ("C-R3", AwardCategory.PERFORMANCE, "W-001", "W-001-V1", "P-002", True),
            ],
        )
        final_candidacies(self.svc)


class QualificationValidationTest(AwardFixture):
    def test_newcomer_age_limit(self) -> None:
        # P-003 2002 年生，2026 年 24 岁，合规
        self.assertEqual(
            self.svc.awards.validate_qualification("C-FIN-NEW", REFERENCE_DATE), []
        )
        future = date(2050, 9, 25)  # 48 岁
        problems = self.svc.awards.validate_qualification("C-FIN-NEW", future)
        self.assertTrue(any("年龄限制" in p for p in problems))

    def test_missing_age_proof_blocks_newcomer(self) -> None:
        self.svc.archive.register_person(SEC, "P-200", "无年龄证明演员")
        self.svc.archive.add_membership(SEC, "P-200", "org-a", date(2020, 1, 1))
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-NOPROOF", AwardCategory.NEWCOMER,
            "W-001", "W-001-V1", "P-200", True,
        )
        problems = self.svc.awards.validate_qualification("C-NOPROOF", REFERENCE_DATE)
        self.assertTrue(any("年龄证明" in p for p in problems))

    def test_prior_newcomer_award_blocks_newcomer(self) -> None:
        self.svc.archive.add_prior_award(
            SEC, "P-003", "往届新人奖", AwardCategory.NEWCOMER, 2023
        )
        # 候选快照建立在加历史获奖之前——这是追加事实，重新对当前档案生成快照校验
        # 资格校验以快照为准，因此这里直接检查"历史获奖限制"规则经快照生效的路径：
        # 为该人员新建一条候选即可冻结到历史获奖
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-NEW2", AwardCategory.NEWCOMER,
            "W-001", "W-001-V1", "P-003", True,
        )
        problems = self.svc.awards.validate_qualification("C-NEW2", REFERENCE_DATE)
        self.assertTrue(any("不具备新人奖资格" in p for p in problems))

    def test_work_award_requires_originality_and_rights(self) -> None:
        self.svc.archive.register_work(SEC, "W-300", "相声", "缺声明作品", "h-300")
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-W300", AwardCategory.PROGRAM,
            "W-300", "W-300-V1", None, True,
        )
        problems = self.svc.awards.validate_qualification("C-W300", REFERENCE_DATE)
        self.assertTrue(any("原创声明" in p for p in problems))
        self.assertTrue(any("权利人" in p for p in problems))


class QuotaAndExclusivityTest(AwardFixture):
    def test_quota_blocks_over_capacity(self) -> None:
        self.svc.awards.configure_quota(
            ORG, QuotaRule(AwardCategory.NEWCOMER, max_winners=1)
        )
        self.svc.awards.confirm_award(ORG, "C-FIN-NEW", REFERENCE_DATE)
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-NEW-X", AwardCategory.NEWCOMER,
            "W-001", "W-001-V1", "P-002", True,
        )
        with self.assertRaises(QuotaError):
            self.svc.awards.confirm_award(ORG, "C-NEW-X", REFERENCE_DATE)

    def test_work_awards_exclusive_per_work(self) -> None:
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-LIT", AwardCategory.LITERATURE,
            "W-001", "W-001-V1", None, True,
        )
        self.svc.awards.confirm_award(ORG, "C-FIN-PROG", REFERENCE_DATE)
        with self.assertRaises(QuotaError):
            self.svc.awards.confirm_award(ORG, "C-LIT", REFERENCE_DATE)
        # 不同作品互不影响
        self.svc.archive.register_work(SEC, "W-400", "评书", "另一部书", "h-400")
        self.svc.archive.declare_originality(SEC, "W-400", "W-400-V1", True)
        self.svc.archive.add_author(SEC, "W-400", "P-003")
        self.svc.archive.add_rights_holder(SEC, "W-400", "P-003")
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-LIT2", AwardCategory.LITERATURE,
            "W-400", "W-400-V1", None, True,
        )
        self.svc.awards.confirm_award(ORG, "C-LIT2", REFERENCE_DATE)

    def test_person_awards_exclusive_per_person(self) -> None:
        self.svc.awards.confirm_award(ORG, "C-FIN-PERF", REFERENCE_DATE)
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-NEW-P2", AwardCategory.NEWCOMER,
            "W-001", "W-001-V1", "P-002", True,
        )
        with self.assertRaises(QuotaError):
            self.svc.awards.confirm_award(ORG, "C-NEW-P2", REFERENCE_DATE)

    def test_invalidation_releases_quota(self) -> None:
        self.svc.awards.configure_quota(
            ORG, QuotaRule(AwardCategory.PROGRAM, max_winners=1,
                           exclusive_with=(AwardCategory.LITERATURE,))
        )
        self.svc.awards.confirm_award(ORG, "C-FIN-PROG", REFERENCE_DATE)
        self.svc.awards.invalidate_result(ORG, "C-FIN-PROG", "公示申诉成立")
        status = self.svc.awards.quota_status()["节目奖"]
        self.assertEqual(status["used"], 0)
        self.assertEqual(status["remaining"], 1)


class ConfirmationGuardTest(AwardFixture):
    def test_secretariat_and_officer_cannot_confirm(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.svc.awards.confirm_award(SEC, "C-FIN-PROG", REFERENCE_DATE)
        with self.assertRaises(AuthorizationError):
            self.svc.awards.confirm_award(OFFICER, "C-FIN-PROG", REFERENCE_DATE)

    def test_locked_candidacy_cannot_confirm(self) -> None:
        appeal_id = self.svc.appeals.file_appeal(
            SEC, AppealTarget.QUALIFICATION, "资格存疑",
            candidacy_id="C-FIN-PROG",
        )
        with self.assertRaises(AppealLockError):
            self.svc.awards.confirm_award(ORG, "C-FIN-PROG", REFERENCE_DATE)
        self.svc.appeals.resolve_appeal(ORG, appeal_id, False, "资格合规")
        self.svc.awards.confirm_award(ORG, "C-FIN-PROG", REFERENCE_DATE)


class TraceResultTest(AwardFixture):
    def test_trace_covers_lineage_seal_recusal_and_appeal(self) -> None:
        # 重复关联
        suspect_id = self.svc.archive.scan_duplicate_suspects(SEC)[0]
        self.svc.archive.confirm_duplicate(
            OFFICER, suspect_id, "同一文本改名报送",
            [{"doc_ref": "D1", "kind": "文本同一性鉴定"}],
        )
        # 回避
        self.svc.jury.register_judge(ORG, "J-A", "P-001", "钱某", org_id="org-a")
        self.svc.jury.evaluate_recusals(ORG, "R-FIN")
        # 改变裁定的申诉
        self.svc.archive.register_person(SEC, "P-J9", "评委九")
        alias = register_judge(self.svc, "J-9", "P-J9", "评委九", round_id="R-FIN")
        score_id = self.svc.jury.submit_score(
            alias, "R-FIN", "W-001", "W-001-V1", 77
        )
        appeal_id = self.svc.appeals.file_appeal(
            SEC, AppealTarget.SCORE, "评分异常",
            candidacy_id="C-FIN-PROG", work_id="W-001", score_ids=[score_id],
        )
        self.svc.appeals.resolve_appeal(ORG, appeal_id, True, "排除该评分")
        self.svc.awards.confirm_award(ORG, "C-FIN-PROG", REFERENCE_DATE)

        trace = self.svc.awards.trace_result("C-FIN-PROG")

        # 作品沿革
        lineage = trace["work_lineage"]
        self.assertEqual(lineage["duplicate_group_id"], "GRP-DUP-001")
        self.assertTrue(lineage["duplicate_links"][0]["same_text"])
        titles = [v["title"] for v in lineage["versions"]]
        self.assertIn("湖畔新声", titles)

        # 封存轮次可追到初/分赛区（这里只有分赛区）
        sealed_ids = {r["round_id"] for r in trace["sealed_rounds"]}
        self.assertIn("R-REG", sealed_ids)
        self.assertEqual(len(trace["sealed_rounds"][0]["materials_fingerprint"]), 64)

        # 回避：哪位评委因何回避
        recusal = next(r for r in trace["recusals"] if r["judge_id"] == "J-A")
        self.assertTrue(recusal["reason"])
        self.assertIn("W-001", recusal["detail"])

        # 申诉：哪一条改变了裁定
        appeal_trace = next(a for a in trace["appeals"] if a["appeal_id"] == appeal_id)
        self.assertTrue(appeal_trace["changed_this_ruling"])
        self.assertEqual(appeal_trace["status"], "裁定变更")
        self.assertEqual(trace["result"]["status"], ResultStatus.CONFIRMED.value)
        excluded = [s for s in trace["scores"] if s["state"] == "已被申诉排除"]
        self.assertEqual(len(excluded), 1)


if __name__ == "__main__":
    unittest.main()
