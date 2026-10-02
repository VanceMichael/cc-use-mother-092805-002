"""商品住房销售管理的枚举、数据类型与错误。

所有领域对象只表达"当前状态"，其历史由 ``src.housing.events`` 中的
只增事件流保存。任何人都不能改写或删除已经发生的事实，只能再追加
一条新事件（新版本、新结论、新衔接记录）来反映变化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DomainError(Exception):
    """业务规则被违反时抛出。"""


class NotFound(DomainError):
    """引用的对象不存在。"""


class Conflict(DomainError):
    """状态冲突（重复版本、楼栋已被暂停等）。"""


class AuthorizationError(DomainError):
    """操作者无权做出该动作。"""


class Role(str, Enum):
    DEVELOPER = "开发企业"
    DISTRICT = "区级管理人员"
    INSPECTOR = "检查人员"
    SURVEYOR = "测绘机构"


class SaleMode(str, Enum):
    PRESALE = "预售"
    SPOT = "现售"


class PlanStatus(str, Enum):
    PENDING = "待审"
    APPROVED = "已批准"
    REJECTED = "未批准"


class InspectionResult(str, Enum):
    ISSUE_FOUND = "发现问题"
    PASS = "通过"


class RectificationStatus(str, Enum):
    OPEN = "待整改"
    SUBMITTED = "已报送"
    APPROVED = "整改通过"
    REJECTED = "整改不通过"
    OVERDUE = "到期未整改"


class AcceptanceStatus(str, Enum):
    PENDING = "待验收"
    DONE = "已验收"


class ChangeEffect(str, Enum):
    """方案或面积变化对已签约房源的衔接方式。"""

    SUPPLEMENT = "补充协议"
    RECTIFICATION = "整改"
    DISPUTE = "争议处理"


DOC_KINDS = frozenset({"建设方案", "公共配套", "测绘成果"})


@dataclass
class Actor:
    """系统参与方。检查人员带有本人负责的检查事项。"""

    code: str
    name: str
    role: Role
    responsible_items: frozenset[str] = frozenset()


@dataclass
class DocumentVersion:
    """建设方案 / 公共配套 / 测绘成果的一个版本。

    content_hash 是版本内容指纹；内容相同则指纹相同，系统据此识别
    "重复报送"并沿用原审批结果，不再产生新的待审版本。
    """

    doc_id: str
    version: int
    kind: str
    title: str
    content_hash: str
    status: PlanStatus
    submitted_by: str
    submitted_at: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str = ""
    supersedes: int | None = None


@dataclass
class Plot:
    code: str
    name: str


@dataclass
class Building:
    code: str
    plot_code: str
    suspended: bool = False
    suspend_reason: str = ""


@dataclass
class House:
    code: str
    building_code: str
    signed: bool = False
    contract_code: str | None = None
    frozen_disclosure_code: str | None = None


@dataclass
class Disclosure:
    """销售现场公示。一个批次可以有多版公示，签约只能冻结其中一版。"""

    code: str
    batch_code: str
    doc_refs: dict[str, dict[str, Any]]  # 单据种类 -> {doc_id, version, title, content_hash}
    displayed_from: str
    displayed_until: str | None = None  # 撤换时间；None 表示仍在展示
    frozen: bool = False


@dataclass
class SalesBatch:
    code: str
    plot_code: str
    building_codes: list[str]
    mode: SaleMode
    opened: bool = False
    disclosures: list[str] = field(default_factory=list)


@dataclass
class Precondition:
    """前置条件确认记录。预售与现售各有一套，确认后批次方可开盘。"""

    batch_code: str
    item: str
    satisfied: bool
    confirmed_by: str
    confirmed_at: str


@dataclass
class Inspection:
    code: str
    building_code: str
    item: str
    result: InspectionResult
    inspector_code: str
    inspected_at: str
    finding: str = ""


@dataclass
class Rectification:
    code: str
    inspection_code: str
    building_code: str
    item: str
    status: RectificationStatus
    due_at: str | None
    developer_code: str
    submitted_at: str | None = None
    evidence: str = ""
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    conclusion: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PendingAcceptance:
    """待验收事项。整改通过只代表整改完成，验收是另一条持久记录。"""

    code: str
    building_code: str
    item: str
    source_rectification: str | None
    status: AcceptanceStatus
    created_at: str
    accepted_by: str | None = None
    accepted_at: str | None = None
    note: str = ""


@dataclass
class ChangeOrder:
    """方案或面积变更单。只对未签约房源生效；已签约部分必须走衔接。"""

    code: str
    doc_kind: str
    doc_id: str
    new_version: int
    affected_houses: list[str]
    applied_to: list[str] = field(default_factory=list)  # 实际作用到的未签约房源
    linked_houses: dict[str, ChangeEffect] = field(default_factory=dict)  # 已签约房源 -> 衔接方式
    created_at: str = ""


@dataclass
class SignedLink:
    """已签约房源与后续变化之间的衔接记录（补充协议/整改/争议处理）。"""

    code: str
    house_code: str
    contract_code: str
    effect: ChangeEffect
    detail: str
    created_by: str
    created_at: str
    change_code: str | None = None
