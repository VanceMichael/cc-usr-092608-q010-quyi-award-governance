"""轮次封存与资格复核测试。"""

from __future__ import annotations

import unittest

from src.quyi.errors import SealedError
from src.quyi.models import AwardCategory, RoundKind

from .domain_support import (
    ORG,
    REVIEWER,
    SEC,
    build_services,
    create_sealed_round,
)


class RoundSealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        create_sealed_round(
            self.svc,
            "R-PRE",
            RoundKind.PRELIMINARY,
            "初评",
            candidacy_specs=[
                ("C-1", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True),
                ("C-2", AwardCategory.PERFORMANCE, "W-001", "W-001-V1", "P-002", False),
            ],
        )

    def test_seal_fingerprints_verify(self) -> None:
        seal = self.svc.store.rounds["R-PRE"].sealed
        self.assertIsNotNone(seal)
        self.assertEqual(len(seal.materials_fingerprint), 64)
        self.assertTrue(self.svc.rounds.verify_seal("R-PRE"))

    def test_sealed_round_rejects_new_material_and_advancement(self) -> None:
        with self.assertRaises(SealedError):
            self.svc.rounds.add_material(SEC, "R-PRE", "late", "hash-late", "文本")
        with self.assertRaises(SealedError):
            self.svc.rounds.record_advancement(
                SEC, "R-PRE", "C-3", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True
            )

    def test_cannot_seal_with_missing_materials(self) -> None:
        self.svc.rounds.create_round(ORG, "R-REG-X", RoundKind.REGIONAL, "某赛区", region="华南")
        self.svc.rounds.add_material(SEC, "R-REG-X", "d0", "h0", "报名表")
        self.svc.rounds.record_advancement(
            SEC, "R-REG-X", "C-X", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True
        )
        with self.assertRaises(SealedError):
            self.svc.rounds.seal_round(SEC, "R-REG-X")


class PreliminaryAndRegionalSealedSeparatelyTest(unittest.TestCase):
    def test_two_rounds_have_independent_fingerprints(self) -> None:
        svc = build_services()
        create_sealed_round(
            svc, "R-PRE", RoundKind.PRELIMINARY, "初评",
            candidacy_specs=[("C-1", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True)],
        )
        create_sealed_round(
            svc, "R-REG", RoundKind.REGIONAL, "华东赛区", region="华东",
            candidacy_specs=[("C-2", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True)],
        )
        self.assertNotEqual(
            svc.store.rounds["R-PRE"].sealed.advancement_fingerprint,
            svc.store.rounds["R-REG"].sealed.advancement_fingerprint,
        )
        self.assertTrue(svc.rounds.verify_seal("R-PRE"))
        self.assertTrue(svc.rounds.verify_seal("R-REG"))


class SupplementaryReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        create_sealed_round(
            self.svc,
            "R-REG",
            RoundKind.REGIONAL,
            "华东赛区",
            region="华东",
            candidacy_specs=[
                ("C-NEW", AwardCategory.NEWCOMER, "W-001", "W-001-V1", "P-003", True),
            ],
        )

    def test_supplement_before_seal_is_rejected(self) -> None:
        self.svc.rounds.create_round(ORG, "R-OPEN", RoundKind.REGIONAL, "未封存赛区", region="华北")
        with self.assertRaises(SealedError):
            self.svc.rounds.submit_supplement(
                SEC, "C-NEW", "R-OPEN", "S-X", "年龄证明", "2002-09-01"
            )

    def test_supplement_enters_review_without_touching_sealed_round(self) -> None:
        materials_before = len(self.svc.store.rounds["R-REG"].materials)
        fp_before = self.svc.store.rounds["R-REG"].sealed.materials_fingerprint
        evidence_id = self.svc.rounds.submit_supplement(
            SEC, "C-NEW", "R-REG", "S-1", "年龄证明", "2002-09-01"
        )
        self.assertEqual(self.svc.rounds.pending_supplements(REVIEWER), [evidence_id])
        self.svc.rounds.review_supplement(
            REVIEWER, evidence_id, True, "年龄材料采信", "终评资格复核"
        )
        # 原轮次材料与封存指纹完全不变
        self.assertEqual(len(self.svc.store.rounds["R-REG"].materials), materials_before)
        self.assertEqual(
            self.svc.store.rounds["R-REG"].sealed.materials_fingerprint, fp_before
        )
        self.assertTrue(self.svc.rounds.verify_seal("R-REG"))
        # 复核结论挂在候选资格上
        addenda = self.svc.store.candidacies["C-NEW"].qualification_snapshot["review_addenda"]
        self.assertTrue(addenda[0]["accepted"])

    def test_rejected_supplement_also_leaves_round_unchanged(self) -> None:
        evidence_id = self.svc.rounds.submit_supplement(
            SEC, "C-NEW", "R-REG", "S-2", "年龄证明", "1990-01-01"
        )
        self.svc.rounds.review_supplement(
            REVIEWER, evidence_id, False, "材料不符，不予采信", "终评资格复核"
        )
        addenda = self.svc.store.candidacies["C-NEW"].qualification_snapshot["review_addenda"]
        self.assertFalse(addenda[0]["accepted"])
        self.assertTrue(self.svc.rounds.verify_seal("R-REG"))

    def test_secretariat_cannot_review_supplement(self) -> None:
        evidence_id = self.svc.rounds.submit_supplement(
            SEC, "C-NEW", "R-REG", "S-3", "年龄证明", "2002-09-01"
        )
        from src.quyi.errors import AuthorizationError

        with self.assertRaises(AuthorizationError):
            self.svc.rounds.review_supplement(
                SEC, evidence_id, True, "越权采信", "终评资格复核"
            )

    def test_accepted_age_supplement_cures_missing_proof_for_award_check(self) -> None:
        from datetime import date

        from src.quyi.models import RoundKind, WorkRelation

        # 一名没有年龄证明的新人候选：先应被拦截
        self.svc.archive.register_person(SEC, "P-NP", "无证明青年演员")
        self.svc.archive.add_membership(SEC, "P-NP", "org-a", date(2022, 1, 1))
        self.svc.archive.add_appearance(
            SEC, "P-NP", "W-001", "W-001-V1",
            WorkRelation.PERFORMER, "org-a",
        )
        self.svc.rounds.create_round(
            ORG, "R-FIN", RoundKind.FINAL, "终评", year=2026
        )
        self.svc.rounds.record_advancement(
            SEC, "R-FIN", "C-NP", AwardCategory.NEWCOMER,
            "W-001", "W-001-V1", "P-NP", True,
        )
        problems_before = self.svc.awards.validate_qualification(
            "C-NP", date(2026, 9, 25)
        )
        self.assertTrue(any("年龄证明" in p for p in problems_before))

        # 后补年龄证明经复核采信后，资格判定通过，且原分赛区封存不变
        evidence_id = self.svc.rounds.submit_supplement(
            SEC, "C-NP", "R-REG", "S-4", "年龄证明", "2001-05-05"
        )
        self.svc.rounds.review_supplement(
            REVIEWER, evidence_id, True, "后补年龄证明采信", "终评资格复核"
        )
        problems_after = self.svc.awards.validate_qualification(
            "C-NP", date(2026, 9, 25)
        )
        self.assertFalse(any("年龄证明" in p for p in problems_after))
        self.assertTrue(self.svc.rounds.verify_seal("R-REG"))
        # 不予采信则仍不合规
        evidence_id2 = self.svc.rounds.submit_supplement(
            SEC, "C-NP", "R-REG", "S-5", "其他证明", "无关内容"
        )
        self.svc.rounds.review_supplement(
            REVIEWER, evidence_id2, False, "与年龄无关", "终评资格复核"
        )
        self.assertTrue(self.svc.rounds.verify_seal("R-REG"))


if __name__ == "__main__":
    unittest.main()
