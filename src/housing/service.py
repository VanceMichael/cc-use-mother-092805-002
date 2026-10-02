"""商品住房销售管理后端服务。

设计要点
========

1. 所有变化都是只增事件（见 :class:`src.housing.events.EventStore`）。
   方案版本、公示、检查结论、整改、验收、签约、补充协议、争议处理
   全部可以按日期回放，没有人能够事后改写。
2. 签约时把购房人看到的那一版公示连同每份材料的版本号与内容指纹
   整体复制进签约事件，形成冻结快照；之后方案再批准新版，也不影响
   已签约合同的核对依据。
3. 预售、现售各自有前置条件清单；检查人员只能确认本人负责的检查
   事项；整改结论与方案审批只能由区级管理人员作出，开发企业不能
   自行批准。
4. 方案或面积变化只能作用于未签约房源；已签约房源必须通过补充
   协议、整改或争议处理衔接。资料冲突只暂停涉事楼栋，其他楼栋的
   签约与流程不被阻断。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .events import Event, EventStore
from .models import (
    AcceptanceStatus,
    Actor,
    ChangeEffect,
    ChangeOrder,
    Conflict,
    Disclosure,
    DOC_KINDS,
    DocumentVersion,
    DomainError,
    House,
    Inspection,
    InspectionResult,
    Building,
    NotFound,
    PendingAcceptance,
    PlanStatus,
    Precondition,
    Rectification,
    RectificationStatus,
    Role,
    SaleMode,
    SalesBatch,
    SignedLink,
    AuthorizationError,
)

# 预售与现售各自的前置条件。
PRESALE_ITEMS = frozenset({"预售许可证", "建设工程规划许可证", "工程形象进度承诺", "预售资金监管协议"})
SPOT_ITEMS = frozenset({"竣工验收备案", "不动产权属证明", "建设工程规划许可证", "房屋测绘成果确认"})

SYSTEM = "SYSTEM"


def content_fingerprint(value: Any) -> str:
    """对报送材料的实际内容计算指纹，用于识别是否重复报送。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class _State:
    """事件回放得到的当前（或某一历史日期的）投影。"""

    actors: dict[str, Actor] = field(default_factory=dict)
    plots: dict[str, dict[str, Any]] = field(default_factory=dict)
    buildings: dict[str, Building] = field(default_factory=dict)
    houses: dict[str, House] = field(default_factory=dict)
    # doc_id -> {"kind", "scope_plot", "scope_buildings", "versions": {版本: DocumentVersion}}
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # content_hash -> (doc_id, version)，识别同一单据的重复报送
    hash_index: dict[str, tuple[str, int]] = field(default_factory=dict)
    batches: dict[str, SalesBatch] = field(default_factory=dict)
    disclosures: dict[str, Disclosure] = field(default_factory=dict)
    preconditions: dict[tuple[str, str], Precondition] = field(default_factory=dict)
    inspections: dict[str, Inspection] = field(default_factory=dict)
    rectifications: dict[str, Rectification] = field(default_factory=dict)
    acceptances: dict[str, PendingAcceptance] = field(default_factory=dict)
    changes: dict[str, ChangeOrder] = field(default_factory=dict)
    links: dict[str, SignedLink] = field(default_factory=dict)
    disputes: dict[str, dict[str, Any]] = field(default_factory=dict)


