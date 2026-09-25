"""曲艺评奖领域模型。

所有事实都带来源、登记时间与登记人；只允许追加事实，不允许覆盖删除，
因此作品文本、现场版本、轮次结论等历史可以完整追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any
import hashlib
import json


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """系统参与角色。秘书处与评分身份隔离是关键边界。"""

    SECRETARIAT = "秘书处"
    AUTHORIZED_OFFICER = "授权人员"
    JUDGE = "评委"
    REVIEWER = "复核人员"
    ORGANIZER = "组委会"


class AwardCategory(str, Enum):
    """奖项类别。作品类与个人类分别校验名额与兼得规则。"""

    PROGRAM = "节目奖"
    LITERATURE = "文学奖"
    PERFORMANCE = "表演奖"
    NEWCOMER = "新人奖"

    @property
    def is_work_award(self) -> bool:
        return self in (AwardCategory.PROGRAM, AwardCategory.LITERATURE)

    @property
    def is_person_award(self) -> bool:
        return self in (AwardCategory.PERFORMANCE, AwardCategory.NEWCOMER)


class RoundKind(str, Enum):
    PRELIMINARY = "初评"
    REGIONAL = "分赛区"
    FINAL = "现场终评"


class WorkRelation(str, Enum):
    """人员与作品的演职关系。"""

    AUTHOR = "作者"
    PERFORMER = "演员"
    RIGHTS_HOLDER = "权利人"
    OTHER_CREW = "其他演职人员"


class CrewStatus(str, Enum):
    """演员在某一节目版本中的状态。"""

    ACTIVE = "参演"
    WITHDRAWN = "退出"


class EventType(str, Enum):
    """现场终评事件。"""

    CAST_CHANGE = "换角"
    SUSPENSION = "中止"
    REPERFORMANCE = "重新表演"


class AppealStatus(str, Enum):
    OPEN = "受理中"
    UPHELD = "裁定变更"
    REJECTED = "驳回"
    WITHDRAWN = "撤回"


class AppealTarget(str, Enum):
    QUALIFICATION = "资格"
    TEXT = "文本"
    SCORE = "评分"


class CandidacyStatus(str, Enum):
    PENDING = "待确认"
    CONFIRMED = "已确认"
    REVOKED = "撤销"


class PublicityStatus(str, Enum):
    NOT_STARTED = "未开始"
    ONGOING = "公示中"
    ENDED = "已结束"


class RecusalReason(str, Enum):
    """回避原因，用于最终溯源说明。"""

    WORK_RELATION = "与作品存在创作或演职关系"
    PERSON_RELATION = "与候选人员存在亲属、师生等关系"
    ORG_RELATION = "本人或近亲属隶属于候选报送单位"
    DECLARED = "评委主动声明的其他利害关系"


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now().astimezone()


def fingerprint(payload: Any) -> str:
    """对任意可序列化事实生成与键顺序无关的 SHA-256 摘要。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class Actor:
    """系统操作者（工作人员或评委），按角色授权。"""

    actor_id: str
    name: str
    role: Role


# ---------------------------------------------------------------------------
# 作品档案
# ---------------------------------------------------------------------------


@dataclass
class TextVersion:
    """文本沿革中的一个版本。改名、实质性修改都追加新版本，旧版本保留。"""

    version_id: str
    title: str
    text_hash: str
    note: str
    recorded_at: datetime
    recorded_by: str
    first_performed_at: date | None = None
    originality_declared: bool = False
    originality_note: str = ""


