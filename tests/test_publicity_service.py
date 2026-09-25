"""公示服务测试：停机顺延与剩余期限保护。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.quyi.errors import TimelineError
from src.quyi.models import PublicityStatus

from .domain_support import ORG, SEC, build_services

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 9, 10, 9, 0, tzinfo=TZ)
DEADLINE = START + timedelta(days=7)


class PublicityFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_services()
        self.svc.publicity.start_publicity(ORG, "PUB-1", START, DEADLINE)

    def test_downtime_extends_deadline_by_overlap(self) -> None:
        self.svc.publicity.report_downtime_start(
            SEC, "PUB-1", START + timedelta(days=2), "机房迁移"
        )
        extension = self.svc.publicity.report_downtime_end(
            SEC, "PUB-1", START + timedelta(days=3)
        )
        self.assertEqual(extension, timedelta(days=1))
        self.assertEqual(
            self.svc.publicity.effective_deadline("PUB-1"),
            DEADLINE + timedelta(days=1),
        )

    def test_multiple_downtimes_accumulate(self) -> None:
        # 第一段 12 小时
        self.svc.publicity.report_downtime_start(
            SEC, "PUB-1", START + timedelta(days=1), "停电"
        )
        self.svc.publicity.report_downtime_end(
            SEC, "PUB-1", START + timedelta(days=1, hours=12)
        )
        # 第二段 2 天
        self.svc.publicity.report_downtime_start(
            SEC, "PUB-1", START + timedelta(days=4), "升级"
        )
        self.svc.publicity.report_downtime_end(
            SEC, "PUB-1", START + timedelta(days=6)
        )
        self.assertEqual(
            self.svc.publicity.downtime_extension("PUB-1"),
            timedelta(days=2, hours=12),
        )
        self.assertEqual(
            self.svc.publicity.effective_deadline("PUB-1"),
            DEADLINE + timedelta(days=2, hours=12),
        )

    def test_downtime_outside_window_does_not_count(self) -> None:
        self.svc.publicity.report_downtime_start(
            SEC, "PUB-1", START - timedelta(days=2), "公示前演练"
        )
        extension = self.svc.publicity.report_downtime_end(
            SEC, "PUB-1", START - timedelta(days=1)
        )
        self.assertEqual(extension, timedelta(0))
        self.assertEqual(self.svc.publicity.effective_deadline("PUB-1"), DEADLINE)

    def test_partial_overlap_downtime(self) -> None:
        # 停机在截止后才恢复，只计到原定截止
        self.svc.publicity.report_downtime_start(
            SEC, "PUB-1", DEADLINE - timedelta(hours=6), "临期故障"
        )
        extension = self.svc.publicity.report_downtime_end(
            SEC, "PUB-1", DEADLINE + timedelta(days=2)
        )
        self.assertEqual(extension, timedelta(hours=6))

    def test_cannot_end_before_effective_deadline(self) -> None:
        self.svc.publicity.report_downtime_start(
            SEC, "PUB-1", START + timedelta(days=2), "停机一天"
        )
        self.svc.publicity.report_downtime_end(
            SEC, "PUB-1", START + timedelta(days=3)
        )
        # 原定截止时刻已到，但剩余期限被停机吃掉一天，必须顺延
        with self.assertRaises(TimelineError):
            self.svc.publicity.end_publicity(ORG, "PUB-1", DEADLINE)
        deadline = self.svc.publicity.end_publicity(
            ORG, "PUB-1", DEADLINE + timedelta(days=1)
        )
        self.assertEqual(deadline, DEADLINE + timedelta(days=1))
        self.assertEqual(
            self.svc.store.publicities["PUB-1"].status, PublicityStatus.ENDED
        )

    def test_open_downtime_blocks_end(self) -> None:
        self.svc.publicity.report_downtime_start(SEC, "PUB-1", START + timedelta(days=1))
        with self.assertRaises(TimelineError):
            self.svc.publicity.end_publicity(
                ORG, "PUB-1", DEADLINE + timedelta(days=10)
            )


if __name__ == "__main__":
    unittest.main()
