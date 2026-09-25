"""角色与权限错误。"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    SUBMITTING_ORG = "报送院团"
    PARTICIPANT = "参评作者与演员"
    JUDGE = "评委"
    SECRETARIAT = "秘书处"
    SUPERVISOR = "监审资格组"
    ORGANIZING_COMMITTEE = "评奖组委会"


class GovernanceError(Exception):
    """裁定服务拒绝某操作时抛出。"""


class PermissionDenied(GovernanceError):
    """角色无权执行该操作。"""


class ImmutableRecord(GovernanceError):
    """记录已封存或已锁定，禁止改写。"""


class ConflictOfInterest(GovernanceError):
    """评委与候选存在应回避关系。"""
