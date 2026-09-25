"""曲艺评奖档案的记录模型。

记录只描述已经登记的事实；裁定逻辑在各服务中。除服务显式允许的状态
迁移外，封存与锁定后的记录不可改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import date, datetime


# ---------------------------------------------------------------- 基础档案


@dataclass
class Organization:
    """院团/单位。"""

    id: str
    name: str


@dataclass
class AgeEvidence:
    """年龄证明：证据编号、所证出生日期、核验日期。"""

    evidence_id: str
    birth_date: date
    verified_at: date
    note: str = ""


@dataclass
class Affiliation:
    """所属单位关系，按生效区间记录；调动只追加新区间。"""

    org_id: str
    valid_from: date
    valid_to: date | None = None


@dataclass
class PastAward:
    """历史获奖及其对本届奖项构成的限制。"""

    award_name: str
    year: int
    restricted_categories: tuple[str, ...]


@dataclass
class Person:
    """参评人员档案。"""

    id: str
    name: str
    affiliations: list[Affiliation] = field(default_factory=list)
    age_evidence: AgeEvidence | None = None
    past_awards: list[PastAward] = field(default_factory=list)
    # 与评委的声明关系（亲属、师承等），值为关系描述
    declared_relations: dict[str, str] = field(default_factory=dict)

    def org_on(self, day: date) -> str | None:
        for item in self.affiliations:
            if item.valid_from <= day and (item.valid_to is None or day <= item.valid_to):
                return item.org_id
        return None


@dataclass
class OriginalityDeclaration:
    """原创声明：声明人、声明时间、证据编号。"""

    declared_by: str
    declared_at: date
    evidence_id: str
    claim: str  # 原创 / 改编并已获授权 等


@dataclass
class PremiereFact:
    """首演事实：时间、地点或场合、证据编号。"""

    premiered_at: date
    venue: str
    evidence_id: str


class WorkKind(str, Enum):
    ORIGINAL = "原创"
    ADAPTED = "改编"


@dataclass
class Work:
    """作品线：同一文本沿革下的全部版本归在同一作品。"""

    id: str
    genre: str  # 曲种
    title: str  # 作品线当前题名
    kind: WorkKind
    authors: tuple[str, ...]  # 作者人员编号
    rights_holders: tuple[str, ...]  # 权利人
    originality: OriginalityDeclaration | None = None
    premiere: PremiereFact | None = None
    # 文本沿革：来源作品编号及关系说明（改编自、取材于）
    lineage: list[tuple[str, str]] = field(default_factory=list)
    version_ids: list[str] = field(default_factory=list)


@dataclass
class WorkVersion:
    """作品版本：每次报送（含改名再报送）产生一个版本。"""

    id: str
    work_id: str
    title: str
    text_fingerprint: str
    submitting_org_id: str
    submitted_at: date
    parent_version_id: str | None = None
    note: str = ""


class PerformanceRole(str, Enum):
    AUTHOR = "创作"
    PERFORMER = "表演"
    ACCOMPANIST = "伴奏"
    COMPOSER = "编曲"


@dataclass
class Participation:
    """演职关系：人员在某版本中以某身份随某院团参演。"""

    person_id: str
    version_id: str
    role: PerformanceRole
    org_id: str  # 参演院团（可与人员所属单位不同）


@dataclass
class RegionalExperience:
    """赛区经历：人员随版本在分赛区的结果。"""

    person_id: str
    version_id: str
    region_id: str
    result: str  # 晋级 / 淘汰


# ---------------------------------------------------------------- 重复关联


class DuplicateStatus(str, Enum):
    SUSPECTED = "疑似"
    CONFIRMED = "已确认关联"
    REJECTED = "不构成关联"


@dataclass
class DuplicateClue:
    """疑似重复线索。确认/驳回只能由授权人员作出。"""

    id: str
    version_id_a: str
    version_id_b: str
    reason: str  # 指纹相同、名称相似等
    status: DuplicateStatus = DuplicateStatus.SUSPECTED
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None


# ---------------------------------------------------------------- 轮次封存


class RoundType(str, Enum):
    PRELIMINARY = "初评"
    REGIONAL = "分赛区"


@dataclass(frozen=True)
class Advancement:
    """一条晋级结论：版本/人员、奖项类别、是否晋级。"""

    version_id: str
    category: str  # 节目奖/文学奖/表演奖/新人奖
    person_id: str | None
    advanced: bool


@dataclass(frozen=True)
class RoundSeal:
    """轮次封存：材料指纹集 + 晋级结论，一经创建不可变。"""

    id: str
    round_type: RoundType
    round_name: str
    sealed_at: datetime
    sealed_by: str
    material_fingerprints: dict[str, str]
    advancements: tuple[Advancement, ...]


# ---------------------------------------------------------------- 资格复核


class ReviewVerdict(str, Enum):
    PENDING = "待裁"
    PASSED = "复核通过"
    REJECTED = "复核不通过"
    UPHELD = "维持原结论"


@dataclass
class SuppliedEvidence:
    """后补证明。"""

    evidence_id: str
    received_at: date
    content: str


@dataclass
class ReviewCase:
    """资格复核案件：后补证明只进入这里，不改写原轮次封存。"""

    id: str
    target_version_id: str
    categories: tuple[str, ...]
    person_id: str | None
    evidence: list[SuppliedEvidence] = field(default_factory=list)
    seal_refs: list[str] = field(default_factory=list)  # 引用的封存记录
    opened_at: datetime | None = None
    verdict: ReviewVerdict = ReviewVerdict.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    rationale: str | None = None
    # 复核通过后形成的资格事实调整（供追溯展示，不回写封存）
    effect: str | None = None


# ---------------------------------------------------------------- 回避与评分


class ConflictType(str, Enum):
    WORK = "作品关系"
    PERSON = "人员关系"
    ORG = "单位关系"


@dataclass(frozen=True)
class JudgeConflict:
    """评委对某候选的回避记录及依据。"""

    judge_id: str
    candidate_version_id: str
    category: str
    person_id: str | None
    conflict_type: ConflictType
    basis: str


@dataclass
class Judge:
    """评委：姓名与其在册单位。"""

    id: str
    name: str
    org_id: str | None = None
    # 评委本人参与创作/表演的作品版本
    worked_version_ids: set[str] = field(default_factory=set)


@dataclass
class BlindScore:
    """匿名评分。秘书处只见匿名编号，映射封闭后由监审揭示。"""

    code: str
    judge_id: str
    round_name: str
    value: float
    basis: str
    submitted_at: datetime


@dataclass
class BlindMapping:
    """匿名编号到候选的映射；揭示前秘书处不可查。"""

    code: str
    version_id: str
    person_id: str | None
    category: str
    revealed: bool = False
    revealed_by: str | None = None
    revealed_at: datetime | None = None


# ---------------------------------------------------------------- 现场终评


class FinalsEventKind(str, Enum):
    CAST_CHANGE = "换角"
    SUSPENSION = "中止"
    RE_PERFORMANCE = "重新表演"


@dataclass
class FinalsEvent:
    """现场终评事件：只追加，不改写历史。"""

    id: str
    session_id: str
    version_id: str
    kind: FinalsEventKind
    occurred_at: datetime
    attempt: int  # 第几次表演尝试
    detail: str
    # 换角：out 被替演员，in 替演人员
    person_out: str | None = None
    person_in: str | None = None
    scored: bool = True  # 该尝试是否计分（中止不计分）


@dataclass
class CandidateStanding:
    """候选在终评中的资格与计分状态，随现场事件更新。"""

    version_id: str
    category: str
    person_id: str | None
    eligible: bool = True
    scoring_attempt: int = 1
    suspension_pending: bool = False  # 中止待决期间不计分，但不抹掉资格
    note: str | None = None
    # 状态变迁留痕
    history: list[str] = field(default_factory=list)

    @property
    def in_scoring(self) -> bool:
        """当前是否进入计分：资格有效且未处于中止待决。"""
        return self.eligible and not self.suspension_pending


# ---------------------------------------------------------------- 名额与公示


class AwardCategoryClass(str, Enum):
    WORK = "作品类"
    PERSONAL = "个人类"


@dataclass(frozen=True)
class AwardWinner:
    category: str
    version_id: str
    person_id: str | None


@dataclass
class QuotaCheck:
    """公示前的名额与兼得校验结论。"""

    category_class: AwardCategoryClass
    checked_at: datetime
    within_quota: bool
    combination_ok: bool
    violations: tuple[str, ...]
    winners: tuple[AwardWinner, ...]

    @property
    def passed(self) -> bool:
        return self.within_quota and self.combination_ok


@dataclass
class Downtime:
    """系统停机窗口。"""

    start: datetime
    end: datetime


@dataclass
class PublicNotice:
    """公示：截止时间可因停机顺延，剩余期限不得缩短。"""

    id: str
    started_at: datetime
    original_end: datetime
    adjusted_end: datetime
    confirmed: bool = False
    confirmed_at: datetime | None = None
    extensions: list[tuple[Downtime, float]] = field(default_factory=list)  # 停机与顺延秒数


# ---------------------------------------------------------------- 申诉


class AppealTarget(str, Enum):
    QUALIFICATION = "资格"
    TEXT = "文本"
    SCORE = "评分"


class AppealStatus(str, Enum):
    OPEN = "受理中"
    UPHELD = "申诉成立"
    REJECTED = "申诉驳回"


@dataclass
class Appeal:
    """申诉：锁定被质疑证据；无关奖项照常确认。"""

    id: str
    target: AppealTarget
    target_ref: str  # 被质疑的资格事实/文本版本/评分编号
    version_id: str
    categories: tuple[str, ...]  # 受影响奖项范围
    filed_at: datetime
    status: AppealStatus = AppealStatus.OPEN
    locked_evidence: tuple[str, ...] = ()
    decision: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    change_summary: str | None = None  # 成立时改变了什么裁定
