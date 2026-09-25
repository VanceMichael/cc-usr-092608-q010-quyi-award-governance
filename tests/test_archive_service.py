"""档案服务测试：作品谱系、人员资格事实、疑似重复确认边界。"""

from __future__ import annotations

import unittest
from datetime import date

from src.quyi.errors import (
    AuthorizationError,
    DuplicateError,
    DuplicateLinkError,
)
from src.quyi.models import AwardCategory, WorkRelation

from .domain_support import OFFICER, ORG, SEC, build_services


class WorkLineageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()

    def test_rename_adds_version_without_overwriting_history(self) -> None:
        work = self.svc.store.work("W-001")
        before = len(work.versions)
        version = self.svc.archive.add_text_version(
            SEC, "W-001", "湖畔新声（巡演版）", "hash-text-hu-an-0002", "巡演修改"
        )
        self.assertEqual(version.version_id, "W-001-V2")
        self.assertEqual(work.current_title, "湖畔新声（巡演版）")
        self.assertEqual(len(work.versions), before + 1)
        self.assertEqual(work.versions[0].title, "湖畔新声")

    def test_duplicate_version_rejected(self) -> None:
        with self.assertRaises(DuplicateError):
            self.svc.archive.add_text_version(
                SEC, "W-001", "湖畔新声", "hash-text-hu-an-0001", "重复登记"
            )

    def test_originality_and_first_performance_are_version_facts(self) -> None:
        self.svc.archive.declare_first_performance(SEC, "W-001", "W-001-V1", date(2024, 5, 1))
        self.svc.archive.declare_originality(SEC, "W-001", "W-001-V1", False, "合作改编待核")
        version = self.svc.store.work("W-001").get_version("W-001-V1")
        self.assertFalse(version.originality_declared)
        self.assertEqual(version.first_performed_at, date(2024, 5, 1))

    def test_merged_work_cannot_receive_new_version(self) -> None:
        suspect_id = self.svc.archive.scan_duplicate_suspects(SEC)[0]
        self.svc.archive.confirm_duplicate(
            OFFICER, suspect_id, "同一文本改名报送",
            [{"doc_ref": "D1", "kind": "文本同一性鉴定"}],
        )
        with self.assertRaises(DuplicateLinkError):
            self.svc.archive.add_text_version(
                SEC, "W-002", "再改名", "hash-text-hu-an-0001", "并入后追加"
            )


class PersonQualificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()

    def test_person_can_appear_through_different_orgs(self) -> None:
        person = self.svc.store.person("P-002")
        orgs = {(a.work_id, a.org_id) for a in person.appearances if a.relation == WorkRelation.PERFORMER}
        self.assertIn(("W-001", "org-a"), orgs)
        self.assertIn(("W-002", "org-b"), orgs)
        self.assertEqual(person.org_at(date(2023, 6, 1)), "org-a")
        self.assertEqual(person.org_at(date(2025, 6, 1)), "org-b")

    def test_age_proof_rejects_future_birth_date(self) -> None:
        self.svc.archive.register_person(SEC, "P-100", "测试演员")
        with self.assertRaises(ValueError):
            self.svc.archive.record_age_proof(SEC, "P-100", date(2099, 1, 1), "DOC-X")

    def test_prior_award_is_recorded_for_limits(self) -> None:
        self.svc.archive.add_prior_award(
            SEC, "P-003", "上届曲艺新人奖", AwardCategory.NEWCOMER, 2024
        )
        prior = self.svc.store.person("P-003").prior_awards
        self.assertEqual(prior[0].category, AwardCategory.NEWCOMER)


class DuplicateConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()

    def test_same_text_scan_creates_suspect_but_no_merge(self) -> None:
        suspects = self.svc.archive.scan_duplicate_suspects(SEC)
        self.assertEqual(len(suspects), 1)
        suspect = self.svc.store.suspects[suspects[0]]
        self.assertEqual(suspect.basis, "文本指纹相同")
        self.assertFalse(suspect.resolved)
        self.assertIsNone(self.svc.store.work("W-002").merged_into)

    def test_secretariat_cannot_confirm_duplicate(self) -> None:
        suspect_id = self.svc.archive.scan_duplicate_suspects(SEC)[0]
        with self.assertRaises(AuthorizationError):
            self.svc.archive.confirm_duplicate(
                SEC, suspect_id, "文本相同",
                [{"doc_ref": "D", "kind": "文本同一性鉴定"}],
            )

    def test_name_similarity_alone_never_merges(self) -> None:
        self.svc.archive.register_work(SEC, "W-099", "评书", "湖畔新声传", "hash-other-99")
        self.svc.archive.scan_duplicate_suspects(SEC, threshold=0.3)
        suspect = next(
            s for s in self.svc.store.suspects.values() if s.basis.startswith("标题相似度")
        )
        # 即使附上与同一性无关的单位证明，也不能按名称合并
        with self.assertRaises(DuplicateLinkError):
            self.svc.archive.confirm_duplicate(
                OFFICER, suspect.suspect_id, "标题接近",
                [{"doc_ref": "D", "kind": "单位证明", "summary": "同城"}],
            )

    def test_identity_appraisal_evidence_allows_link(self) -> None:
        self.svc.archive.register_work(SEC, "W-099", "评书", "完全不同的名字", "hash-other-99")
        self.svc.archive.scan_duplicate_suspects(SEC, threshold=0.0)
        suspect = next(
            s for s in self.svc.store.suspects.values()
            if {s.work_id_a, s.work_id_b} == {"W-001", "W-099"}
        )
        group = self.svc.archive.confirm_duplicate(
            OFFICER, suspect.suspect_id, "专家鉴定为同一底本流变",
            [{"doc_ref": "D2", "kind": "文本同一性鉴定", "summary": "底本同源"}],
        )
        self.assertTrue(group.startswith("GRP-"))
        self.assertEqual(self.svc.store.work("W-099").merged_into, "W-001")

    def test_officer_can_reject_suspect(self) -> None:
        self.svc.archive.register_work(SEC, "W-099", "评书", "湖畔风声", "hash-other-99")
        self.svc.archive.scan_duplicate_suspects(SEC, threshold=0.3)
        suspect = next(
            s for s in self.svc.store.suspects.values() if s.basis.startswith("标题相似度")
        )
        self.svc.archive.reject_suspect(OFFICER, suspect.suspect_id, "并非同一作品")
        self.assertTrue(suspect.resolved)
        self.assertIsNone(self.svc.store.work("W-099").duplicate_group_id)


if __name__ == "__main__":
    unittest.main()
