"""评委回避与匿名评分测试。"""

from __future__ import annotations

import unittest
from datetime import date

from src.quyi.errors import (
    AppealLockError,
    AuthorizationError,
    RecusalError,
    SealedError,
)
from src.quyi.models import AwardCategory, RecusalReason, RoundKind, WorkRelation

from .domain_support import (
    ORG,
    SEC,
    build_services,
    create_sealed_round,
    final_candidacies,
)


class RecusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        create_sealed_round(
            self.svc, "R-PRE", RoundKind.PRELIMINARY, "初评",
            candidacy_specs=[
                ("C-P1", AwardCategory.PERFORMANCE, "W-001", "W-001-V1", "P-002", True),
            ],
        )
        # 三名评委：本人参与作品、同单位、无关系
        self.svc.archive.register_person(SEC, "P-J1", "评委甲")
        self.svc.archive.register_person(SEC, "P-J2", "评委乙")
        self.svc.jury.register_judge(ORG, "J-AUTHOR", "P-001", "钱某", org_id="org-a")
        self.svc.jury.register_judge(ORG, "J-ORG", "P-J1", "评委甲", org_id="org-a")
        self.svc.jury.register_judge(ORG, "J-FREE", "P-J2", "评委乙")
        self.svc.jury.declare_relation(ORG, "J-FREE", "P-002", "师生")

    def test_recusals_cover_work_person_and_org_relations(self) -> None:
        created = self.svc.jury.evaluate_recusals(ORG, "R-PRE")
        reasons = {(r.judge_id, r.reason) for r in created}
        self.assertIn(("J-AUTHOR", RecusalReason.WORK_RELATION), reasons)
        self.assertIn(("J-ORG", RecusalReason.ORG_RELATION), reasons)
        self.assertIn(("J-FREE", RecusalReason.PERSON_RELATION), reasons)

    def test_recusal_is_idempotent(self) -> None:
        first = self.svc.jury.evaluate_recusals(ORG, "R-PRE")
        second = self.svc.jury.evaluate_recusals(ORG, "R-PRE")
        self.assertEqual(len(second), 0)
        self.assertEqual(len(self.svc.jury.recusals_for("R-PRE")), len(first))

    def test_assignable_judges_exclude_conflicted(self) -> None:
        self.svc.jury.evaluate_recusals(ORG, "R-PRE")
        assignable = self.svc.jury.assignable_judges(ORG, "R-PRE", "W-001")
        self.assertNotIn("J-AUTHOR", assignable)
        self.assertNotIn("J-ORG", assignable)
        self.assertNotIn("J-FREE", assignable)


class AnonymousScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        # 评分发生在进行中的轮次（封存后通道关闭，另见 test_sealed_round_closes_scoring）
        self.svc.rounds.create_round(ORG, "R-PRE", RoundKind.PRELIMINARY, "初评", year=2026)
        self.svc.rounds.record_advancement(
            SEC, "R-PRE", "C-P1", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True
        )
        self.svc.archive.register_person(SEC, "P-J1", "评委甲")
        self.svc.jury.register_judge(ORG, "J-1", "P-J1", "评委甲")
        self.alias = self.svc.jury.issue_alias(ORG, "J-1", "R-PRE")

    def test_secretariat_cannot_resolve_alias(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.svc.jury.resolve_alias(SEC, self.alias)
        self.assertEqual(self.svc.jury.resolve_alias(ORG, self.alias), "J-1")

    def test_score_only_by_alias_and_valid_range(self) -> None:
        self.svc.jury.submit_score(self.alias, "R-PRE", "W-001", "W-001-V1", 88.5)
        with self.assertRaises(ValueError):
            self.svc.jury.submit_score(self.alias, "R-PRE", "W-001", "W-001-V1", 101)
        with self.assertRaises(AuthorizationError):
            self.svc.jury.submit_score("J-FAKE", "R-PRE", "W-001", "W-001-V1", 80)

    def test_secretariat_ledger_has_no_identity(self) -> None:
        self.svc.jury.submit_score(self.alias, "R-PRE", "W-001", "W-001-V1", 88.5)
        ledger = self.svc.jury.ledger_for_secretariat(SEC, "R-PRE")
        self.assertEqual(len(ledger), 1)
        self.assertNotIn("judge_id", ledger[0])
        self.assertNotIn("person_id", ledger[0])
        self.assertEqual(ledger[0]["judge_alias"], self.alias)

    def test_recused_judge_cannot_score(self) -> None:
        # 让评委与作品产生单位关系并重发代号到同一轮次
        self.svc.archive.add_membership(
            SEC, "P-J1", "org-a", date(2020, 1, 1)
        )
        judge = self.svc.store.judges["J-1"]
        judge.org_id = "org-a"
        self.svc.jury.evaluate_recusals(ORG, "R-PRE")
        with self.assertRaises(RecusalError):
            self.svc.jury.submit_score(self.alias, "R-PRE", "W-001", "W-001-V1", 90)

    def test_sealed_round_closes_scoring(self) -> None:
        # 直接封存已封存轮次不可能；改用新轮次封存后评分
        create_sealed_round(
            self.svc, "R-REG", RoundKind.REGIONAL, "华东赛区", region="华东",
            candidacy_specs=[
                ("C-R1", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True),
            ],
        )
        alias = self.svc.jury.issue_alias(ORG, "J-1", "R-REG")
        with self.assertRaises(SealedError):
            self.svc.jury.submit_score(alias, "R-REG", "W-001", "W-001-V1", 90)


if __name__ == "__main__":
    unittest.main()
