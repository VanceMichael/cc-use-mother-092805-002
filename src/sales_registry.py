"""商品住房销售管理后端记录。

以追加式台账保存地块、楼栋、房屋、建设方案、公共配套、测绘成果、
检查整改、销售批次与合同附件之间的版本关系，并保证：

- 方案与测绘按内容摘要识别，重复报送沿用原审批结果，内容变化形成新的待审版本；
- 销售前公示在发布时冻结，合同签约时记录所依据的公示摘要，房屋、公示与合同可互相核对；
- 预售与现售分别校验各自的前置条件；
- 检查人员只能确认本人负责事项，整改结论只能由住房管理部门批准，开发企业不能自批；
- 方案或面积变化只作用于尚未签约的房源，已签约部分通过补充协议、整改或争议处理衔接；
- 楼栋资料冲突时只暂停该栋关联房源，不无故阻断其他楼栋；
- 到期整改与待验收事项随台账持久保存；
- 管理人员可按历史日期还原一套房屋的公示、检查结论、签约依据以及之后发生的每次变化。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any


class Role(str, Enum):
    """参与方角色，是职责分离的判断依据。"""

    REGULATOR = "regulator"  # 住房管理部门
    DEVELOPER = "developer"  # 房地产开发企业
    INSPECTOR = "inspector"  # 项目检查人员
    SURVEYOR = "surveyor"  # 测绘机构


class SaleMode(str, Enum):
    """销售方式，预售与现售各有前置条件。"""

    PRESALE = "presale"  # 预售
    EXISTING = "existing"  # 现售


class SurveyKind(str, Enum):
    """测绘成果类型。"""

    PREDICTED = "predicted"  # 预测绘
    FINAL = "final"  # 实测


class InspectionKind(str, Enum):
    """检查类型，验收结论是现售的前置条件。"""

    ROUTINE = "routine"  # 日常检查
    ACCEPTANCE = "acceptance"  # 验收


class RecordError(Exception):
    """台账记录的基础错误。"""


class UnknownEntityError(RecordError):
    """引用了不存在的对象。"""


class PreconditionError(RecordError):
    """前置条件未满足。"""


class DutyError(RecordError):
    """越权操作，违反职责分离。"""


class FrozenError(RecordError):
    """试图改动已冻结的签约依据。"""


class ConflictError(RecordError):
    """资料冲突或状态矛盾。"""


def content_fingerprint(value: Any) -> str:
    """生成与键顺序无关的内容摘要，用于识别报送内容是否变化。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 台账事件与持久化
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """台账中的一条记录，recorded_on 是业务日期，作为历史还原的基准。"""

    seq: int
    recorded_on: date
    kind: str
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "recorded_on": self.recorded_on.isoformat(),
            "kind": self.kind,
            "data": self.data,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Event:
        if not isinstance(raw, dict) or set(raw) != {"seq", "recorded_on", "kind", "data"}:
            raise RecordError("台账记录结构不完整")
        if not isinstance(raw["seq"], int) or raw["seq"] < 1:
            raise RecordError("台账序号无效")
        if not isinstance(raw["kind"], str) or not raw["kind"]:
            raise RecordError("台账类型无效")
        if not isinstance(raw["data"], dict):
            raise RecordError("台账内容无效")
        return cls(
            seq=raw["seq"],
            recorded_on=date.fromisoformat(raw["recorded_on"]),
            kind=raw["kind"],
            data=raw["data"],
        )


