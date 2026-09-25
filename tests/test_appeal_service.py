"""申诉服务测试：证据精确锁定与裁定影响。"""

from __future__ import annotations

import unittest
from datetime import date

from src.quyi.errors import AppealLockError
from src.quyi.models import (
    AppealStatus,
    AppealTarget,
    AwardCategory,
    CandidacyStatus,
    RoundKind,
)

from .domain_support import (
    ORG,
    REFERENCE_DATE,
    SEC,
    build_services,
    create_sealed_round,
    final_candidacies,
    register_judge,
)


class AppealLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        create_sealed_round(
            self.svc, "R-REG", RoundKind.REGIONAL, "华东赛区", region="华东",
            candidacy_specs=[
                ("C-R1", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True),
                ("C-R2", AwardCategory.NEWCOMER, "W-001", "W-001-V1", "P-003", True),
            ],
        )
        final_candidacies(self.svc)
        self.svc.archive.register_person(SEC, "P-J1", "评委甲")
        self.alias = register_judge(self.svc, "J-1", "P-J1", "评委甲", round_id="R-FIN")
        self.score_id = self.svc.jury.submit_score(
            self.alias, "R-FIN", "W-001", "W-001-V1", 90
        )

    def test_open_appeal_locks_score_and_candidacy(self) -> None:
        appeal_id = self.svc.appeals.file_appeal(
            SEC, AppealTarget.SCORE, "评分异常",
            candidacy_id="C-FIN-PROG", work_id="W-001", score_ids=[self.score_id],
        )
        self.assertTrue(self.svc.store.scores[self.score_id].locked_by_appeal)
        self.assertTrue(self.svc.appeals.candidacy_locked("C-FIN-PROG"))
        # 锁定中的评分不能修改
        with self.assertRaises(AppealLockError):
            self.svc.jury.submit_score(self.alias, "R-FIN", "W-001", "W-001-V1", 30)
        # 无关候选不被锁定，照常可确认
        self.assertEqual(self.svc.awards.can_confirm("C-FIN-NEW", REFERENCE_DATE), [])
        self.svc.awards.confirm_award(ORG, "C-FIN-NEW", REFERENCE_DATE)
        self.assertEqual(self.svc.appeals.open_appeals()[0].appeal_id, appeal_id)

    def test_upheld_score_appeal_excludes_score(self) -> None:
        appeal_id = self.svc.appeals.file_appeal(
            SEC, AppealTarget.SCORE, "评分异常",
            candidacy_id="C-FIN-PROG", work_id="W-001", score_ids=[self.score_id],
        )
        appeal = self.svc.appeals.resolve_appeal(
            ORG, appeal_id, True, "依据不足，排除该评分"
        )
        self.assertEqual(appeal.status, AppealStatus.UPHELD)
        self.assertTrue(self.svc.store.scores[self.score_id].excluded)
        self.assertFalse(self.svc.store.scores[self.score_id].locked_by_appeal)
        self.assertIn("C-FIN-PROG", appeal.changed_candidacies)
        # 现行评分计算排除被申诉排除的评分
        self.assertEqual(
            self.svc.jury.current_scores("R-FIN", "W-001", "W-001-V1"), []
        )

    def test_rejected_appeal_releases_lock(self) -> None:
        appeal_id = self.svc.appeals.file_appeal(
            SEC, AppealTarget.SCORE, "评分异常",
            candidacy_id="C-FIN-PROG", work_id="W-001", score_ids=[self.score_id],
        )
        self.svc.appeals.resolve_appeal(ORG, appeal_id, False, "评分合规")
        self.assertFalse(self.svc.store.scores[self.score_id].locked_by_appeal)
        self.assertEqual(self.svc.appeals.candidacy_locked("C-FIN-PROG"), [])
        # 锁解除后评分可更新
        self.svc.jury.submit_score(self.alias, "R-FIN", "W-001", "W-001-V1", 91)

    def test_qualification_appeal_upheld_revokes_candidacy(self) -> None:
        appeal_id = self.svc.appeals.file_appeal(
            SEC, AppealTarget.QUALIFICATION, "年龄证明存疑",
            candidacy_id="C-FIN-NEW",
        )
        self.svc.appeals.resolve_appeal(ORG, appeal_id, True, "资格不成立")
        self.assertEqual(
            self.svc.store.candidacies["C-FIN-NEW"].status, CandidacyStatus.REVOKED
        )
        problems = self.svc.awards.validate_qualification("C-FIN-NEW", REFERENCE_DATE)
        self.assertTrue(any("撤销" in p for p in problems))

    def test_text_appeal_locks_version(self) -> None:
        appeal_id = self.svc.appeals.file_appeal(
            SEC, AppealTarget.TEXT, "文本涉嫌抄袭",
            work_id="W-001", version_id="W-001-V1",
        )
        self.assertIn(appeal_id, self.svc.appeals.version_locked("W-001-V1"))
        problems = self.svc.awards.validate_qualification("C-FIN-PROG", REFERENCE_DATE)
        self.assertTrue(any("文本" in p and "申诉" in p for p in problems))
        # 撤回后解锁
        self.svc.appeals.withdraw_appeal(SEC, appeal_id)
        self.assertEqual(self.svc.appeals.version_locked("W-001-V1"), [])

    def test_text_appeal_follows_same_text_into_new_live_version(self) -> None:
        from src.quyi.models import EventType
        from datetime import datetime, timedelta, timezone

        # 重新表演产生沿用同一文本指纹的新版本
        event = self.svc.live.record_event(
            ORG, "R-FIN", "W-001", EventType.REPERFORMANCE, "设备恢复后重演",
            occurred_at=datetime(2026, 6, 1, 20, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertNotEqual(event.new_version_id, "W-001-V1")
        self.assertEqual(
            self.svc.store.work("W-001").get_version(event.new_version_id).text_hash,
            self.svc.store.work("W-001").get_version("W-001-V1").text_hash,
        )
        # 对旧版本文本的申诉同样锁定现行新版本候选
        self.svc.appeals.file_appeal(
            SEC, AppealTarget.TEXT, "底本涉嫌侵权",
            work_id="W-001", version_id="W-001-V1",
        )
        problems = self.svc.awards.validate_qualification("C-FIN-PROG", REFERENCE_DATE)
        self.assertTrue(any("文本" in p and "申诉" in p for p in problems))
        # 成立：撤销使用同一文本的全部候选（含新版本候选）
        appeal_id = self.svc.appeals.open_appeals()[0].appeal_id
        appeal = self.svc.appeals.resolve_appeal(ORG, appeal_id, True, "文本侵权成立")
        self.assertIn("C-FIN-PROG", appeal.changed_candidacies)
        self.assertEqual(
            self.svc.store.candidacies["C-FIN-PROG"].status, CandidacyStatus.REVOKED
        )


if __name__ == "__main__":
    unittest.main()
