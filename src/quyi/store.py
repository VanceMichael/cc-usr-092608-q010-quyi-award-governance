"""内存数据存储与演示种子。

裁定服务以纯 Python 实现、无外部依赖；``AwardStore`` 集中持有全部领域对象，
各服务只通过存储读写，保证事实追加、可追溯。生产环境可将同一接口替换为持久化实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import (
    Actor,
    Appeal,
    Candidacy,
    CastAppearance,
    Judge,
    LiveEvent,
    OrgMembership,
    QuotaRule,
    Recusal,
    RoundRecord,
    ScoreRecord,
    SupplementaryEvidence,
    WorkRecord,
    PersonRecord,
    DuplicateSuspect,
    Publicity,
    AwardCategory,
)


@dataclass
class Org:
    org_id: str
    name: str


@dataclass
class AwardStore:
    actors: dict[str, Actor] = field(default_factory=dict)
    orgs: dict[str, Org] = field(default_factory=dict)
    works: dict[str, WorkRecord] = field(default_factory=dict)
    persons: dict[str, PersonRecord] = field(default_factory=dict)
    suspects: dict[str, DuplicateSuspect] = field(default_factory=dict)
    rounds: dict[str, RoundRecord] = field(default_factory=dict)
    supplements: dict[str, SupplementaryEvidence] = field(default_factory=dict)
    judges: dict[str, Judge] = field(default_factory=dict)
    # judge_alias -> judge_id，只有评委服务可按授权查询映射
    alias_to_judge: dict[str, str] = field(default_factory=dict)
    judge_aliases: dict[str, list[str]] = field(default_factory=dict)
    recusals: list[Recusal] = field(default_factory=list)
    scores: dict[str, ScoreRecord] = field(default_factory=dict)
    live_events: list[LiveEvent] = field(default_factory=list)
    appeals: dict[str, Appeal] = field(default_factory=dict)
    candidacies: dict[str, Candidacy] = field(default_factory=dict)
    quotas: dict[AwardCategory, QuotaRule] = field(default_factory=dict)
    results: list = field(default_factory=list)  # list[AwardResult]
    publicities: dict[str, Publicity] = field(default_factory=dict)
    # 审计日志：谁在何时对什么事实做了什么
    audit: list[dict] = field(default_factory=list)

    # -- 便捷查询 -----------------------------------------------------------

    def work(self, work_id: str) -> WorkRecord:
        from .errors import NotFoundError

        work = self.works.get(work_id)
        if work is None:
            raise NotFoundError(f"作品不存在：{work_id}")
        return work

    def person(self, person_id: str) -> PersonRecord:
        from .errors import NotFoundError

        person = self.persons.get(person_id)
        if person is None:
            raise NotFoundError(f"人员不存在：{person_id}")
        return person

    def round(self, round_id: str) -> RoundRecord:
        from .errors import NotFoundError

        round_record = self.rounds.get(round_id)
        if round_record is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        return round_record

    def candidacy(self, candidacy_id: str) -> Candidacy:
        from .errors import NotFoundError

        candidacy = self.candidacies.get(candidacy_id)
        if candidacy is None:
            raise NotFoundError(f"候选不存在：{candidacy_id}")
        return candidacy

    def appearances_for(self, work_id: str) -> list[CastAppearance]:
        found: list[CastAppearance] = []
        for person in self.persons.values():
            found.extend(a for a in person.appearances if a.work_id == work_id)
        return found

    def log(self, actor_id: str, action: str, target: str, detail: str = "") -> None:
        from .models import utc_now

        self.audit.append(
            {
                "at": utc_now(),
                "actor_id": actor_id,
                "action": action,
                "target": target,
                "detail": detail,
            }
        )


# ---------------------------------------------------------------------------
# 演示数据：434 个节目规模仅以数量字段体现，示例对象全部为虚构身份
# ---------------------------------------------------------------------------


def seed_store() -> AwardStore:
    """构造覆盖主要关系的虚构演示数据，不含任何真实个人信息。"""
    from .models import (
        AgeProof,
        TextVersion,
        WorkRelation,
        CrewStatus,
        Role,
        RoundKind,
        RecusalReason,
        utc_now,
    )

    store = AwardStore()

    store.actors["sec-zhang"] = Actor("sec-zhang", "张某（秘书处）", Role.SECRETARIAT)
    store.actors["org-chen"] = Actor("org-chen", "陈某（组委会，虚构）", Role.ORGANIZER)
    store.actors["officer-li"] = Actor("officer-li", "李某（组委会授权人员）", Role.AUTHORIZED_OFFICER)
    store.actors["officer-wang"] = Actor("officer-wang", "王某（组委会授权人员）", Role.AUTHORIZED_OFFICER)
    store.actors["reviewer-zhao"] = Actor("reviewer-zhao", "赵某（资格复核人员）", Role.REVIEWER)

    store.orgs["org-a"] = Org("org-a", "甲市曲艺团（虚构）")
    store.orgs["org-b"] = Org("org-b", "乙县说唱团（虚构）")

    # 同一文本改名后再次报送：W-001 与 W-002 文本指纹相同、标题不同
    v1 = TextVersion(
        version_id="W-001-V1",
        title="湖畔新声",
        text_hash="hash-text-hu-an-0001",
        note="初版文本",
        recorded_at=utc_now(),
        recorded_by="sec-zhang",
        first_performed_at=date(2024, 5, 1),
        originality_declared=True,
        originality_note="作者声明原创",
    )
    w1 = WorkRecord(
        work_id="W-001",
        genre="相声",
        current_title="湖畔新声",
        versions=[v1],
        author_ids=["P-001"],
        rights_holder_ids=["P-001"],
        submitting_org_id="org-a",
    )
    v2 = TextVersion(
        version_id="W-002-V1",
        title="湖畔新声（修订报送版）",
        text_hash="hash-text-hu-an-0001",
        note="改名后以另一院团名义再次报送",
        recorded_at=utc_now(),
        recorded_by="sec-zhang",
    )
    w2 = WorkRecord(
        work_id="W-002",
        genre="相声",
        current_title="湖畔新声（修订报送版）",
        versions=[v2],
        author_ids=["P-001"],
        rights_holder_ids=["P-001"],
        submitting_org_id="org-b",
    )
    store.works["W-001"] = w1
    store.works["W-002"] = w2

    # 人员：P-002 随不同院团参演；P-003 为新人奖适龄演员
    store.persons["P-001"] = PersonRecord(
        person_id="P-001",
        name="钱某（虚构作者）",
        age_proof=AgeProof("AP-1", date(1980, 3, 3), "虚构证件编号A", utc_now(), "sec-zhang"),
        memberships=[OrgMembership("org-a", "甲市曲艺团（虚构）", date(2010, 1, 1))],
        appearances=[CastAppearance("W-001", "W-001-V1", WorkRelation.AUTHOR, "org-a")],
    )
    store.persons["P-002"] = PersonRecord(
        person_id="P-002",
        name="孙某（虚构演员）",
        age_proof=AgeProof("AP-2", date(1985, 7, 7), "虚构证件编号B", utc_now(), "sec-zhang"),
        memberships=[
            OrgMembership("org-a", "甲市曲艺团（虚构）", date(2012, 1, 1), date(2023, 12, 31)),
            OrgMembership("org-b", "乙县说唱团（虚构）", date(2024, 1, 1)),
        ],
        appearances=[
            CastAppearance("W-001", "W-001-V1", WorkRelation.PERFORMER, "org-a"),
            CastAppearance("W-002", "W-002-V1", WorkRelation.PERFORMER, "org-b"),
        ],
    )
    store.persons["P-003"] = PersonRecord(
        person_id="P-003",
        name="周某（虚构青年演员）",
        age_proof=AgeProof("AP-3", date(2002, 9, 1), "虚构证件编号C", utc_now(), "sec-zhang"),
        memberships=[OrgMembership("org-a", "甲市曲艺团（虚构）", date(2021, 6, 1))],
        appearances=[CastAppearance("W-001", "W-001-V1", WorkRelation.PERFORMER, "org-a", detail="捧哏")],
    )

    return store