@dataclass
class WorkRecord:
    """作品（节目）档案。曲种、版本谱系、作者与权利人均在此保存。"""

    work_id: str
    genre: str
    current_title: str
    versions: list[TextVersion] = field(default_factory=list)
    author_ids: list[str] = field(default_factory=list)
    rights_holder_ids: list[str] = field(default_factory=list)
    submitting_org_id: str | None = None
    # 重复作品关联只能由授权人员确认；关联组内作品被视为同一谱系。
    duplicate_group_id: str | None = None
    duplicate_links: list[dict[str, Any]] = field(default_factory=list)
    merged_into: str | None = None

    def all_text_hashes(self) -> set[str]:
        return {v.text_hash for v in self.versions}

    def get_version(self, version_id: str) -> TextVersion | None:
        return next((v for v in self.versions if v.version_id == version_id), None)


@dataclass
class DuplicateSuspect:
    """疑似重复线索。名称相似度只能生成线索，不能直接合并。"""

    suspect_id: str
    work_id_a: str
    work_id_b: str
    similarity_score: float
    basis: str
    created_at: datetime
    resolved: bool = False
    resolution: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 人员档案
# ---------------------------------------------------------------------------


@dataclass
class AgeProof:
    proof_id: str
    birth_date: date
    doc_ref: str
    recorded_at: datetime
    recorded_by: str


@dataclass
class OrgMembership:
    """所属单位关系；一名演员可随不同院团参演，故允许多段关系。"""

    org_id: str
    org_name: str
    started_from: date
    ended_at: date | None = None
    note: str = ""


@dataclass
class CastAppearance:
    """演职关系：人员在某作品某版本中的具体参与事实。"""

    work_id: str
    version_id: str
    relation: WorkRelation
    org_id: str | None
    status: CrewStatus = CrewStatus.ACTIVE
    detail: str = ""


@dataclass
class RegionalHistory:
    region: str
    round_id: str
    year: int
    advanced: bool


@dataclass
class PriorAward:
    """历史获奖记录，用于表演奖/新人奖等限制规则。"""

    award: str
    category: AwardCategory
    year: int
    detail: str = ""


@dataclass
class PersonRecord:
    person_id: str
    name: str
    age_proof: AgeProof | None = None
    memberships: list[OrgMembership] = field(default_factory=list)
    appearances: list[CastAppearance] = field(default_factory=list)
    regional_histories: list[RegionalHistory] = field(default_factory=list)
    prior_awards: list[PriorAward] = field(default_factory=list)

    def org_at(self, day: date) -> str | None:
        for m in self.memberships:
            if m.started_from <= day and (m.ended_at is None or day <= m.ended_at):
                return m.org_id
        return None

    def active_orgs(self) -> set[str]:
        return {m.org_id for m in self.memberships if m.ended_at is None}


# ---------------------------------------------------------------------------
# 轮次与封存
# ---------------------------------------------------------------------------


@dataclass
class Advancement:
    """晋级候选：作品类指向作品版本，个人类指向人员在版本中的表演。"""

    candidacy_id: str
    category: AwardCategory
    work_id: str
    version_id: str
    person_id: str | None
    advanced: bool
    note: str = ""


@dataclass
class RoundSeal:
    """封存快照：材料指纹 + 晋级结论指纹，封存后不可变。"""

    sealed_at: datetime
    sealed_by: str
    materials_fingerprint: str
    advancement_fingerprint: str
    material_refs: list[str]
    advancement_summary: list[dict[str, Any]]


@dataclass
class RoundRecord:
    round_id: str
    kind: RoundKind
    name: str
    region: str | None = None
    year: int = 0
    started_at: datetime | None = None
    sealed: RoundSeal | None = None
    advancements: dict[str, Advancement] = field(default_factory=dict)
    materials: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SupplementaryEvidence:
    """后补证明：只进入资格复核，绝不回写原轮次材料。"""

    evidence_id: str
    candidacy_id: str
    round_id: str
    doc_ref: str
    fact_type: str
    content: str
    submitted_at: datetime
    submitted_by: str
    review_round_id: str | None = None
    accepted: bool | None = None
    review_note: str = ""


# ---------------------------------------------------------------------------
# 评委、回避与评分
# ---------------------------------------------------------------------------


