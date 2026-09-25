"""领域错误。所有违反业务约束的操作都抛出 ``GovernanceError`` 的子类。"""

from __future__ import annotations


class GovernanceError(Exception):
    """裁定服务业务错误基类。"""


class DuplicateError(GovernanceError):
    """标识冲突或重复登记。"""


class NotFoundError(GovernanceError):
    """引用的作品、人员、轮次等不存在。"""


class AuthorizationError(GovernanceError):
    """操作者角色无权执行该操作。"""


class SealedError(GovernanceError):
    """轮次已封存，原轮次事实不得改写。"""


class DuplicateLinkError(GovernanceError):
    """疑似重复作品未经授权人员按实质证据确认。"""


class QualificationError(GovernanceError):
    """资格事实不满足（年龄、历史获奖限制、声明缺失等）。"""


class RecusalError(GovernanceError):
    """评委存在应回避关系，或对已回避评委/作品操作评分。"""


class QuotaError(GovernanceError):
    """名额超限或违反兼得规则。"""


class AppealLockError(GovernanceError):
    """证据被申诉锁定，相关裁定暂停；无关奖项不受影响。"""


class PublicityError(GovernanceError):
    """公示状态不允许该操作（未开始、已结束等）。"""


class TimelineError(GovernanceError):
    """事件时间顺序不合法，例如停机区间不覆盖公示期。"""
