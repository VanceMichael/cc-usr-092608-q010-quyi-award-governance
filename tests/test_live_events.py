"""现场终评事件测试：换角、中止、重新表演对资格与评分的影响。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from src.quyi.errors import AuthorizationError, RecusalError
from src.quyi.models import (
    AwardCategory,
    CandidacyStatus,
    CrewStatus,
    EventType,
    RoundKind,
    WorkRelation,
)

from .domain_support import (
    ORG,
    SEC,
    build_services,
    create_sealed_round,
    final_candidacies,
    register_judge,
)

T = datetime(2026, 6, 1, 19, 30, tzinfo=timezone(timedelta(hours=8)))


class LiveEventFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        create_sealed_round(
            self.svc, "R-REG", RoundKind.REGIONAL, "华东赛区", region="华东",
            candidacy_specs=[
                ("C-R-PERF", AwardCategory.PERFORMANCE, "W-001", "W-001-V1", "P-002", True),
                ("C-R-NEW", AwardCategory.NEWCOMER, "W-001", "W-001-V1", "P-003", True),
                ("C-R-PROG", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True),
            ],
        )
        final_candidacies(self.svc)
        self.svc.archive.register_person(SEC, "P-J1", "评委甲")
        self.alias = register_judge(self.svc, "J-1", "P-J1", "评委甲", round_id="R-FIN")
        self.svc.archive.register_person(SEC, "P-006", "褚某（接替演员）")
        self.svc.archive.record_age_proof(SEC, "P-006", date(2000, 1, 1), "DOC-6")
        self.svc.archive.add_membership(SEC, "P-006", "org-a", date(2019, 1, 1))
        self.svc.archive.add_appearance(
            SEC, "P-006", "W-001", "W-001-V1", WorkRelation.OTHER_CREW, "org-a"
        )


class CastChangeTest(LiveEventFixture):
    def test_cast_change_revokes_outgoing_and_creates_incoming_candidacy(self) -> None:
        self.svc.jury.submit_score(self.alias, "R-FIN", "W-001", "W-001-V1", 90)
        event = self.svc.live.record_event(
            ORG, "R-FIN", "W-001", EventType.CAST_CHANGE, "演员伤病",
            occurred_at=T, outgoing_person_id="P-002", incoming_person_id="P-006",
        )
        # 原表演候选资格丧失；新人候选（P-003）与节目候选仅切换版本
        self.assertEqual(self.svc.store.candidacies["C-FIN-PERF"].status, CandidacyStatus.REVOKED)
        self.assertNotEqual(
            self.svc.store.candidacies["C-FIN-PERF"].status,
            self.svc.store.candidacies["C-FIN-NEW"].status,
        )
        self.assertEqual(
            self.svc.store.candidacies["C-FIN-NEW"].effective_version_id, event.new_version_id
        )
        # 接替演员产生同类别（表演奖）新候选
        new_cand = next(
            c for c in self.svc.store.candidacies.values()
            if c.person_id == "P-006" and c.source_round_id == "R-FIN"
        )
        self.assertEqual(new_cand.category, AwardCategory.PERFORMANCE)
        self.assertEqual(new_cand.version_id, event.new_version_id)
        # 演职关系：原演员退出，新演员参演新版本
        self.assertTrue(
            all(
                a.status == CrewStatus.WITHDRAWN
                for p in self.svc.store.persons.values() if p.person_id == "P-002"
                for a in p.appearances if a.work_id == "W-001" and a.relation == WorkRelation.PERFORMER
            )
        )

    def test_old_version_scores_archived_but_viewable(self) -> None:
        score_id = self.svc.jury.submit_score(
            self.alias, "R-FIN", "W-001", "W-001-V1", 90
        )
        event = self.svc.live.record_event(
            ORG, "R-FIN", "W-001", EventType.CAST_CHANGE, "演员伤病",
            occurred_at=T, outgoing_person_id="P-002", incoming_person_id="P-006",
        )
        old_score = self.svc.store.scores[score_id]
        self.assertTrue(old_score.superseded)
        self.assertEqual(old_score.value, 90)  # 数值仍可查看
        # 旧版本不能继续评分，新版本可以重新打分
        with self.assertRaises(RecusalError):
            self.svc.jury.submit_score(self.alias, "R-FIN", "W-001", "W-001-V1", 50)
        self.svc.jury.submit_score(self.alias, "R-FIN", "W-001", event.new_version_id, 93)

    def test_lineage_retains_old_version(self) -> None:
        event = self.svc.live.record_event(
            ORG, "R-FIN", "W-001", EventType.CAST_CHANGE, "演员伤病",
            occurred_at=T, outgoing_person_id="P-002", incoming_person_id="P-006",
        )
        versions = self.svc.store.work("W-001").versions
        version_ids = [v.version_id for v in versions]
        self.assertIn("W-001-V1", version_ids)
        self.assertIn(event.new_version_id, version_ids)


class SuspensionReperformanceTest(LiveEventFixture):
    def test_suspension_then_reperformance_rotates_version(self) -> None:
        self.svc.jury.submit_score(self.alias, "R-FIN", "W-001", "W-001-V1", 80)
        self.svc.live.record_event(
            ORG, "R-FIN", "W-001", EventType.SUSPENSION, "设备故障", occurred_at=T
        )
        self.assertTrue(self.svc.live.is_suspended("R-FIN", "W-001"))
        event = self.svc.live.record_event(
            ORG, "R-FIN", "W-001", EventType.REPERFORMANCE, "恢复后重演",
            occurred_at=T + timedelta(minutes=30),
        )
        self.assertFalse(self.svc.live.is_suspended("R-FIN", "W-001"))
        old_scores = [
            s for s in self.svc.store.scores.values()
            if s.work_id == "W-001" and s.version_id == "W-001-V1"
        ]
        self.assertTrue(old_scores and all(s.superseded for s in old_scores))
        self.svc.jury.submit_score(self.alias, "R-FIN", "W-001", event.new_version_id, 96)
        self.assertEqual(
            self.svc.store.candidacies["C-FIN-PROG"].effective_version_id,
            event.new_version_id,
        )


class LiveEventGuardTest(unittest.TestCase):
    def test_event_requires_sealed_path_and_final_candidacy(self) -> None:
        svc = build_services()
        # 全新作品未经任何封存轮次，不允许登记现场事件
        svc.archive.register_work(SEC, "W-900", "相声", "无资格作品", "hash-900")
        svc.rounds.create_round(ORG, "R-FIN", RoundKind.FINAL, "终评", year=2026)
        with self.assertRaises(AuthorizationError):
            svc.live.record_event(
                ORG, "R-FIN", "W-900", EventType.SUSPENSION, "中止"
            )

    def test_secretariat_cannot_record_live_event(self) -> None:
        svc = build_services()
        create_sealed_round(
            svc, "R-REG", RoundKind.REGIONAL, "华东赛区", region="华东",
            candidacy_specs=[
                ("C-R1", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True),
            ],
        )
        final_candidacies(svc)
        with self.assertRaises(AuthorizationError):
            svc.live.record_event(
                SEC, "R-FIN", "W-001", EventType.SUSPENSION, "中止"
            )


if __name__ == "__main__":
    unittest.main()