@dataclass
class Judge:
    judge_id: str
    person_id: str
    name: str
    org_id: str | None = None
    active: bool = True
    # 评委主动申报的利害关系人（亲属、师生等）：人员 -> 关系说明
    related_person_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class Recusal:
    judge_id: str
    round_id: str
    work_id: str
    reason: RecusalReason
    detail: str
    declared_at: datetime


@dataclass
class ScoreRecord:
    """评分只认匿名评委代号与具体节目版本；秘书处看不到代号背后身份。"""

    score_id: str
    round_id: str
    work_id: str
    version_id: str
    judge_alias: str
    value: float
    submitted_at: datetime
    locked_by_appeal: bool = False
    # 重新表演/换角后旧版本评分归档于此标记，不再计入现行评分
    superseded: bool = False
    # 申诉成立后被裁定排除的评分
    excluded: bool = False


# ---------------------------------------------------------------------------
# 现场终评事件
# ---------------------------------------------------------------------------


@dataclass
class LiveEvent:
    event_id: str
    round_id: str
    work_id: str
    version_id: str
    kind: EventType
    occurred_at: datetime
    detail: str
    new_version_id: str | None = None
    effects: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 申诉
# ---------------------------------------------------------------------------


@dataclass
class Appeal:
    appeal_id: str
    target_type: AppealTarget
    # 资格锁定到候选；文本锁定到作品版本；评分锁定到具体评分证据
    candidacy_id: str | None
    work_id: str | None
    version_id: str | None
    score_ids: list[str]
    reason: str
    submitted_at: datetime
    status: AppealStatus = AppealStatus.OPEN
    resolution_note: str = ""
    resolved_at: datetime | None = None
    changed_candidacies: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 奖项候选、名额与公示
# ---------------------------------------------------------------------------


@dataclass
class Candidacy:
    candidacy_id: str
    category: AwardCategory
    work_id: str
    version_id: str
    person_id: str | None
    source_round_id: str
    status: CandidacyStatus = CandidacyStatus.PENDING
    # 资格事实在候选生成时冻结快照，后补证明经复核后另行登记
    qualification_snapshot: dict[str, Any] = field(default_factory=dict)
    qualifying_round_id: str | None = None
    lock_reasons: list[str] = field(default_factory=list)
    # 现场换角/重新表演后现行评分版本（版本谱系保留原值可追溯）
    effective_version_id: str | None = None
    # 影响该候选的现场事件与申诉痕迹
    effects: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class QuotaRule:
    """名额与兼得规则。

    - max_winners：该奖项获奖总数上限；
    - exclusive_with：同一主体不得同时获得的奖项（兼得限制）；
    - newcomer_age_limit / newcomer_exclude_categories：新人奖年龄与历史获奖限制。
    """

    category: AwardCategory
    max_winners: int
    exclusive_with: tuple[AwardCategory, ...] = ()
    newcomer_age_limit: int | None = None
    newcomer_exclude_categories: tuple[AwardCategory, ...] = ()


class ResultStatus(str, Enum):
    CONFIRMED = "已确认"
    INVALIDATED = "已撤销"


@dataclass
class AwardResult:
    candidacy_id: str
    category: AwardCategory
    work_id: str
    person_id: str | None
    confirmed_at: datetime
    rank: int
    status: ResultStatus = ResultStatus.CONFIRMED
    appeal_ids: list[str] = field(default_factory=list)
    invalidate_reason: str = ""


@dataclass
class DownTime:
    """公示期内的系统停机区间，结束时按停机时长顺延。"""

    started_at: datetime
    ended_at: datetime | None = None
    reason: str = ""


@dataclass
class Publicity:
    publicity_id: str
    started_at: datetime
    planned_deadline: datetime
    status: PublicityStatus = PublicityStatus.NOT_STARTED
    downtimes: list[DownTime] = field(default_factory=list)
    ended_at: datetime | None = None