class JsonLinesStore:
    """把台账事件逐行追加到 JSONL 文件，保证整改与验收事项持久保存。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_json(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    def load(self) -> list[Event]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(Event.from_json(json.loads(line)))
        events.sort(key=lambda event: event.seq)
        return events


# ---------------------------------------------------------------------------
# 台账折叠出的内部状态
# ---------------------------------------------------------------------------


@dataclass
class _Actor:
    actor_id: str
    role: str
    name: str


@dataclass
class _Parcel:
    parcel_id: str
    name: str


@dataclass
class _Building:
    building_id: str
    parcel_id: str
    name: str
    suspended: bool = False
    suspend_reason: str | None = None


@dataclass
class _Unit:
    unit_id: str
    building_id: str
    room_no: str
    suspended: bool = False
    suspend_reason: str | None = None
    plan_ref: list | None = None
    survey_ref: list | None = None


@dataclass
class _PlanVersion:
    plan_id: str
    version_no: int
    parcel_id: str
    content_hash: str
    content: dict[str, Any]
    status: str  # pending / approved / rejected
    reused_from: list | None
    submitted_on: date
    submitted_by: str
    decided_on: date | None
    decided_by: str | None
    note: str
    seq: int


@dataclass
class _SurveyVersion:
    survey_id: str
    version_no: int
    building_id: str
    kind: str
    content_hash: str
    areas: dict[str, float]
    status: str
    reused_from: list | None
    submitted_on: date
    submitted_by: str
    decided_on: date | None
    decided_by: str | None
    note: str
    seq: int


@dataclass
class _Batch:
    batch_id: str
    parcel_id: str
    building_ids: list[str]
    mode: str
    opened_on: date
    opened_by: str
    status: str = "open"


@dataclass
class _Disclosure:
    disclosure_id: str
    batch_id: str
    plan_id: str
    plan_version: int
    surveys: dict[str, list]
    content_hash: str
    content: dict[str, Any]
    published_on: date
    published_by: str
    seq: int


@dataclass
class _Contract:
    contract_id: str
    unit_id: str
    batch_id: str
    buyer_ref: str
    disclosure_id: str
    disclosure_hash: str
    plan_id: str
    plan_version: int
    surveys: dict[str, list]
    attachments: list[dict[str, str]]
    agreements: list[str]
    signed_on: date
    signed_by: str
    status: str = "active"


@dataclass
class _Agreement:
    agreement_id: str
    contract_id: str
    content_hash: str
    attachments: list[dict[str, str]]
    signed_on: date
    signed_by: str


@dataclass
class _Dispute:
    dispute_id: str
    contract_id: str
    reason: str
    opened_on: date
    opened_by: str
    status: str = "open"
    outcome: str = ""
    resolved_on: date | None = None


@dataclass
class _InspectionItem:
    key: str
    responsible: str
    confirmed: bool = False
    passed: bool = False
    conclusion: str = ""
    confirmed_by: str | None = None
    confirmed_on: date | None = None
    rectification_deadline: date | None = None
    evidence_hash: str | None = None
    rectification_submitted_on: date | None = None
    rectification_closed: bool = False
    rectification_approved: bool = False
    rectification_closed_by: str | None = None
    rectification_closed_on: date | None = None

    @property
    def status(self) -> str:
        if not self.confirmed:
            return "open"
        if self.passed:
            return "passed"
        if self.rectification_closed and self.rectification_approved:
            return "rectified"
        return "rectifying"


@dataclass
class _Inspection:
    inspection_id: str
    building_id: str
    kind: str
    opened_on: date
    opened_by: str
    items: dict[str, _InspectionItem] = field(default_factory=dict)


@dataclass
class _State:
    actors: dict[str, _Actor] = field(default_factory=dict)
    parcels: dict[str, _Parcel] = field(default_factory=dict)
    buildings: dict[str, _Building] = field(default_factory=dict)
    units: dict[str, _Unit] = field(default_factory=dict)
    plans: dict[tuple[str, int], _PlanVersion] = field(default_factory=dict)
    surveys: dict[tuple[str, int], _SurveyVersion] = field(default_factory=dict)
    batches: dict[str, _Batch] = field(default_factory=dict)
    disclosures: dict[str, _Disclosure] = field(default_factory=dict)
    contracts: dict[str, _Contract] = field(default_factory=dict)
    contract_by_unit: dict[str, str] = field(default_factory=dict)
    agreements: dict[str, _Agreement] = field(default_factory=dict)
    disputes: dict[str, _Dispute] = field(default_factory=dict)
    inspections: dict[str, _Inspection] = field(default_factory=dict)


def _apply(state: _State, event: Event) -> None:
    """把一条台账记录折叠进状态，所有事件只追加不修改。"""
    data = event.data
    kind = event.kind
    on = event.recorded_on
    if kind == "actor_registered":
        state.actors[data["actor_id"]] = _Actor(data["actor_id"], data["role"], data["name"])
    elif kind == "parcel_registered":
        state.parcels[data["parcel_id"]] = _Parcel(data["parcel_id"], data["name"])
    elif kind == "building_registered":
        state.buildings[data["building_id"]] = _Building(
            data["building_id"], data["parcel_id"], data["name"]
        )
    elif kind == "unit_registered":
        state.units[data["unit_id"]] = _Unit(data["unit_id"], data["building_id"], data["room_no"])
    elif kind == "plan_submitted":
        approved = data["status"] == "approved"
        state.plans[(data["plan_id"], data["version_no"])] = _PlanVersion(
            plan_id=data["plan_id"],
            version_no=data["version_no"],
            parcel_id=data["parcel_id"],
            content_hash=data["content_hash"],
            content=data["content"],
            status=data["status"],
            reused_from=data.get("reused_from"),
            submitted_on=on,
            submitted_by=data["submitted_by"],
            decided_on=on if approved else None,
            decided_by=data.get("decided_by") if approved else None,
            note=data.get("note", ""),
            seq=event.seq,
        )
    elif kind == "plan_reviewed":
        plan = state.plans[(data["plan_id"], data["version_no"])]
        plan.status = data["decision"]
        plan.decided_on = on
        plan.decided_by = data["reviewed_by"]
        plan.note = data.get("note", "")
    elif kind == "survey_submitted":
        approved = data["status"] == "approved"
        state.surveys[(data["survey_id"], data["version_no"])] = _SurveyVersion(
            survey_id=data["survey_id"],
            version_no=data["version_no"],
            building_id=data["building_id"],
            kind=data["kind"],
            content_hash=data["content_hash"],
            areas=data["areas"],
            status=data["status"],
            reused_from=data.get("reused_from"),
            submitted_on=on,
            submitted_by=data["submitted_by"],
            decided_on=on if approved else None,
            decided_by=data.get("decided_by") if approved else None,
            note=data.get("note", ""),
            seq=event.seq,
        )
    elif kind == "survey_reviewed":
        survey = state.surveys[(data["survey_id"], data["version_no"])]
        survey.status = data["decision"]
        survey.decided_on = on
        survey.decided_by = data["reviewed_by"]
        survey.note = data.get("note", "")
    elif kind == "batch_opened":
        state.batches[data["batch_id"]] = _Batch(
            batch_id=data["batch_id"],
            parcel_id=data["parcel_id"],
            building_ids=list(data["building_ids"]),
            mode=data["mode"],
            opened_on=on,
            opened_by=data["opened_by"],
        )
    elif kind == "disclosure_published":
        state.disclosures[data["disclosure_id"]] = _Disclosure(
            disclosure_id=data["disclosure_id"],
            batch_id=data["batch_id"],
            plan_id=data["plan_id"],
            plan_version=data["plan_version"],
            surveys={key: list(ref) for key, ref in data["surveys"].items()},
            content_hash=data["content_hash"],
            content=data["content"],
            published_on=on,
            published_by=data["published_by"],
            seq=event.seq,
        )
    elif kind == "contract_signed":
        state.contracts[data["contract_id"]] = _Contract(
            contract_id=data["contract_id"],
            unit_id=data["unit_id"],
            batch_id=data["batch_id"],
            buyer_ref=data["buyer_ref"],
            disclosure_id=data["disclosure_id"],
            disclosure_hash=data["disclosure_hash"],
            plan_id=data["plan_id"],
            plan_version=data["plan_version"],
            surveys={key: list(ref) for key, ref in data["surveys"].items()},
            attachments=[dict(item) for item in data["attachments"]],
            agreements=[],
            signed_on=on,
            signed_by=data["signed_by"],
        )
        state.contract_by_unit[data["unit_id"]] = data["contract_id"]
    elif kind == "agreement_signed":
        state.agreements[data["agreement_id"]] = _Agreement(
            agreement_id=data["agreement_id"],
            contract_id=data["contract_id"],
            content_hash=data["content_hash"],
            attachments=[dict(item) for item in data["attachments"]],
            signed_on=on,
            signed_by=data["signed_by"],
        )
        contract = state.contracts[data["contract_id"]]
        contract.agreements.append(data["agreement_id"])
        contract.attachments.extend(dict(item) for item in data["attachments"])
    elif kind == "dispute_opened":
        state.disputes[data["dispute_id"]] = _Dispute(
            dispute_id=data["dispute_id"],
            contract_id=data["contract_id"],
            reason=data["reason"],
            opened_on=on,
            opened_by=data["opened_by"],
        )
    elif kind == "dispute_resolved":
        dispute = state.disputes[data["dispute_id"]]
        dispute.status = "resolved"
        dispute.outcome = data["outcome"]
        dispute.resolved_on = on
    elif kind == "inspection_opened":
        state.inspections[data["inspection_id"]] = _Inspection(
            inspection_id=data["inspection_id"],
            building_id=data["building_id"],
            kind=data["kind"],
            opened_on=on,
            opened_by=data["opened_by"],
            items={
                item["key"]: _InspectionItem(key=item["key"], responsible=item["responsible"])
                for item in data["items"]
            },
        )
    elif kind == "inspection_item_confirmed":
        item = state.inspections[data["inspection_id"]].items[data["item_key"]]
        item.confirmed = True
        item.passed = data["passed"]
        item.conclusion = data["conclusion"]
        item.confirmed_by = data["inspector_id"]
        item.confirmed_on = on
        deadline = data.get("rectification_deadline")
        item.rectification_deadline = date.fromisoformat(deadline) if deadline else None
    elif kind == "rectification_submitted":
        item = state.inspections[data["inspection_id"]].items[data["item_key"]]
        item.evidence_hash = data["evidence_hash"]
        item.rectification_submitted_on = on
        item.rectification_closed = False
        item.rectification_approved = False
    elif kind == "rectification_closed":
        item = state.inspections[data["inspection_id"]].items[data["item_key"]]
        item.rectification_closed = True
        item.rectification_approved = data["approved"]
        item.rectification_closed_by = data["closed_by"]
        item.rectification_closed_on = on
    elif kind == "building_suspended":
        building = state.buildings[data["building_id"]]
        building.suspended = True
        building.suspend_reason = data["reason"]
    elif kind == "building_resumed":
        building = state.buildings[data["building_id"]]
        building.suspended = False
        building.suspend_reason = None
    elif kind == "unit_suspended":
        unit = state.units[data["unit_id"]]
        unit.suspended = True
        unit.suspend_reason = data["reason"]
    elif kind == "unit_resumed":
        unit = state.units[data["unit_id"]]
        unit.suspended = False
        unit.suspend_reason = None
    elif kind == "unit_rebased":
        unit = state.units[data["unit_id"]]
        if data.get("plan_id") is not None:
            unit.plan_ref = [data["plan_id"], data["plan_version"]]
        if data.get("survey_id") is not None:
            unit.survey_ref = [data["survey_id"], data["survey_version"]]
    else:
        raise RecordError(f"未知台账类型:{kind}")


# ---------------------------------------------------------------------------
# 对外只读视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanView:
    plan_id: str
    version_no: int
    parcel_id: str
    status: str
    content_hash: str
    reused_from: tuple[str, int] | None
    decided_by: str | None


@dataclass(frozen=True)
class SurveyView:
    survey_id: str
    version_no: int
    building_id: str
    kind: str
    status: str
    content_hash: str
    reused_from: tuple[str, int] | None
    decided_by: str | None


@dataclass(frozen=True)
class DisclosureView:
    """冻结后的公示，content 即购房人当时看到的内容。"""

    disclosure_id: str
    batch_id: str
    plan_ref: tuple[str, int]
    survey_refs: dict[str, tuple[str, int]]
    content_hash: str
    content: dict[str, Any]
    published_on: date


@dataclass(frozen=True)
class ContractView:
    contract_id: str
    unit_id: str
    batch_id: str
    buyer_ref: str
    disclosure_id: str
    disclosure_hash: str
    plan_ref: tuple[str, int]
    survey_refs: dict[str, tuple[str, int]]
    attachments: tuple[dict[str, str], ...]
    agreements: tuple[str, ...]
    signed_on: date


@dataclass(frozen=True)
class InspectionItemView:
    key: str
    responsible: str
    status: str
    conclusion: str
    confirmed_by: str | None
    rectification_deadline: date | None
    evidence_hash: str | None
    rectification_approved: bool | None
    rectification_closed_by: str | None


@dataclass(frozen=True)
class InspectionView:
    inspection_id: str
    building_id: str
    kind: str
    items: tuple[InspectionItemView, ...]


@dataclass(frozen=True)
class RectificationView:
    inspection_id: str
    building_id: str
    item_key: str
    deadline: date
    evidence_hash: str | None


@dataclass(frozen=True)
class AcceptanceItemView:
    inspection_id: str
    building_id: str
    item_key: str
    responsible: str
    status: str
    deadline: date | None


@dataclass(frozen=True)
class AreaConflict:
    unit_id: str
    planned: float | None
    surveyed: float | None
    difference: float | None


@dataclass(frozen=True)
class ContractCheck:
    """房屋、公示与合同互相核对的结果。"""

    contract_id: str
    ok: bool
    problems: tuple[str, ...]
    disclosure: DisclosureView | None


@dataclass(frozen=True)
class UnitHistory:
    """按历史日期还原的一套房屋档案。"""

    unit_id: str
    as_of: date
    disclosure: DisclosureView | None
    inspections: tuple[InspectionView, ...]
    contract: ContractView | None
    subsequent_changes: tuple[Event, ...]


# ---------------------------------------------------------------------------
# 台账主体
# ---------------------------------------------------------------------------


class SalesRegistry:
    """商品住房销售管理台账，所有写入先校验再追加事件。"""

    def __init__(
        self,
        store: JsonLinesStore | None = None,
        events: Iterable[Event] = (),
    ) -> None:
        self._store = store
        self._events: list[Event] = []
        self._state = _State()
        for event in events:
            _apply(self._state, event)
            self._events.append(event)

    @classmethod
    def load(cls, path: Path | str) -> SalesRegistry:
        """从持久化文件恢复台账，之后的新记录继续追加到同一文件。"""
        store = JsonLinesStore(path)
        return cls(store=store, events=store.load())

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    # -- 内部工具 ----------------------------------------------------------

    def _record(self, kind: str, on: date, data: dict[str, Any]) -> Event:
        if self._events and on < self._events[-1].recorded_on:
            raise RecordError("业务日期不能早于台账已有记录")
        event = Event(seq=len(self._events) + 1, recorded_on=on, kind=kind, data=data)
        _apply(self._state, event)
        self._events.append(event)
        if self._store is not None:
            self._store.append(event)
        return event

    def _state_at(self, as_of: date) -> _State:
        state = _State()
        for event in self._events:
            if event.recorded_on <= as_of:
                _apply(state, event)
        return state

    def _now(self) -> date:
        return self._events[-1].recorded_on if self._events else date.today()

    def _state_and_date(self, as_of: date | None) -> tuple[_State, date]:
        if as_of is None:
            return self._state, self._now()
        return self._state_at(as_of), as_of

    def _require_actor(self, actor_id: str, role: Role | None, message: str) -> _Actor:
        actor = self._state.actors.get(actor_id)
        if actor is None:
            raise UnknownEntityError(f"参与方不存在:{actor_id}")
        if role is not None and actor.role != role.value:
            raise DutyError(message)
        return actor

    def _building(self, building_id: str) -> _Building:
        building = self._state.buildings.get(building_id)
        if building is None:
            raise UnknownEntityError(f"楼栋不存在:{building_id}")
        return building

    def _unit(self, unit_id: str) -> _Unit:
        unit = self._state.units.get(unit_id)
        if unit is None:
            raise UnknownEntityError(f"房屋不存在:{unit_id}")
        return unit

    def _building_units(self, state: _State, building_id: str) -> list[str]:
        return [uid for uid, unit in state.units.items() if unit.building_id == building_id]

    def _latest_approved_plan(self, state: _State, parcel_id: str) -> _PlanVersion | None:
        candidates = [
            plan
            for plan in state.plans.values()
            if plan.parcel_id == parcel_id and plan.status == "approved"
        ]
        return max(candidates, key=lambda plan: plan.seq, default=None)

    def _latest_approved_survey(
        self, state: _State, building_id: str, kind: SurveyKind | None = None
    ) -> _SurveyVersion | None:
        candidates = [
            survey
            for survey in state.surveys.values()
            if survey.building_id == building_id
            and survey.status == "approved"
            and (kind is None or survey.kind == kind.value)
        ]
        return max(candidates, key=lambda survey: survey.seq, default=None)

    @staticmethod
    def _overdue_items(
        state: _State, building_ids: set[str], as_of: date
    ) -> list[tuple[_Inspection, _InspectionItem]]:
        found = []
        for inspection in state.inspections.values():
            if inspection.building_id not in building_ids:
                continue
            for item in inspection.items.values():
                if (
                    item.status == "rectifying"
                    and item.rectification_deadline is not None
                    and item.rectification_deadline < as_of
                ):
                    found.append((inspection, item))
        return found

    @staticmethod
    def _acceptance_done(state: _State, building_id: str) -> bool:
        for inspection in state.inspections.values():
            if inspection.building_id != building_id or inspection.kind != InspectionKind.ACCEPTANCE.value:
                continue
            if inspection.items and all(
                item.status in ("passed", "rectified") for item in inspection.items.values()
            ):
                return True
        return False

    # -- 基础登记 ----------------------------------------------------------

    def register_actor(self, actor_id: str, role: Role | str, name: str, on: date) -> None:
        role = Role(role)
        if not actor_id.strip() or not name.strip():
            raise RecordError("参与方标识与名称不能为空")
        if actor_id in self._state.actors:
            raise ConflictError(f"参与方已存在:{actor_id}")
        self._record(
            "actor_registered", on, {"actor_id": actor_id, "role": role.value, "name": name}
        )

    def register_parcel(self, parcel_id: str, name: str, on: date) -> None:
        if not parcel_id.strip() or not name.strip():
            raise RecordError("地块标识与名称不能为空")
        if parcel_id in self._state.parcels:
            raise ConflictError(f"地块已存在:{parcel_id}")
        self._record("parcel_registered", on, {"parcel_id": parcel_id, "name": name})

    def register_building(self, building_id: str, parcel_id: str, name: str, on: date) -> None:
        if parcel_id not in self._state.parcels:
            raise UnknownEntityError(f"地块不存在:{parcel_id}")
        if building_id in self._state.buildings:
            raise ConflictError(f"楼栋已存在:{building_id}")
        self._record(
            "building_registered",
            on,
            {"building_id": building_id, "parcel_id": parcel_id, "name": name},
        )

    def register_unit(self, unit_id: str, building_id: str, room_no: str, on: date) -> None:
        self._building(building_id)
        if unit_id in self._state.units:
            raise ConflictError(f"房屋已存在:{unit_id}")
        self._record(
            "unit_registered",
            on,
            {"unit_id": unit_id, "building_id": building_id, "room_no": room_no},
        )

    # -- 建设方案与公共配套 -------------------------------------------------

    def _validate_plan_content(self, parcel_id: str, content: Any) -> None:
        if not isinstance(content, dict):
            raise RecordError("方案内容无效")
        facilities = content.get("facilities")
        if (
            not isinstance(facilities, list)
            or not facilities
            or any(not isinstance(item, dict) or not str(item.get("name", "")).strip() for item in facilities)
        ):
            raise RecordError("方案须包含公共配套")
        unit_areas = content.get("unit_areas")
        if not isinstance(unit_areas, dict) or not unit_areas:
            raise RecordError("方案须包含房屋承诺面积")
        for unit_id, area in unit_areas.items():
            unit = self._state.units.get(unit_id)
            if unit is None or self._state.buildings[unit.building_id].parcel_id != parcel_id:
                raise UnknownEntityError(f"方案包含地块外房屋:{unit_id}")
            if isinstance(area, bool) or not isinstance(area, (int, float)) or area <= 0:
                raise RecordError(f"房屋承诺面积无效:{unit_id}")

    def submit_plan(
        self, plan_id: str, parcel_id: str, content: dict[str, Any], on: date, by: str
    ) -> PlanView:
        """报送建设方案；重复报送沿用原审批结果，内容变化形成新的待审版本。"""
        self._require_actor(by, Role.DEVELOPER, "建设方案须由开发企业报送")
        if parcel_id not in self._state.parcels:
            raise UnknownEntityError(f"地块不存在:{parcel_id}")
        self._validate_plan_content(parcel_id, content)
        content_hash = content_fingerprint(content)
        series = [plan for plan in self._state.plans.values() if plan.plan_id == plan_id]
        version_no = max((plan.version_no for plan in series), default=0) + 1
        reused_from = None
        status = "pending"
        decided_by = None
        note = ""
        approved_same = [
            plan
            for plan in series
            if plan.status == "approved" and plan.content_hash == content_hash
        ]
        if approved_same:
            source = max(approved_same, key=lambda plan: plan.seq)
            reused_from = [source.plan_id, source.version_no]
            status = "approved"
            decided_by = source.decided_by
            note = f"重复报送，沿用第{source.version_no}版审批结果"
        self._record(
            "plan_submitted",
            on,
            {
                "plan_id": plan_id,
                "version_no": version_no,
                "parcel_id": parcel_id,
                "content_hash": content_hash,
                "content": content,
                "status": status,
                "reused_from": reused_from,
                "decided_by": decided_by,
                "note": note,
                "submitted_by": by,
            },
        )
        return self._plan_view(self._state.plans[(plan_id, version_no)])

    def review_plan(
        self, plan_id: str, version_no: int, approve: bool, by: str, on: date, note: str = ""
    ) -> None:
        self._require_actor(by, Role.REGULATOR, "方案审批须由住房管理部门作出")
        plan = self._state.plans.get((plan_id, version_no))
        if plan is None:
            raise UnknownEntityError(f"方案版本不存在:{plan_id}第{version_no}版")
        if plan.status != "pending":
            raise ConflictError("只有待审版本可以审批")
        self._record(
            "plan_reviewed",
            on,
            {
                "plan_id": plan_id,
                "version_no": version_no,
                "decision": "approved" if approve else "rejected",
                "note": note,
                "reviewed_by": by,
            },
        )

    # -- 测绘成果 ------------------------------------------------------------

    def submit_survey(
        self,
        survey_id: str,
        building_id: str,
        kind: SurveyKind | str,
        areas: dict[str, float],
        on: date,
        by: str,
    ) -> SurveyView:
        """报送测绘成果；与方案一样按内容摘要去重并沿用原审批结果。"""
        self._require_actor(by, Role.SURVEYOR, "测绘成果须由测绘机构报送")
        kind = SurveyKind(kind)
        self._building(building_id)
        expected = set(self._building_units(self._state, building_id))
        if not expected:
            raise PreconditionError("楼栋尚未登记房屋")
        if not isinstance(areas, dict) or set(areas) != expected:
            raise PreconditionError("测绘成果须覆盖楼栋全部房屋")
        for unit_id, area in areas.items():
            if isinstance(area, bool) or not isinstance(area, (int, float)) or area <= 0:
                raise RecordError(f"测绘面积无效:{unit_id}")
        content = {"building_id": building_id, "kind": kind.value, "areas": areas}
        content_hash = content_fingerprint(content)
        series = [survey for survey in self._state.surveys.values() if survey.survey_id == survey_id]
        version_no = max((survey.version_no for survey in series), default=0) + 1
        reused_from = None
        status = "pending"
        decided_by = None
        note = ""
        approved_same = [
            survey
            for survey in series
            if survey.status == "approved" and survey.content_hash == content_hash
        ]
        if approved_same:
            source = max(approved_same, key=lambda survey: survey.seq)
            reused_from = [source.survey_id, source.version_no]
            status = "approved"
            decided_by = source.decided_by
            note = f"重复报送，沿用第{source.version_no}版审批结果"
        self._record(
            "survey_submitted",
            on,
            {
                "survey_id": survey_id,
                "version_no": version_no,
                "building_id": building_id,
                "kind": kind.value,
                "content_hash": content_hash,
                "areas": areas,
                "status": status,
                "reused_from": reused_from,
                "decided_by": decided_by,
                "note": note,
                "submitted_by": by,
            },
        )
        return self._survey_view(self._state.surveys[(survey_id, version_no)])

    def review_survey(
        self, survey_id: str, version_no: int, approve: bool, by: str, on: date, note: str = ""
    ) -> None:
        self._require_actor(by, Role.REGULATOR, "测绘成果审批须由住房管理部门作出")
        survey = self._state.surveys.get((survey_id, version_no))
        if survey is None:
            raise UnknownEntityError(f"测绘版本不存在:{survey_id}第{version_no}版")
        if survey.status != "pending":
            raise ConflictError("只有待审版本可以审批")
        self._record(
            "survey_reviewed",
            on,
            {
                "survey_id": survey_id,
                "version_no": version_no,
                "decision": "approved" if approve else "rejected",
                "note": note,
                "reviewed_by": by,
            },
        )

    # -- 销售批次与公示 ------------------------------------------------------

    def open_batch(
        self,
        batch_id: str,
        building_ids: list[str],
        mode: SaleMode | str,
        on: date,
        by: str,
    ) -> None:
        """开放销售批次，预售与现售分别校验各自的前置条件。"""
        self._require_actor(by, Role.REGULATOR, "销售批次须由住房管理部门核准")
        mode = SaleMode(mode)
        if batch_id in self._state.batches:
            raise ConflictError(f"销售批次已存在:{batch_id}")
        if not building_ids:
            raise PreconditionError("销售批次须包含楼栋")
        buildings = [self._building(building_id) for building_id in building_ids]
        parcel_ids = {building.parcel_id for building in buildings}
        if len(parcel_ids) != 1:
            raise PreconditionError("销售批次须属于同一地块")
        parcel_id = parcel_ids.pop()
        for building in buildings:
            if building.suspended:
                raise PreconditionError(f"楼栋处于暂停状态:{building.building_id}")
        if self._latest_approved_plan(self._state, parcel_id) is None:
            raise PreconditionError("缺少已批准的建设方案")
        needed = SurveyKind.PREDICTED if mode is SaleMode.PRESALE else SurveyKind.FINAL
        label = "预测绘" if needed is SurveyKind.PREDICTED else "实测"
        for building in buildings:
            if self._latest_approved_survey(self._state, building.building_id, needed) is None:
                raise PreconditionError(f"楼栋{building.building_id}缺少已批准的{label}成果")
        overdue = self._overdue_items(
            self._state, {building.building_id for building in buildings}, on
        )
        if overdue:
            raise PreconditionError("存在到期未完成的整改事项")
        if mode is SaleMode.EXISTING:
            for building in buildings:
                if not self._acceptance_done(self._state, building.building_id):
                    raise PreconditionError(f"楼栋{building.building_id}尚未完成验收")
        self._record(
            "batch_opened",
            on,
            {
                "batch_id": batch_id,
                "parcel_id": parcel_id,
                "building_ids": list(building_ids),
                "mode": mode.value,
                "opened_by": by,
            },
        )

    def publish_disclosure(
        self,
        disclosure_id: str,
        batch_id: str,
        on: date,
        by: str,
        plan_ref: tuple[str, int] | None = None,
        survey_refs: dict[str, tuple[str, int]] | None = None,
    ) -> DisclosureView:
        """发布销售前公示并冻结内容，发布后不可修改。"""
        self._require_actor(by, Role.DEVELOPER, "公示须由开发企业发布")
        if disclosure_id in self._state.disclosures:
            raise ConflictError(f"公示编号已存在:{disclosure_id}")
        batch = self._state.batches.get(batch_id)
        if batch is None:
            raise UnknownEntityError(f"销售批次不存在:{batch_id}")
        if batch.status != "open":
            raise PreconditionError("销售批次未开放")
        if plan_ref is None:
            plan = self._latest_approved_plan(self._state, batch.parcel_id)
            if plan is None:
                raise PreconditionError("缺少已批准的建设方案")
        else:
            plan = self._state.plans.get(tuple(plan_ref))
            if plan is None or plan.parcel_id != batch.parcel_id:
                raise UnknownEntityError("方案版本不存在或不属于本地块")
            if plan.status != "approved":
                raise PreconditionError("方案版本未批准")
        surveys: dict[str, _SurveyVersion] = {}
        survey_refs = survey_refs or {}
        for building_id in batch.building_ids:
            if building_id in survey_refs:
                survey = self._state.surveys.get(tuple(survey_refs[building_id]))
                if survey is None or survey.building_id != building_id:
                    raise UnknownEntityError(f"测绘版本不存在或不属于楼栋:{building_id}")
                if survey.status != "approved":
                    raise PreconditionError(f"楼栋{building_id}的测绘版本未批准")
            else:
                needed = (
                    SurveyKind.FINAL if batch.mode == SaleMode.EXISTING.value else SurveyKind.PREDICTED
                )
                survey = self._latest_approved_survey(self._state, building_id, needed)
                if survey is None:
                    raise PreconditionError(f"楼栋{building_id}缺少已批准的测绘成果")
            surveys[building_id] = survey
        units = sorted(
            unit_id
            for unit_id, unit in self._state.units.items()
            if unit.building_id in batch.building_ids
        )
        planned_areas = plan.content.get("unit_areas", {})
        missing = [unit_id for unit_id in units if unit_id not in planned_areas]
        if missing:
            raise PreconditionError(f"方案缺少房屋承诺面积:{','.join(missing)}")
        content = {
            "batch": {
                "batch_id": batch_id,
                "mode": batch.mode,
                "buildings": sorted(batch.building_ids),
            },
            "plan": {
                "plan_id": plan.plan_id,
                "version_no": plan.version_no,
                "content": plan.content,
            },
            "surveys": {
                building_id: {
                    "survey_id": survey.survey_id,
                    "version_no": survey.version_no,
                    "kind": survey.kind,
                    "areas": survey.areas,
                }
                for building_id, survey in surveys.items()
            },
            "units": units,
        }
        content_hash = content_fingerprint(content)
        self._record(
            "disclosure_published",
            on,
            {
                "disclosure_id": disclosure_id,
                "batch_id": batch_id,
                "plan_id": plan.plan_id,
                "plan_version": plan.version_no,
                "surveys": {
                    building_id: [survey.survey_id, survey.version_no]
                    for building_id, survey in surveys.items()
                },
                "content_hash": content_hash,
                "content": content,
                "published_by": by,
            },
        )
        return self._disclosure_view(self._state.disclosures[disclosure_id])

    # -- 合同与合同附件 ------------------------------------------------------

    @staticmethod
    def _freeze_attachments(attachments: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
        frozen = []
        for attachment in attachments:
            name = attachment.get("name") if isinstance(attachment, dict) else None
            content = attachment.get("content") if isinstance(attachment, dict) else None
            if not name or content is None:
                raise RecordError("合同附件须包含名称与内容")
            frozen.append({"name": name, "content_hash": content_fingerprint(content)})
        return frozen

    def sign_contract(
        self,
        contract_id: str,
        unit_id: str,
        batch_id: str,
        buyer_ref: str,
        disclosure_id: str,
        on: date,
        by: str,
        attachments: Iterable[dict[str, Any]] = (),
    ) -> ContractView:
        """签约时把所依据的公示摘要与版本冻结进合同。"""
        self._require_actor(by, Role.DEVELOPER, "签约经办须为开发企业")
        if contract_id in self._state.contracts:
            raise ConflictError(f"合同编号已存在:{contract_id}")
        unit = self._unit(unit_id)
        building = self._state.buildings[unit.building_id]
        if not buyer_ref.strip():
            raise RecordError("购房人引用不能为空")
        if unit.suspended or building.suspended:
            raise PreconditionError("房源已暂停销售")
        if unit_id in self._state.contract_by_unit:
            raise ConflictError("房屋已签约")
        batch = self._state.batches.get(batch_id)
        if batch is None or batch.status != "open":
            raise PreconditionError("销售批次未开放")
        if unit.building_id not in batch.building_ids:
            raise PreconditionError("房屋不在销售批次范围内")
        disclosure = self._state.disclosures.get(disclosure_id)
        if disclosure is None:
            raise UnknownEntityError(f"公示不存在:{disclosure_id}")
        if disclosure.batch_id != batch_id:
            raise PreconditionError("公示与销售批次不一致")
        if unit_id not in disclosure.content.get("units", []):
            raise PreconditionError("公示未覆盖该房屋")
        frozen = self._freeze_attachments(attachments)
        self._record(
            "contract_signed",
            on,
            {
                "contract_id": contract_id,
                "unit_id": unit_id,
                "batch_id": batch_id,
                "buyer_ref": buyer_ref,
                "disclosure_id": disclosure_id,
                "disclosure_hash": disclosure.content_hash,
                "plan_id": disclosure.plan_id,
                "plan_version": disclosure.plan_version,
                "surveys": {key: list(ref) for key, ref in disclosure.surveys.items()},
                "attachments": frozen,
                "signed_by": by,
            },
        )
        return self._contract_view(self._state.contracts[contract_id])

    def sign_agreement(
        self,
        agreement_id: str,
        contract_id: str,
        content: dict[str, Any],
        on: date,
        by: str,
        attachments: Iterable[dict[str, Any]] = (),
    ) -> None:
        """已签约房源通过补充协议衔接后续变化，协议内容同样冻结摘要。"""
        self._require_actor(by, Role.DEVELOPER, "补充协议经办须为开发企业")
        if agreement_id in self._state.agreements:
            raise ConflictError(f"补充协议已存在:{agreement_id}")
        contract = self._state.contracts.get(contract_id)
        if contract is None:
            raise UnknownEntityError(f"合同不存在:{contract_id}")
        if not isinstance(content, dict) or not content:
            raise RecordError("补充协议内容不能为空")
        frozen = self._freeze_attachments(attachments)
        self._record(
            "agreement_signed",
            on,
            {
                "agreement_id": agreement_id,
                "contract_id": contract_id,
                "content_hash": content_fingerprint(content),
                "attachments": frozen,
                "signed_by": by,
            },
        )

    def open_dispute(self, dispute_id: str, contract_id: str, reason: str, on: date, by: str) -> None:
        self._require_actor(by, None, "")
        if dispute_id in self._state.disputes:
            raise ConflictError(f"争议记录已存在:{dispute_id}")
        if contract_id not in self._state.contracts:
            raise UnknownEntityError(f"合同不存在:{contract_id}")
        if not reason.strip():
            raise RecordError("争议原因不能为空")
        self._record(
            "dispute_opened",
            on,
            {
                "dispute_id": dispute_id,
                "contract_id": contract_id,
                "reason": reason,
                "opened_by": by,
            },
        )

    def resolve_dispute(self, dispute_id: str, outcome: str, on: date, by: str) -> None:
        self._require_actor(by, Role.REGULATOR, "争议处理结论须由住房管理部门作出")
        dispute = self._state.disputes.get(dispute_id)
        if dispute is None:
            raise UnknownEntityError(f"争议记录不存在:{dispute_id}")
        if dispute.status != "open":
            raise ConflictError("争议已处理")
        if not outcome.strip():
            raise RecordError("争议处理结论不能为空")
        self._record(
            "dispute_resolved",
            on,
            {"dispute_id": dispute_id, "outcome": outcome},
        )

    def rebase_unit(
        self,
        unit_id: str,
        on: date,
        by: str,
        plan_ref: tuple[str, int] | None = None,
        survey_ref: tuple[str, int] | None = None,
        agreement_id: str | None = None,
    ) -> None:
        """把方案或面积变化落到房屋上；已签约房源必须凭补充协议衔接。"""
        self._require_actor(by, Role.REGULATOR, "房源版本调整须由住房管理部门记录")
        unit = self._unit(unit_id)
        building = self._state.buildings[unit.building_id]
        if plan_ref is None and survey_ref is None:
            raise RecordError("须指定新的方案或测绘版本")
        data: dict[str, Any] = {"unit_id": unit_id, "agreement_id": agreement_id, "rebased_by": by}
        if plan_ref is not None:
            plan = self._state.plans.get(tuple(plan_ref))
            if plan is None or plan.parcel_id != building.parcel_id:
                raise UnknownEntityError("方案版本不存在或不属于本地块")
            if plan.status != "approved":
                raise PreconditionError("方案版本未批准")
            data["plan_id"], data["plan_version"] = plan.plan_id, plan.version_no
        if survey_ref is not None:
            survey = self._state.surveys.get(tuple(survey_ref))
            if survey is None or survey.building_id != unit.building_id:
                raise UnknownEntityError("测绘版本不存在或不属于本楼栋")
            if survey.status != "approved":
                raise PreconditionError("测绘版本未批准")
            data["survey_id"], data["survey_version"] = survey.survey_id, survey.version_no
        contract_id = self._state.contract_by_unit.get(unit_id)
        if contract_id is not None:
            if agreement_id is None:
                raise FrozenError("已签约房源的方案或面积变化须通过补充协议、整改或争议处理衔接")
            agreement = self._state.agreements.get(agreement_id)
            if agreement is None or agreement.contract_id != contract_id:
                raise FrozenError("补充协议与该房屋合同不一致")
        elif agreement_id is not None:
            raise PreconditionError("未签约房源无需补充协议")
        self._record("unit_rebased", on, data)

    # -- 检查与整改 ----------------------------------------------------------

    def open_inspection(
        self,
        inspection_id: str,
        building_id: str,
        kind: InspectionKind | str,
        items: Iterable[tuple[str, str]],
        on: date,
        by: str,
    ) -> None:
        """开立检查，每个事项指定唯一负责的检查人员。"""
        self._require_actor(by, Role.REGULATOR, "检查须由住房管理部门组织")
        kind = InspectionKind(kind)
        self._building(building_id)
        if inspection_id in self._state.inspections:
            raise ConflictError(f"检查记录已存在:{inspection_id}")
        entries = []
        seen = set()
        for key, responsible in items:
            if not key.strip():
                raise RecordError("检查事项不能为空")
            if key in seen:
                raise ConflictError(f"检查事项重复:{key}")
            seen.add(key)
            self._require_actor(responsible, Role.INSPECTOR, "检查事项负责人须为检查人员")
            entries.append({"key": key, "responsible": responsible})
        if not entries:
            raise RecordError("检查事项不能为空")
        self._record(
            "inspection_opened",
            on,
            {
                "inspection_id": inspection_id,
                "building_id": building_id,
                "kind": kind.value,
                "items": entries,
                "opened_by": by,
            },
        )

    def _inspection_item(self, inspection_id: str, item_key: str) -> _InspectionItem:
        inspection = self._state.inspections.get(inspection_id)
        if inspection is None:
            raise UnknownEntityError(f"检查记录不存在:{inspection_id}")
        item = inspection.items.get(item_key)
        if item is None:
            raise UnknownEntityError(f"检查事项不存在:{item_key}")
        return item

    def confirm_item(
        self,
        inspection_id: str,
        item_key: str,
        inspector_id: str,
        passed: bool,
        conclusion: str,
        on: date,
        rectification_deadline: date | None = None,
    ) -> None:
        """检查人员只能确认本人负责的事项；不合格事项须给出整改期限。"""
        self._require_actor(inspector_id, Role.INSPECTOR, "只有检查人员可以确认检查事项")
        item = self._inspection_item(inspection_id, item_key)
        if item.responsible != inspector_id:
            raise DutyError("检查人员只能确认本人负责事项")
        if item.confirmed:
            raise ConflictError("检查事项已确认")
        if not conclusion.strip():
            raise RecordError("检查结论不能为空")
        if not passed and rectification_deadline is None:
            raise PreconditionError("不合格事项须给出整改期限")
        self._record(
            "inspection_item_confirmed",
            on,
            {
                "inspection_id": inspection_id,
                "item_key": item_key,
                "passed": bool(passed),
                "conclusion": conclusion,
                "inspector_id": inspector_id,
                "rectification_deadline": (
                    rectification_deadline.isoformat() if rectification_deadline else None
                ),
            },
        )

    def submit_rectification(
        self, inspection_id: str, item_key: str, evidence: dict[str, Any], on: date, by: str
    ) -> None:
        """开发企业报送整改材料，结论仍须管理部门作出。"""
        self._require_actor(by, Role.DEVELOPER, "整改材料须由开发企业报送")
        item = self._inspection_item(inspection_id, item_key)
        if not item.confirmed or item.passed:
            raise PreconditionError("该事项无需整改")
        if item.rectification_closed and item.rectification_approved:
            raise ConflictError("整改已结论")
        if not isinstance(evidence, dict) or not evidence:
            raise RecordError("整改材料不能为空")
        self._record(
            "rectification_submitted",
            on,
            {
                "inspection_id": inspection_id,
                "item_key": item_key,
                "evidence_hash": content_fingerprint(evidence),
                "submitted_by": by,
            },
        )

    def close_rectification(
        self, inspection_id: str, item_key: str, approved: bool, by: str, on: date, note: str = ""
    ) -> None:
        """整改结论只能由住房管理部门批准，开发企业不能自行批准。"""
        self._require_actor(by, Role.REGULATOR, "整改结论只能由住房管理部门批准，开发企业不能自批")
        item = self._inspection_item(inspection_id, item_key)
        if item.evidence_hash is None:
            raise PreconditionError("尚未报送整改材料")
        if item.rectification_closed:
            raise ConflictError("整改结论已作出")
        self._record(
            "rectification_closed",
            on,
            {
                "inspection_id": inspection_id,
                "item_key": item_key,
                "approved": bool(approved),
                "note": note,
                "closed_by": by,
            },
        )

    # -- 暂停与恢复 ----------------------------------------------------------

    def suspend_building(self, building_id: str, reason: str, on: date, by: str) -> None:
        self._require_actor(by, Role.REGULATOR, "暂停销售须由住房管理部门决定")
        self._suspend_building(building_id, reason, on)

    def _suspend_building(self, building_id: str, reason: str, on: date) -> None:
        building = self._building(building_id)
        if building.suspended:
            raise ConflictError("楼栋已处于暂停状态")
        if not reason.strip():
            raise RecordError("暂停原因不能为空")
        self._record(
            "building_suspended", on, {"building_id": building_id, "reason": reason}
        )

    def resume_building(self, building_id: str, on: date, by: str) -> None:
        self._require_actor(by, Role.REGULATOR, "恢复销售须由住房管理部门决定")
        building = self._building(building_id)
        if not building.suspended:
            raise ConflictError("楼栋未处于暂停状态")
        self._record("building_resumed", on, {"building_id": building_id})

    def suspend_unit(self, unit_id: str, reason: str, on: date, by: str) -> None:
        self._require_actor(by, Role.REGULATOR, "暂停销售须由住房管理部门决定")
        unit = self._unit(unit_id)
        if unit.suspended:
            raise ConflictError("房屋已处于暂停状态")
        if not reason.strip():
            raise RecordError("暂停原因不能为空")
        self._record("unit_suspended", on, {"unit_id": unit_id, "reason": reason})

    def resume_unit(self, unit_id: str, on: date, by: str) -> None:
        self._require_actor(by, Role.REGULATOR, "恢复销售须由住房管理部门决定")
        unit = self._unit(unit_id)
        if not unit.suspended:
            raise ConflictError("房屋未处于暂停状态")
        self._record("unit_resumed", on, {"unit_id": unit_id})

    def check_building_consistency(
        self, building_id: str, on: date, tolerance: float = 0.6
    ) -> tuple[AreaConflict, ...]:
        """比对已批准方案承诺面积与最新已批准测绘面积。

        发现冲突时只暂停该栋关联房源，不影响其他楼栋。
        """
        building = self._building(building_id)
        plan = self._latest_approved_plan(self._state, building.parcel_id)
        survey = self._latest_approved_survey(self._state, building_id)
        conflicts: list[AreaConflict] = []
        if plan is not None and survey is not None:
            planned_areas = plan.content.get("unit_areas", {})
            for unit_id in self._building_units(self._state, building_id):
                planned = planned_areas.get(unit_id)
                surveyed = survey.areas.get(unit_id)
                if planned is None:
                    conflicts.append(AreaConflict(unit_id, None, surveyed, None))
                elif surveyed is not None and abs(planned - surveyed) > tolerance:
                    conflicts.append(
                        AreaConflict(unit_id, planned, surveyed, round(abs(planned - surveyed), 4))
                    )
        if conflicts and not building.suspended:
            self._suspend_building(building_id, "方案与测绘面积资料冲突", on)
        return tuple(conflicts)

    # -- 查询与历史还原 ------------------------------------------------------

    def is_sellable(self, unit_id: str) -> bool:
        unit = self._unit(unit_id)
        building = self._state.buildings[unit.building_id]
        return (
            not unit.suspended
            and not building.suspended
            and unit_id not in self._state.contract_by_unit
        )

    def overdue_rectifications(self, as_of: date | None = None) -> tuple[RectificationView, ...]:
        """到期未完成的整改事项；默认以台账最新业务日期为基准。"""
        state, effective = self._state_and_date(as_of)
        return tuple(
            RectificationView(
                inspection_id=inspection.inspection_id,
                building_id=inspection.building_id,
                item_key=item.key,
                deadline=item.rectification_deadline,
                evidence_hash=item.evidence_hash,
            )
            for inspection, item in self._overdue_items(
                state, set(state.buildings), effective
            )
        )

    def pending_acceptance_items(self, as_of: date | None = None) -> tuple[AcceptanceItemView, ...]:
        """尚未得出合格结论的验收事项。"""
        state, _ = self._state_and_date(as_of)
        found = []
        for inspection in state.inspections.values():
            if inspection.kind != InspectionKind.ACCEPTANCE.value:
                continue
            for item in inspection.items.values():
                if item.status in ("open", "rectifying"):
                    found.append(
                        AcceptanceItemView(
                            inspection_id=inspection.inspection_id,
                            building_id=inspection.building_id,
                            item_key=item.key,
                            responsible=item.responsible,
                            status=item.status,
                            deadline=item.rectification_deadline,
                        )
                    )
        return tuple(found)

    def verify_contract(self, contract_id: str, as_of: date | None = None) -> ContractCheck:
        """把合同冻结的公示摘要与公示记录、房屋互相核对。"""
        state, _ = self._state_and_date(as_of)
        contract = state.contracts.get(contract_id)
        if contract is None:
            raise UnknownEntityError(f"合同不存在:{contract_id}")
        problems: list[str] = []
        disclosure = state.disclosures.get(contract.disclosure_id)
        view = None
        if disclosure is None:
            problems.append("公示记录缺失")
        else:
            view = self._disclosure_view(disclosure)
            if content_fingerprint(disclosure.content) != disclosure.content_hash:
                problems.append("公示内容与发布时摘要不一致")
            if contract.disclosure_hash != disclosure.content_hash:
                problems.append("合同冻结摘要与公示记录摘要不一致")
            if contract.unit_id not in disclosure.content.get("units", []):
                problems.append("公示未覆盖合同房屋")
            if (contract.plan_id, contract.plan_version) != (
                disclosure.plan_id,
                disclosure.plan_version,
            ):
                problems.append("合同与公示的方案版本不一致")
            if contract.surveys != disclosure.surveys:
                problems.append("合同与公示的测绘版本不一致")
        return ContractCheck(contract_id, not problems, tuple(problems), view)

    def unit_history(self, unit_id: str, as_of: date) -> UnitHistory:
        """按历史日期还原一套房屋的公示、检查结论、签约依据与之后的每次变化。"""
        state = self._state_at(as_of)
        unit = state.units.get(unit_id)
        if unit is None:
            raise UnknownEntityError("该日期房屋尚未登记")
        contract = None
        disclosure = None
        contract_id = state.contract_by_unit.get(unit_id)
        if contract_id is not None:
            contract = self._contract_view(state.contracts[contract_id])
            record = state.disclosures.get(state.contracts[contract_id].disclosure_id)
            if record is not None:
                disclosure = self._disclosure_view(record)
        else:
            covering = [
                record
                for record in state.disclosures.values()
                if unit_id in record.content.get("units", [])
            ]
            if covering:
                disclosure = self._disclosure_view(max(covering, key=lambda record: record.seq))
        inspections = tuple(
            self._inspection_view(inspection)
            for inspection in state.inspections.values()
            if inspection.building_id == unit.building_id
        )
        changes = tuple(
            event
            for event in self._events
            if event.recorded_on > as_of and self._affects_unit(event, unit_id)
        )
        return UnitHistory(unit_id, as_of, disclosure, inspections, contract, changes)

    def _affects_unit(self, event: Event, unit_id: str) -> bool:
        state = self._state
        unit = state.units[unit_id]
        building_id = unit.building_id
        parcel_id = state.buildings[building_id].parcel_id
        contract_ids = {
            contract.contract_id
            for contract in state.contracts.values()
            if contract.unit_id == unit_id
        }
        inspection_ids = {
            inspection.inspection_id
            for inspection in state.inspections.values()
            if inspection.building_id == building_id
        }
        batch_ids = {
            batch.batch_id
            for batch in state.batches.values()
            if building_id in batch.building_ids
        }
        data = event.data
        kind = event.kind
        if kind in ("unit_registered", "unit_suspended", "unit_resumed", "unit_rebased"):
            return data.get("unit_id") == unit_id
        if kind in ("building_registered", "building_suspended", "building_resumed"):
            return data.get("building_id") == building_id
        if kind == "inspection_opened":
            return data.get("building_id") == building_id
        if kind in ("inspection_item_confirmed", "rectification_submitted", "rectification_closed"):
            return data.get("inspection_id") in inspection_ids
        if kind == "contract_signed":
            return data.get("unit_id") == unit_id
        if kind in ("agreement_signed", "dispute_opened", "dispute_resolved"):
            return data.get("contract_id") in contract_ids
        if kind == "disclosure_published":
            return data.get("batch_id") in batch_ids
        if kind == "batch_opened":
            return building_id in data.get("building_ids", [])
        if kind in ("plan_submitted", "plan_reviewed"):
            return data.get("parcel_id") == parcel_id
        if kind in ("survey_submitted", "survey_reviewed"):
            return data.get("building_id") == building_id
        return False

    # -- 只读取件 ------------------------------------------------------------

    def get_plan(self, plan_id: str, version_no: int | None = None) -> PlanView:
        versions = [
            plan for (pid, _), plan in self._state.plans.items() if pid == plan_id
        ]
        if version_no is not None:
            versions = [plan for plan in versions if plan.version_no == version_no]
        if not versions:
            raise UnknownEntityError(f"方案版本不存在:{plan_id}")
        return self._plan_view(max(versions, key=lambda plan: plan.seq))

    def get_survey(self, survey_id: str, version_no: int | None = None) -> SurveyView:
        versions = [
            survey for (sid, _), survey in self._state.surveys.items() if sid == survey_id
        ]
        if version_no is not None:
            versions = [survey for survey in versions if survey.version_no == version_no]
        if not versions:
            raise UnknownEntityError(f"测绘版本不存在:{survey_id}")
        return self._survey_view(max(versions, key=lambda survey: survey.seq))

    def get_disclosure(self, disclosure_id: str) -> DisclosureView:
        disclosure = self._state.disclosures.get(disclosure_id)
        if disclosure is None:
            raise UnknownEntityError(f"公示不存在:{disclosure_id}")
        return self._disclosure_view(disclosure)

    def get_contract(self, contract_id: str) -> ContractView:
        contract = self._state.contracts.get(contract_id)
        if contract is None:
            raise UnknownEntityError(f"合同不存在:{contract_id}")
        return self._contract_view(contract)

    def get_inspection(self, inspection_id: str) -> InspectionView:
        inspection = self._state.inspections.get(inspection_id)
        if inspection is None:
            raise UnknownEntityError(f"检查记录不存在:{inspection_id}")
        return self._inspection_view(inspection)

    # -- 视图构造 ------------------------------------------------------------

    @staticmethod
    def _plan_view(plan: _PlanVersion) -> PlanView:
        return PlanView(
            plan_id=plan.plan_id,
            version_no=plan.version_no,
            parcel_id=plan.parcel_id,
            status=plan.status,
            content_hash=plan.content_hash,
            reused_from=tuple(plan.reused_from) if plan.reused_from else None,
            decided_by=plan.decided_by,
        )

    @staticmethod
    def _survey_view(survey: _SurveyVersion) -> SurveyView:
        return SurveyView(
            survey_id=survey.survey_id,
            version_no=survey.version_no,
            building_id=survey.building_id,
            kind=survey.kind,
            status=survey.status,
            content_hash=survey.content_hash,
            reused_from=tuple(survey.reused_from) if survey.reused_from else None,
            decided_by=survey.decided_by,
        )

    @staticmethod
    def _disclosure_view(disclosure: _Disclosure) -> DisclosureView:
        return DisclosureView(
            disclosure_id=disclosure.disclosure_id,
            batch_id=disclosure.batch_id,
            plan_ref=(disclosure.plan_id, disclosure.plan_version),
            survey_refs={
                key: (ref[0], ref[1]) for key, ref in disclosure.surveys.items()
            },
            content_hash=disclosure.content_hash,
            content=copy.deepcopy(disclosure.content),
            published_on=disclosure.published_on,
        )

    @staticmethod
    def _contract_view(contract: _Contract) -> ContractView:
        return ContractView(
            contract_id=contract.contract_id,
            unit_id=contract.unit_id,
            batch_id=contract.batch_id,
            buyer_ref=contract.buyer_ref,
            disclosure_id=contract.disclosure_id,
            disclosure_hash=contract.disclosure_hash,
            plan_ref=(contract.plan_id, contract.plan_version),
            survey_refs={key: (ref[0], ref[1]) for key, ref in contract.surveys.items()},
            attachments=tuple(dict(item) for item in contract.attachments),
            agreements=tuple(contract.agreements),
            signed_on=contract.signed_on,
        )

    @staticmethod
    def _inspection_view(inspection: _Inspection) -> InspectionView:
        return InspectionView(
            inspection_id=inspection.inspection_id,
            building_id=inspection.building_id,
            kind=inspection.kind,
            items=tuple(
                InspectionItemView(
                    key=item.key,
                    responsible=item.responsible,
                    status=item.status,
                    conclusion=item.conclusion,
                    confirmed_by=item.confirmed_by,
                    rectification_deadline=item.rectification_deadline,
                    evidence_hash=item.evidence_hash,
                    rectification_approved=(
                        item.rectification_approved if item.rectification_closed else None
                    ),
                    rectification_closed_by=item.rectification_closed_by,
                )
                for item in inspection.items.values()
            ),
        )


__all__ = [
    "AcceptanceItemView",
    "AreaConflict",
    "ConflictError",
    "ContractCheck",
    "ContractView",
    "DisclosureView",
    "DutyError",
    "Event",
    "FrozenError",
    "InspectionItemView",
    "InspectionKind",
    "InspectionView",
    "JsonLinesStore",
    "PlanView",
    "PreconditionError",
    "RecordError",
    "RectificationView",
    "Role",
    "SaleMode",
    "SalesRegistry",
    "SurveyKind",
    "SurveyView",
    "UnitHistory",
    "UnknownEntityError",
    "content_fingerprint",
]