class HousingSalesBackend:
    """商品住房销售管理应用服务。每个方法校验规则后向事件流追加事件。"""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()

    # ------------------------------------------------------------------ 基础

    @staticmethod
    def bootstrap(store: EventStore, district_code: str, district_name: str, at: str) -> "HousingSalesBackend":
        """创建后端并登记第一位区级管理人员（引导账户）。"""
        backend = HousingSalesBackend(store)
        store.append(at, "ActorRegistered", SYSTEM, {
            "code": district_code, "name": district_name,
            "role": Role.DISTRICT.value, "responsible_items": [],
        })
        return backend

    def _state(self, as_of: str | None = None) -> _State:
        state = _State()
        for event in self.store.replay():
            if as_of is not None and event.at > as_of:
                continue
            self._apply(state, event)
        return state

    def _append(self, at: str, type_: str, actor: str, payload: dict[str, Any]) -> Event:
        return self.store.append(at, type_, actor, payload)

    def _actor(self, state: _State, code: str) -> Actor:
        try:
            return state.actors[code]
        except KeyError:
            raise NotFound(f"参与方不存在：{code}") from None

    def _require_role(self, state: _State, code: str, role: Role) -> Actor:
        actor = self._actor(state, code)
        if actor.role != role:
            raise AuthorizationError(f"该操作仅{role.value}可执行，{actor.name}的角色为{actor.role.value}")
        return actor

    # ------------------------------------------------------------- 参与方与地块

    def register_actor(self, code: str, name: str, role: Role,
                       responsible_items: frozenset[str] | set[str] | tuple[str, ...] = (),
                       *, actor: str, at: str) -> None:
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        if code in state.actors:
            raise Conflict(f"参与方已存在：{code}")
        role_value = role.value if isinstance(role, Role) else str(role)
        if role_value not in {r.value for r in Role}:
            raise DomainError("未知角色")
        self._append(at, "ActorRegistered", actor, {
            "code": code, "name": name, "role": role_value,
            "responsible_items": sorted(responsible_items),
        })

    def register_plot(self, code: str, name: str, *, actor: str, at: str) -> None:
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        if code in state.plots:
            raise Conflict(f"地块已存在：{code}")
        self._append(at, "PlotRegistered", actor, {"code": code, "name": name})

    def register_building(self, code: str, plot_code: str, *, actor: str, at: str) -> None:
        state = self._state()
        self._actor(state, actor)  # 任何已登记参与方均可录入楼栋结构
        if plot_code not in state.plots:
            raise NotFound(f"地块不存在：{plot_code}")
        if code in state.buildings:
            raise Conflict(f"楼栋已存在：{code}")
        self._append(at, "BuildingRegistered", actor, {"code": code, "plot_code": plot_code})

    def register_house(self, code: str, building_code: str, *, actor: str, at: str) -> None:
        state = self._state()
        self._actor(state, actor)
        if building_code not in state.buildings:
            raise NotFound(f"楼栋不存在：{building_code}")
        if code in state.houses:
            raise Conflict(f"房屋已存在：{code}")
        self._append(at, "HouseRegistered", actor, {"code": code, "building_code": building_code})

    # ------------------------------------------------------------- 材料版本链

    def submit_document(self, kind: str, doc_id: str, title: str, content_hash: str,
                        scope_plot: str | None = None,
                        scope_buildings: list[str] | tuple[str, ...] | None = None,
                        *, actor: str, at: str) -> DocumentVersion:
        """报送建设方案/公共配套/测绘成果的一个版本。

        同一材料内容指纹未变视为重复报送：不产生新的待审版本，直接
        沿用原审批结果；内容变化则形成新的待审版本。
        """
        state = self._state()
        if kind == "测绘成果":
            self._require_role(state, actor, Role.SURVEYOR)
        else:
            self._require_role(state, actor, Role.DEVELOPER)
        if kind not in DOC_KINDS:
            raise DomainError(f"未知材料类型：{kind}")
        buildings = list(scope_buildings or [])
        if scope_plot and scope_plot not in state.plots:
            raise NotFound(f"地块不存在：{scope_plot}")
        for building in buildings:
            if building not in state.buildings:
                raise NotFound(f"楼栋不存在：{building}")

        doc = state.docs.get(doc_id)
        if doc and doc["kind"] != kind:
            raise Conflict(f"材料{doc_id}的类型不能改变")
        previous = state.hash_index.get(content_hash)
        if previous and previous[0] == doc_id:
            old = state.docs[doc_id]["versions"][previous[1]]
            # 重复报送：沿用原审批结果，不追加待审版本。
            return old

        version = (max(doc["versions"]) + 1) if doc else 1
        self._append(at, "DocumentSubmitted", actor, {
            "kind": kind, "doc_id": doc_id, "version": version, "title": title,
            "content_hash": content_hash, "scope_plot": scope_plot,
            "scope_buildings": buildings, "supersedes": version - 1 if version > 1 else None,
        })
        state = self._state()
        return state.docs[doc_id]["versions"][version]

    def review_document(self, doc_id: str, version: int, approve: bool, note: str,
                        *, actor: str, at: str) -> None:
        """区级管理人员审批材料版本。开发企业无权调用。"""
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        doc = state.docs.get(doc_id)
        if not doc or version not in doc["versions"]:
            raise NotFound(f"材料版本不存在：{doc_id}:{version}")
        record = doc["versions"][version]
        if record.status != PlanStatus.PENDING:
            raise Conflict(f"版本已审结：{doc_id}:{version}，结论为{record.status.value}")
        self._append(at, "DocumentReviewed", actor, {
            "doc_id": doc_id, "version": version,
            "status": PlanStatus.APPROVED.value if approve else PlanStatus.REJECTED.value,
            "note": note,
        })

    # ------------------------------------------------------------- 销售批次

    def create_batch(self, code: str, plot_code: str, building_codes: list[str],
                     mode: SaleMode, *, actor: str, at: str) -> None:
        state = self._state()
        self._require_role(state, actor, Role.DEVELOPER)
        if code in state.batches:
            raise Conflict(f"销售批次已存在：{code}")
        if plot_code not in state.plots:
            raise NotFound(f"地块不存在：{plot_code}")
        for building in building_codes:
            record = state.buildings.get(building)
            if not record or record.plot_code != plot_code:
                raise NotFound(f"楼栋不存在或不属于该地块：{building}")
        if not building_codes:
            raise DomainError("销售批次至少包含一栋楼")
        self._append(at, "BatchCreated", actor, {
            "code": code, "plot_code": plot_code,
            "building_codes": list(building_codes), "mode": mode.value,
        })

    def confirm_precondition(self, batch_code: str, item: str, satisfied: bool,
                             *, actor: str, at: str) -> None:
        """确认一项前置条件。预售/现售只能确认各自清单内的事项。"""
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        batch = self._batch(state, batch_code)
        allowed = PRESALE_ITEMS if batch.mode == SaleMode.PRESALE else SPOT_ITEMS
        if item not in allowed:
            raise DomainError(f"{batch.mode.value}批次不存在前置条件：{item}")
        self._append(at, "PreconditionConfirmed", actor, {
            "batch_code": batch_code, "item": item, "satisfied": satisfied,
        })

    def open_batch(self, batch_code: str, *, actor: str, at: str) -> None:
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        batch = self._batch(state, batch_code)
        if batch.opened:
            raise Conflict(f"批次已开盘：{batch_code}")
        required = PRESALE_ITEMS if batch.mode == SaleMode.PRESALE else SPOT_ITEMS
        confirmed = {
            item for item in required
            if (record := state.preconditions.get((batch_code, item))) and record.satisfied
        }
        missing = required - confirmed
        if missing:
            raise DomainError(f"前置条件未全部满足：{sorted(missing)}")
        if batch.mode == SaleMode.PRESALE:
            if not self._approved_doc(state, "建设方案", plot_code=batch.plot_code):
                raise DomainError("预售批次缺少已批准的建设方案")
        else:
            for building in batch.building_codes:
                if not self._approved_doc(state, "测绘成果", building_code=building):
                    raise DomainError(f"现售批次缺少楼栋{building}的已批准测绘成果")
        self._append(at, "BatchOpened", actor, {"batch_code": batch_code})

    @staticmethod
    def _batch(state: _State, batch_code: str) -> SalesBatch:
        try:
            return state.batches[batch_code]
        except KeyError:
            raise NotFound(f"销售批次不存在：{batch_code}") from None

    @staticmethod
    def _approved_doc(state: _State, kind: str, *, plot_code: str | None = None,
                      building_code: str | None = None) -> DocumentVersion | None:
        for doc in state.docs.values():
            if doc["kind"] != kind:
                continue
            if plot_code and doc["scope_plot"] != plot_code:
                continue
            if building_code and building_code not in doc["scope_buildings"]:
                continue
            approved = [v for v in doc["versions"].values() if v.status == PlanStatus.APPROVED]
            if approved:
                return max(approved, key=lambda v: v.version)
        return None

    # ------------------------------------------------------------- 公示与签约

    def publish_disclosure(self, code: str, batch_code: str,
                           refs: dict[str, str], *, actor: str, at: str) -> None:
        """在销售现场公示一版材料。refs: 材料类型 -> "材料标识:版本号"。

        只能引用已批准版本；发布新版时旧版标记撤换时间，但已经被
        合同冻结的快照不受影响。
        """
        state = self._state()
        self._require_role(state, actor, Role.DEVELOPER)
        batch = self._batch(state, batch_code)
        if not batch.opened:
            raise DomainError("批次未开盘，不能发布公示")
        if code in state.disclosures:
            raise Conflict(f"公示已存在：{code}")
        if set(refs) - DOC_KINDS:
            raise DomainError("公示包含未知材料类型")
        resolved: dict[str, dict[str, Any]] = {}
        for kind, pointer in refs.items():
            doc_id, _, version_text = pointer.partition(":")
            doc = state.docs.get(doc_id)
            if not doc or not version_text.isdigit() or int(version_text) not in doc["versions"]:
                raise NotFound(f"公示引用的材料版本不存在：{pointer}")
            record = doc["versions"][int(version_text)]
            if record.kind != kind:
                raise DomainError(f"公示材料类型不符：{pointer}应为{kind}")
            if record.status != PlanStatus.APPROVED:
                raise DomainError(f"公示只能引用已批准版本：{pointer}为{record.status.value}")
            resolved[kind] = {
                "doc_id": doc_id, "version": record.version,
                "title": record.title, "content_hash": record.content_hash,
            }
        replaced = None
        active = [d for d in state.disclosures.values()
                  if d.batch_code == batch_code and d.displayed_until is None]
        if active:
            replaced = active[0].code
        self._append(at, "DisclosurePublished", actor, {
            "code": code, "batch_code": batch_code,
            "refs": dict(refs), "resolved": resolved, "replaced_code": replaced,
        })

    def disclosure_on_display(self, batch_code: str, at: str) -> Disclosure | None:
        """还原某一时刻销售现场正在展示的公示。"""
        state = self._state()
        for disclosure in state.disclosures.values():
            if disclosure.batch_code != batch_code:
                continue
            if disclosure.displayed_from <= at and (
                disclosure.displayed_until is None or at < disclosure.displayed_until
            ):
                return disclosure
        return None

    def sign_contract(self, house_code: str, contract_code: str, disclosure_code: str,
                      *, actor: str, at: str) -> None:
        """购房签约：冻结当时公示内容，并与房屋、合同绑定。"""
        state = self._state()
        self._require_role(state, actor, Role.DEVELOPER)
        house = state.houses.get(house_code)
        if not house:
            raise NotFound(f"房屋不存在：{house_code}")
        if house.signed:
            raise Conflict(f"房屋已签约：{house_code}，合同号{house.contract_code}")
        building = state.buildings[house.building_code]
        if building.suspended:
            raise DomainError(f"楼栋{building.code}资料冲突暂停销售，不能签约")
        disclosure = state.disclosures.get(disclosure_code)
        if not disclosure:
            raise NotFound(f"公示不存在：{disclosure_code}")
        batch = state.batches[disclosure.batch_code]
        if house.building_code not in batch.building_codes:
            raise DomainError("公示所属批次不包含该房屋")
        if not (disclosure.displayed_from <= at and (
            disclosure.displayed_until is None or at < disclosure.displayed_until
        )):
            raise DomainError("签约时该版公示已不在销售现场展示，不能作为签约依据")
        # 冻结快照：把版本号与内容指纹整体复制进签约事件，之后任何
        # 新材料版本都不会改变这份快照。
        self._append(at, "ContractSigned", actor, {
            "house_code": house_code,
            "building_code": house.building_code,
            "batch_code": disclosure.batch_code,
            "contract_code": contract_code,
            "disclosure_code": disclosure_code,
            "frozen_docs": disclosure.doc_refs,
        })

    def cross_check_contract(self, house_code: str) -> dict[str, Any]:
        """核对一套房屋：冻结的签约依据与当前各材料版本的差异。"""
        state = self._state()
        house = state.houses.get(house_code)
        if not house or not house.signed:
            raise NotFound(f"房屋未签约：{house_code}")
        sign = next(
            e for e in self.store.replay()
            if e.type == "ContractSigned" and e.payload["house_code"] == house_code
        )
        frozen_docs = []
        for kind, ref in sign.payload["frozen_docs"].items():
            doc_id, version, title, digest = ref["doc_id"], ref["version"], ref["title"], ref["content_hash"]
            current = max(state.docs[doc_id]["versions"])
            frozen_docs.append({
                "kind": kind, "doc_id": doc_id, "title": title,
                "signed_version": version, "signed_hash": digest,
                "current_version": current,
                "current_status": state.docs[doc_id]["versions"][current].status.value,
                "changed_after_signing": current != version,
            })
        return {
            "house_code": house_code,
            "contract_code": house.contract_code,
            "disclosure_code": house.frozen_disclosure_code,
            "signed_at": sign.at,
            "building_suspended": state.buildings[house.building_code].suspended,
            "frozen_docs": frozen_docs,
        }

    # ------------------------------------------------------------- 检查与整改

    def record_inspection(self, code: str, building_code: str, item: str,
                          result: InspectionResult, finding: str,
                          *, actor: str, at: str, due_at: str | None = None) -> None:
        """登记检查结论。检查人员只能确认本人负责事项。"""
        state = self._state()
        inspector = self._require_role(state, actor, Role.INSPECTOR)
        if item not in inspector.responsible_items:
            raise AuthorizationError(f"检查人员{inspector.name}不负责{item}，不能确认该事项")
        if building_code not in state.buildings:
            raise NotFound(f"楼栋不存在：{building_code}")
        if code in state.inspections:
            raise Conflict(f"检查记录已存在：{code}")
        self._append(at, "InspectionRecorded", actor, {
            "code": code, "building_code": building_code, "item": item,
            "result": result.value, "finding": finding, "inspector_code": actor,
        })
        if result == InspectionResult.ISSUE_FOUND:
            rect_code = f"R-{code}"
            self._append(at, "RectificationOpened", actor, {
                "code": rect_code, "inspection_code": code,
                "building_code": building_code, "item": item,
                "due_at": due_at, "developer_code": None,
            })

    def submit_rectification(self, rect_code: str, evidence: str,
                             *, actor: str, at: str) -> None:
        """开发企业报送整改情况，等待区级管理人员结论。"""
        state = self._state()
        self._require_role(state, actor, Role.DEVELOPER)
        rect = self._rectification(state, rect_code)
        if rect.status not in (RectificationStatus.OPEN, RectificationStatus.OVERDUE, RectificationStatus.REJECTED):
            raise Conflict(f"整改单当前状态{rect.status.value}，不能报送")
        self._append(at, "RectificationSubmitted", actor, {
            "code": rect_code, "evidence": evidence,
        })

    def review_rectification(self, rect_code: str, approve: bool, conclusion: str,
                             *, actor: str, at: str) -> None:
        """区级管理人员作出整改结论；开发企业不能自行批准。"""
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        rect = self._rectification(state, rect_code)
        if rect.status != RectificationStatus.SUBMITTED:
            raise Conflict(f"整改单未处于待审状态：{rect.status.value}")
        self._append(at, "RectificationReviewed", actor, {
            "code": rect_code,
            "status": RectificationStatus.APPROVED.value if approve else RectificationStatus.REJECTED.value,
            "conclusion": conclusion,
        })
        if approve:
            # 整改通过不等于验收完成：另立持久化的待验收事项。
            acceptance_code = f"A-{rect_code}"
            self._append(at, "PendingAcceptanceCreated", actor, {
                "code": acceptance_code, "building_code": rect.building_code,
                "item": rect.item, "source_rectification": rect_code,
                "status": AcceptanceStatus.PENDING.value,
            })

    def sweep_overdue(self, as_of: str, *, actor: str) -> list[str]:
        """把已到整改期限仍未报送的事项标记为到期未整改，记录持久保留。"""
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        overdue = []
        for rect in state.rectifications.values():
            if rect.status == RectificationStatus.OPEN and rect.due_at and rect.due_at < as_of:
                self._append(as_of, "RectificationOverdue", actor, {"code": rect.code})
                overdue.append(rect.code)
        return overdue

    def complete_acceptance(self, acceptance_code: str, note: str,
                            *, actor: str, at: str) -> None:
        """区级管理人员完成待验收事项。"""
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        record = state.acceptances.get(acceptance_code)
        if not record:
            raise NotFound(f"待验收事项不存在：{acceptance_code}")
        if record.status == AcceptanceStatus.DONE:
            raise Conflict("该事项已验收")
        self._append(at, "PendingAcceptanceDone", actor, {"code": acceptance_code, "note": note})

    @staticmethod
    def _rectification(state: _State, rect_code: str) -> Rectification:
        try:
            return state.rectifications[rect_code]
        except KeyError:
            raise NotFound(f"整改单不存在：{rect_code}") from None

    # ------------------------------------------------------------- 楼栋冲突隔离

    def suspend_building(self, building_code: str, reason: str, *, actor: str, at: str) -> None:
        """发现资料冲突时只暂停涉事楼栋的关联房源。"""
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        building = state.buildings.get(building_code)
        if not building:
            raise NotFound(f"楼栋不存在：{building_code}")
        if building.suspended:
            raise Conflict(f"楼栋已暂停：{building_code}")
        self._append(at, "BuildingSuspended", actor,
                     {"building_code": building_code, "reason": reason})

    def resume_building(self, building_code: str, *, actor: str, at: str) -> None:
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        building = state.buildings.get(building_code)
        if not building or not building.suspended:
            raise Conflict(f"楼栋未处于暂停状态：{building_code}")
        self._append(at, "BuildingResumed", actor, {"building_code": building_code})

    # ------------------------------------------------------------- 变更与衔接

    def register_change(self, code: str, doc_kind: str, doc_id: str, new_version: int,
                        *, actor: str, at: str) -> None:
        """登记一次方案或面积变化（依据一个新批准的材料版本）。"""
        state = self._state()
        self._require_role(state, actor, Role.DEVELOPER)
        doc = state.docs.get(doc_id)
        if not doc or new_version not in doc["versions"]:
            raise NotFound(f"材料版本不存在：{doc_id}:{new_version}")
        if doc["kind"] != doc_kind:
            raise DomainError("材料类型不符")
        if doc["versions"][new_version].status != PlanStatus.APPROVED:
            raise DomainError("变化依据的版本尚未批准")
        if code in state.changes:
            raise Conflict(f"变更单已存在：{code}")
        self._append(at, "ChangeRegistered", actor, {
            "code": code, "doc_kind": doc_kind, "doc_id": doc_id, "new_version": new_version,
        })

    def apply_change_to_unsigned(self, change_code: str, house_codes: list[str],
                                 *, actor: str, at: str) -> None:
        """变化只作用于未签约房源；已签约房源必须走衔接流程。"""
        state = self._state()
        self._actor(state, actor)
        change = state.changes.get(change_code)
        if not change:
            raise NotFound(f"变更单不存在：{change_code}")
        doc = state.docs[change.doc_id]
        for house_code in house_codes:
            house = state.houses.get(house_code)
            if not house:
                raise NotFound(f"房屋不存在：{house_code}")
            if house.signed:
                raise DomainError(
                    f"房屋{house_code}已签约（{house.contract_code}），"
                    "变化不能直接作用，须通过补充协议、整改或争议处理衔接"
                )
            scope = doc["scope_buildings"] or []
            if scope and house.building_code not in scope:
                raise DomainError(f"材料范围不覆盖房屋{house_code}所在楼栋")
        self._append(at, "ChangeApplied", actor,
                     {"change_code": change_code, "house_codes": list(house_codes)})

    def link_signed_house(self, house_code: str, effect: ChangeEffect, detail: str,
                          change_code: str | None = None, *, actor: str, at: str) -> str:
        """已签约房源通过补充协议、整改或争议处理与变化衔接。"""
        state = self._state()
        self._actor(state, actor)
        house = state.houses.get(house_code)
        if not house or not house.signed:
            raise DomainError(f"房屋未签约：{house_code}")
        if change_code and change_code not in state.changes:
            raise NotFound(f"变更单不存在：{change_code}")
        link_code = f"L-{house_code}-{len(state.links) + 1}"
        self._append(at, "SignedLinkCreated", actor, {
            "code": link_code, "house_code": house_code,
            "contract_code": house.contract_code, "change_code": change_code,
            "effect": effect.value, "detail": detail,
        })
        return link_code

    def unlinked_signed_houses(self, change_code: str) -> list[str]:
        """找出受变更影响但尚未完成衔接的已签约房源。"""
        state = self._state()
        if change_code not in state.changes:
            raise NotFound(f"变更单不存在：{change_code}")
        linked = {
            link.house_code
            for link in state.links.values()
            if link.change_code == change_code
        }
        # 以变更单范围内、已签约的房屋为检查对象。
        result = []
        change = state.changes[change_code]
        doc = state.docs[change.doc_id]
        for house in state.houses.values():
            if not house.signed:
                continue
            scope = doc["scope_buildings"]
            if scope and house.building_code not in scope:
                continue
            if house.code not in linked:
                result.append(house.code)
        return result

    def record_dispute(self, house_code: str, detail: str, *, actor: str, at: str) -> str:
        state = self._state()
        self._actor(state, actor)
        house = state.houses.get(house_code)
        if not house or not house.signed:
            raise DomainError(f"房屋未签约：{house_code}")
        code = f"D-{house_code}-{len(state.disputes) + 1}"
        self._append(at, "DisputeRecorded", actor,
                     {"code": code, "house_code": house_code,
                      "contract_code": house.contract_code, "detail": detail})
        return code

    def resolve_dispute(self, dispute_code: str, outcome: str, *, actor: str, at: str) -> None:
        state = self._state()
        self._require_role(state, actor, Role.DISTRICT)
        if dispute_code not in state.disputes:
            raise NotFound(f"争议记录不存在：{dispute_code}")
        if state.disputes[dispute_code].get("resolved_at"):
            raise Conflict("争议已处理")
        self._append(at, "DisputeResolved", actor,
                     {"code": dispute_code, "outcome": outcome})

    # ------------------------------------------------------------- 历史还原

    def house_record(self, house_code: str, as_of: str | None = None) -> dict[str, Any]:
        """按历史日期还原一套房屋的公示、检查结论、签约依据与之后的每次变化。

        as_of 给定时，只回放该日期（含）之前的事件，可还原任意时点状态。
        """
        full = self._state()
        house = full.houses.get(house_code)
        if not house:
            raise NotFound(f"房屋不存在：{house_code}")
        building_code = house.building_code
        batch_codes = {b.code for b in full.batches.values() if building_code in b.building_codes}

        # 部分事件（到期、审批结论、争议办结）只携带单号，先建立
        # 整改单号 -> 楼栋、争议单号 -> 房屋 的映射再过滤。
        rect_building: dict[str, str] = {}
        dispute_house: dict[str, str] = {}
        acceptance_building: dict[str, str] = {}
        for event in self.store.replay():
            if as_of is not None and event.at > as_of:
                continue
            p = event.payload
            if event.type == "RectificationOpened":
                rect_building[p["code"]] = p["building_code"]
            elif event.type == "DisputeRecorded":
                dispute_house[p["code"]] = p["house_code"]
            elif event.type == "PendingAcceptanceCreated":
                acceptance_building[p["code"]] = p["building_code"]

        disclosures: list[dict[str, Any]] = []
        inspections: list[dict[str, Any]] = []
        rectifications: list[dict[str, Any]] = []
        acceptances: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        disputes: list[dict[str, Any]] = []
        contract: dict[str, Any] | None = None
        suspension_events: list[dict[str, Any]] = []

        for event in self.store.replay():
            if as_of is not None and event.at > as_of:
                continue
            p = event.payload
            if event.type == "DisclosurePublished" and p["batch_code"] in batch_codes:
                disclosures.append({"at": event.at, **p})
            elif event.type == "ContractSigned" and p["house_code"] == house_code:
                contract = {"at": event.at, **p}
            elif event.type == "InspectionRecorded" and p["building_code"] == building_code:
                inspections.append({"at": event.at, **p})
            elif event.type.startswith("Rectification") and rect_building.get(p.get("code")) == building_code:
                rectifications.append({"at": event.at, "type": event.type, **p})
            elif event.type.startswith("PendingAcceptance") and (
                p.get("building_code") == building_code
                or acceptance_building.get(p.get("code")) == building_code
            ):
                acceptances.append({"at": event.at, "type": event.type, **p})
            elif event.type == "ChangeApplied" and house_code in p["house_codes"]:
                changes.append({"at": event.at, "relation": "直接作用于本房屋", **p})
            elif event.type == "ChangeRegistered":
                changes.append({"at": event.at, "relation": "登记变更", **p})
            elif event.type == "SignedLinkCreated" and p["house_code"] == house_code:
                links.append({"at": event.at, **p})
            elif event.type in ("DisputeRecorded", "DisputeResolved") and dispute_house.get(p.get("code")) == house_code:
                disputes.append({"at": event.at, "type": event.type, **p})
            elif event.type in ("BuildingSuspended", "BuildingResumed") and p.get("building_code") == building_code:
                suspension_events.append({"at": event.at, "type": event.type, **p})

        return {
            "house_code": house_code,
            "building_code": building_code,
            "as_of": as_of,
            "signed": bool(contract),
            "contract": contract,
            "disclosures": disclosures,
            "inspections": inspections,
            "rectifications": rectifications,
            "pending_acceptances": acceptances,
            "changes": changes,
            "signed_links": links,
            "disputes": disputes,
            "building_suspension_history": suspension_events,
        }

    # ------------------------------------------------------------------ 回放

    @staticmethod
    def _apply(state: _State, event: Event) -> None:
        p = event.payload
        kind = event.type
        if kind == "ActorRegistered":
            state.actors[p["code"]] = Actor(
                p["code"], p["name"], Role(p["role"]), frozenset(p.get("responsible_items", []))
            )
        elif kind == "PlotRegistered":
            state.plots[p["code"]] = {"code": p["code"], "name": p["name"]}
        elif kind == "BuildingRegistered":
            state.buildings[p["code"]] = Building(p["code"], p["plot_code"])
        elif kind == "HouseRegistered":
            state.houses[p["code"]] = House(p["code"], p["building_code"])
        elif kind == "DocumentSubmitted":
            doc = state.docs.setdefault(p["doc_id"], {
                "kind": p["kind"], "scope_plot": p.get("scope_plot"),
                "scope_buildings": list(p.get("scope_buildings", [])),
                "versions": {},
            })
            record = DocumentVersion(
                doc_id=p["doc_id"], version=p["version"], kind=p["kind"], title=p["title"],
                content_hash=p["content_hash"], status=PlanStatus.PENDING,
                submitted_by=event.actor, submitted_at=event.at, supersedes=p.get("supersedes"),
            )
            doc["versions"][p["version"]] = record
            state.hash_index.setdefault(p["content_hash"], (p["doc_id"], p["version"]))
        elif kind == "DocumentReviewed":
            record = state.docs[p["doc_id"]]["versions"][p["version"]]
            record.status = PlanStatus(p["status"])
            record.reviewed_by = event.actor
            record.reviewed_at = event.at
            record.review_note = p.get("note", "")
        elif kind == "BatchCreated":
            state.batches[p["code"]] = SalesBatch(
                code=p["code"], plot_code=p["plot_code"],
                building_codes=list(p["building_codes"]), mode=SaleMode(p["mode"]),
            )
        elif kind == "PreconditionConfirmed":
            state.preconditions[(p["batch_code"], p["item"])] = Precondition(
                p["batch_code"], p["item"], p["satisfied"], event.actor, event.at
            )
        elif kind == "BatchOpened":
            state.batches[p["batch_code"]].opened = True
        elif kind == "DisclosurePublished":
            if p.get("replaced_code"):
                previous = state.disclosures[p["replaced_code"]]
                if previous.displayed_until is None:
                    previous.displayed_until = event.at
            disclosure = Disclosure(
                code=p["code"], batch_code=p["batch_code"],
                doc_refs=p["resolved"], displayed_from=event.at,
            )
            state.disclosures[p["code"]] = disclosure
            state.batches[p["batch_code"]].disclosures.append(p["code"])
        elif kind == "BuildingSuspended":
            state.buildings[p["building_code"]].suspended = True
            state.buildings[p["building_code"]].suspend_reason = p["reason"]
        elif kind == "BuildingResumed":
            state.buildings[p["building_code"]].suspended = False
            state.buildings[p["building_code"]].suspend_reason = ""
        elif kind == "ContractSigned":
            house = state.houses[p["house_code"]]
            house.signed = True
            house.contract_code = p["contract_code"]
            house.frozen_disclosure_code = p["disclosure_code"]
            state.disclosures[p["disclosure_code"]].frozen = True
        elif kind == "InspectionRecorded":
            state.inspections[p["code"]] = Inspection(
                code=p["code"], building_code=p["building_code"], item=p["item"],
                result=InspectionResult(p["result"]), inspector_code=p["inspector_code"],
                inspected_at=event.at, finding=p.get("finding", ""),
            )
        elif kind == "RectificationOpened":
            state.rectifications[p["code"]] = Rectification(
                code=p["code"], inspection_code=p["inspection_code"],
                building_code=p["building_code"], item=p["item"],
                status=RectificationStatus.OPEN, due_at=p.get("due_at"),
                developer_code=p.get("developer_code"),
            )
        elif kind == "RectificationSubmitted":
            rect = state.rectifications[p["code"]]
            rect.history.append({"at": event.at, "status": rect.status.value})
            rect.status = RectificationStatus.SUBMITTED
            rect.submitted_at = event.at
            rect.evidence = p["evidence"]
            rect.developer_code = event.actor
        elif kind == "RectificationReviewed":
            rect = state.rectifications[p["code"]]
            rect.history.append({"at": event.at, "status": rect.status.value})
            rect.status = RectificationStatus(p["status"])
            rect.reviewed_by = event.actor
            rect.reviewed_at = event.at
            rect.conclusion = p.get("conclusion", "")
        elif kind == "RectificationOverdue":
            rect = state.rectifications[p["code"]]
            rect.history.append({"at": event.at, "status": rect.status.value})
            rect.status = RectificationStatus.OVERDUE
        elif kind == "PendingAcceptanceCreated":
            state.acceptances[p["code"]] = PendingAcceptance(
                code=p["code"], building_code=p["building_code"], item=p["item"],
                source_rectification=p.get("source_rectification"),
                status=AcceptanceStatus.PENDING, created_at=event.at,
            )
        elif kind == "PendingAcceptanceDone":
            record = state.acceptances[p["code"]]
            record.status = AcceptanceStatus.DONE
            record.accepted_by = event.actor
            record.accepted_at = event.at
            record.note = p.get("note", "")
        elif kind == "ChangeRegistered":
            state.changes[p["code"]] = ChangeOrder(
                code=p["code"], doc_kind=p["doc_kind"], doc_id=p["doc_id"],
                new_version=p["new_version"], affected_houses=[], created_at=event.at,
            )
        elif kind == "ChangeApplied":
            change = state.changes[p["change_code"]]
            change.applied_to.extend(p["house_codes"])
        elif kind == "SignedLinkCreated":
            link = SignedLink(
                code=p["code"], house_code=p["house_code"], contract_code=p["contract_code"],
                effect=ChangeEffect(p["effect"]), detail=p["detail"],
                created_by=event.actor, created_at=event.at,
                change_code=p.get("change_code"),
            )
            state.links[p["code"]] = link
            change_code = p.get("change_code")
            if change_code:
                state.changes[change_code].linked_houses[p["house_code"]] = link.effect
        elif kind == "DisputeRecorded":
            state.disputes[p["code"]] = {
                "code": p["code"], "house_code": p["house_code"],
                "contract_code": p["contract_code"], "detail": p["detail"],
                "recorded_at": event.at,
            }
        elif kind == "DisputeResolved":
            state.disputes[p["code"]]["resolved_at"] = event.at
            state.disputes[p["code"]]["outcome"] = p["outcome"]
