"""奖项名额、兼得规则与获奖确认。

公示前作品类（节目奖、文学奖）与个人类（表演奖、新人奖）分别校验名额
与兼得；任何一类未通过都不得公示。被受理中申诉锁定的候选暂停确认，
其他候选与奖项不受影响。
"""

from __future__ import annotations

from datetime import date, datetime

from .eligibility import (
    EligibilityService,
    LITERATURE_AWARD,
    NEWCOMER_AWARD,
    PERFORMANCE_AWARD,
    PROGRAM_AWARD,
)
from .records import (
    AwardCategoryClass,
    AwardWinner,
    QuotaCheck,
)
from .repository import AwardRepository
from .roles import GovernanceError, PermissionDenied, Role

CATEGORY_CLASS = {
    PROGRAM_AWARD: AwardCategoryClass.WORK,
    LITERATURE_AWARD: AwardCategoryClass.WORK,
    PERFORMANCE_AWARD: AwardCategoryClass.PERSONAL,
    NEWCOMER_AWARD: AwardCategoryClass.PERSONAL,
}


def default_quotas() -> dict[str, int]:
    return {
        PROGRAM_AWARD: 8,
        LITERATURE_AWARD: 8,
        PERFORMANCE_AWARD: 6,
        NEWCOMER_AWARD: 6,
    }


class AwardService:
    def __init__(
        self,
        repo: AwardRepository,
        eligibility: EligibilityService,
        quotas: dict[str, int] | None = None,
    ) -> None:
        self.repo = repo
        self.eligibility = eligibility
        self.quotas = quotas or default_quotas()
        self.confirmed_winners: list[AwardWinner] = []
        # 确认批次（时间、当批获奖名单），证明被申诉锁定的候选未随无关奖项提前确认。
        self.confirmation_log: list[tuple[datetime, list[AwardWinner]]] = []

    # ------------------------------------------------------------ 公示前校验

    def check_before_publication(
        self,
        winners: list[AwardWinner],
        newcomer_deadline: date,
        actor_role: Role,
        checked_at: datetime,
    ) -> dict[AwardCategoryClass, QuotaCheck]:
        """作品类与个人类各自独立校验。"""
        if actor_role not in (Role.ORGANIZING_COMMITTEE, Role.SUPERVISOR):
            raise PermissionDenied("只有组委会可在公示前校验名额")
        for winner in winners:
            if winner.category not in CATEGORY_CLASS:
                raise GovernanceError(f"未知奖项类别：{winner.category}")
        result: dict[AwardCategoryClass, QuotaCheck] = {}
        for category_class in (AwardCategoryClass.WORK, AwardCategoryClass.PERSONAL):
            scoped = [w for w in winners if CATEGORY_CLASS[w.category] is category_class]
            violations: list[str] = []
            violations.extend(self._quota_violations(scoped))
            violations.extend(self._combination_violations(scoped, category_class))
            violations.extend(
                self._qualification_violations(scoped, newcomer_deadline, checked_at)
            )
            violations.extend(self._lock_violations(scoped))
            check = QuotaCheck(
                category_class=category_class,
                checked_at=checked_at,
                within_quota=not any("名额" in v for v in violations),
                combination_ok=not any("兼得" in v for v in violations),
                violations=tuple(violations),
                winners=tuple(scoped),
            )
            self.repo.quota_checks.append(check)
            result[category_class] = check
        return result

    def _quota_violations(self, winners: list[AwardWinner]) -> list[str]:
        violations: list[str] = []
        counts: dict[str, int] = {}
        for winner in winners:
            counts[winner.category] = counts.get(winner.category, 0) + 1
        for category, count in counts.items():
            if count > self.quotas[category]:
                violations.append(
                    f"{category}获奖{count}名，超过名额{self.quotas[category]}名"
                )
        return violations

    def _combination_violations(
        self, winners: list[AwardWinner], category_class: AwardCategoryClass
    ) -> list[str]:
        violations: list[str] = []
        if category_class is AwardCategoryClass.WORK:
            # 同一版本不得兼得节目奖与文学奖。
            keys = {(w.version_id, w.category) for w in winners}
            for version_id in {w.version_id for w in winners}:
                if (version_id, PROGRAM_AWARD) in keys and (
                    version_id,
                    LITERATURE_AWARD,
                ) in keys:
                    violations.append(
                        f"版本{version_id}兼得节目奖与文学奖，违反兼得规则"
                    )
        else:
            # 同一人不得兼得表演奖与新人奖。
            keys = {(w.person_id, w.category) for w in winners}
            for person_id in {w.person_id for w in winners if w.person_id}:
                if (person_id, PERFORMANCE_AWARD) in keys and (
                    person_id,
                    NEWCOMER_AWARD,
                ) in keys:
                    violations.append(
                        f"人员{person_id}兼得表演奖与新人奖，违反兼得规则"
                    )
        return violations

    def _qualification_violations(
        self,
        winners: list[AwardWinner],
        newcomer_deadline: date,
        checked_at: datetime,
    ) -> list[str]:
        violations: list[str] = []
        for winner in winners:
            result = self.eligibility.qualification(
                winner.version_id,
                winner.category,
                newcomer_deadline,
                winner.person_id,
            )
            if result["status"] != "通过":
                violations.append(
                    f"{winner.category}候选{winner.version_id}"
                    f"/{winner.person_id or '作品'}资格结论为{result['status']}"
                )
            standing = self.repo.standings.get(
                (winner.version_id, winner.category, winner.person_id)
            )
            if standing is not None and not standing.in_scoring:
                violations.append(
                    f"{winner.category}候选{winner.version_id}/"
                    f"{winner.person_id or '作品'}未处于计分状态（{standing.note or '资格取消'}）"
                )
        return violations

    def _lock_violations(self, winners: list[AwardWinner]) -> list[str]:
        violations: list[str] = []
        for winner in winners:
            blocked = self.repo.blocked_categories(winner.version_id)
            if winner.category in blocked:
                violations.append(
                    f"{winner.category}候选{winner.version_id}被受理中申诉锁定，暂停确认"
                )
        return violations

    # ------------------------------------------------------------ 获奖确认

    def confirm_winners(
        self,
        winners: list[AwardWinner],
        notice_id: str,
        confirmed_at: datetime,
        actor_role: Role,
    ) -> list[AwardWinner]:
        """公示期满、无锁定障碍时确认获奖；无关候选不受其他申诉影响。"""
        if actor_role is not Role.ORGANIZING_COMMITTEE:
            raise PermissionDenied("获奖结果由组委会确认")
        notice = self.repo.notices.get(notice_id)
        if notice is None:
            raise GovernanceError("公示不存在")
        if not notice.confirmed and confirmed_at < notice.adjusted_end:
            raise GovernanceError(
                f"公示期未满（顺延截止{notice.adjusted_end.isoformat()}），不得确认"
            )
        for winner in winners:
            if self.repo.is_category_blocked(winner.version_id, winner.category):
                raise GovernanceError(
                    f"{winner.category}候选{winner.version_id}仍被申诉锁定"
                )
        notice.confirmed = True
        notice.confirmed_at = confirmed_at
        self.confirmed_winners.extend(winners)
        self.confirmation_log.append((confirmed_at, list(winners)))
        return winners
