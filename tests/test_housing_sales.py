"""商品住房销售管理后端的端到端规则测试。

测试数据均为虚构，不含真实个人信息。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.housing.events import EventStore, EventStoreError
from src.housing.models import (
    AuthorizationError,
    ChangeEffect,
    Conflict,
    DomainError,
    InspectionResult,
    NotFound,
    RectificationStatus,
    Role,
    SaleMode,
)
from src.housing.service import HousingSalesBackend, content_fingerprint


DISTRICT = "D01"
DEVELOPER = "DEV1"
SURVEYOR = "SUR1"
INSPECTOR_A = "INSP-A"
INSPECTOR_B = "INSP-B"


def build_world() -> HousingSalesBackend:
    """两栋楼、两名检查人员、一套已批准材料的基础场景。"""
    store = EventStore()
    backend = HousingSalesBackend.bootstrap(store, DISTRICT, "区级张管理", "2026-01-01T09:00")
    backend.register_actor(DEVELOPER, "某开发企业", Role.DEVELOPER, actor=DISTRICT, at="2026-01-02T09:00")
    backend.register_actor(SURVEYOR, "某测绘机构", Role.SURVEYOR,
                           responsible_items=(), actor=DISTRICT, at="2026-01-02T09:00")
    backend.register_actor(INSPECTOR_A, "检查李", Role.INSPECTOR,
                           responsible_items={"消防", "配套用房"}, actor=DISTRICT, at="2026-01-02T09:00")
    backend.register_actor(INSPECTOR_B, "检查王", Role.INSPECTOR,
                           responsible_items={"节能"}, actor=DISTRICT, at="2026-01-02T09:00")
    backend.register_plot("P1", "一号地块", actor=DISTRICT, at="2026-01-03T09:00")
    backend.register_building("B1", "P1", actor=DEVELOPER, at="2026-01-04T09:00")
    backend.register_building("B2", "P1", actor=DEVELOPER, at="2026-01-04T09:00")
    backend.register_house("H1", "B1", actor=DEVELOPER, at="2026-01-05T09:00")
    backend.register_house("H2", "B1", actor=DEVELOPER, at="2026-01-05T09:00")
    backend.register_house("H3", "B2", actor=DEVELOPER, at="2026-01-05T09:00")
    return backend


def approve_presale(backend: HousingSalesBackend, batch: str = "BATCH1",
                    buildings=("B1", "B2"), disclosure: str = "PUB1",
                    plan_hash: str | None = None) -> str:
    """让一个预售批次达到开盘+公示状态，返回公示编号。"""
    hash_v1 = plan_hash or content_fingerprint({"楼栋": list(buildings), "配套": "养老用房80平"})
    backend.submit_document("建设方案", "PLAN-A", "一号地块建设方案", hash_v1,
                            scope_plot="P1", actor=DEVELOPER, at="2026-02-01T09:00")
    backend.review_document("PLAN-A", 1, True, "同意", actor=DISTRICT, at="2026-02-02T09:00")
    backend.create_batch(batch, "P1", list(buildings), SaleMode.PRESALE,
                         actor=DEVELOPER, at="2026-02-03T09:00")
    for item in ("预售许可证", "建设工程规划许可证", "工程形象进度承诺", "预售资金监管协议"):
        backend.confirm_precondition(batch, item, True, actor=DISTRICT, at="2026-02-04T09:00")
    backend.open_batch(batch, actor=DISTRICT, at="2026-02-05T09:00")
    backend.publish_disclosure(disclosure, batch,
                               {"建设方案": "PLAN-A:1"}, actor=DEVELOPER, at="2026-02-06T09:00")
    return disclosure


class VersionChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = build_world()

    def test_duplicate_submission_reuses_approval(self) -> None:
        """内容指纹相同=重复报送，沿用原审批结果，不产生待审版本。"""
        same = content_fingerprint({"楼栋": ["B1", "B2"], "配套": "养老用房80平"})
        first = self.backend.submit_document(
            "建设方案", "PLAN-A", "方案", same, scope_plot="P1",
            actor=DEVELOPER, at="2026-02-01T09:00")
        self.assertEqual(first.version, 1)
        self.backend.review_document("PLAN-A", 1, True, "同意", actor=DISTRICT, at="2026-02-02T09:00")

        echoed = self.backend.submit_document(
            "建设方案", "PLAN-A", "方案", same, scope_plot="P1",
            actor=DEVELOPER, at="2026-02-10T09:00")
        # 返回的就是原版本，状态仍为已批准，最大版本仍为1。
        self.assertEqual(echoed.version, 1)
        state = self.backend._state()
        self.assertEqual(set(state.docs["PLAN-A"]["versions"]), {1})
        self.assertEqual(echoed.status.value, "已批准")

    def test_content_change_creates_new_pending_version(self) -> None:
        """内容变化形成新的待审版本，且不能覆盖旧版本。"""
        approve_presale(self.backend)
        hash_v2 = content_fingerprint({"楼栋": ["B1", "B2"], "配套": "养老用房120平"})
        v2 = self.backend.submit_document(
            "建设方案", "PLAN-A", "方案v2", hash_v2, scope_plot="P1",
            actor=DEVELOPER, at="2026-03-01T09:00")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.status.value, "待审")
        state = self.backend._state()
        self.assertEqual(state.docs["PLAN-A"]["versions"][1].status.value, "已批准")
        self.assertEqual(state.docs["PLAN-A"]["versions"][2].status.value, "待审")


class DisclosureFreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = build_world()
        approve_presale(self.backend)

    def test_sign_freeze_snapshot_survives_later_version(self) -> None:
        """签约冻结的公示快照不被之后批准的新版本改变。"""
        self.backend.sign_contract("H1", "HT-001", "PUB1",
                                   actor=DEVELOPER, at="2026-02-08T10:00")
        # 之后批准方案v2并换版公示
        hash_v2 = content_fingerprint({"楼栋": ["B1", "B2"], "配套": "养老用房120平"})
        self.backend.submit_document("建设方案", "PLAN-A", "方案v2", hash_v2,
                                     scope_plot="P1", actor=DEVELOPER, at="2026-03-01T09:00")
        self.backend.review_document("PLAN-A", 2, True, "同意调整", actor=DISTRICT, at="2026-03-02T09:00")
        self.backend.publish_disclosure("PUB2", "BATCH1", {"建设方案": "PLAN-A:2"},
                                        actor=DEVELOPER, at="2026-03-03T09:00")

        report = self.backend.cross_check_contract("H1")
        self.assertEqual(report["contract_code"], "HT-001")
        self.assertEqual(report["disclosure_code"], "PUB1")
        frozen = report["frozen_docs"][0]
        self.assertEqual(frozen["signed_version"], 1)
        self.assertEqual(frozen["current_version"], 2)
        self.assertTrue(frozen["changed_after_signing"])
        # 冻结的旧版仍标记冻结，新版公示是当前展示版
        on_display = self.backend.disclosure_on_display("BATCH1", "2026-03-04T09:00")
        assert on_display is not None
        self.assertEqual(on_display.code, "PUB2")
        on_display_old = self.backend.disclosure_on_display("BATCH1", "2026-02-08T10:00")
        assert on_display_old is not None
        self.assertEqual(on_display_old.code, "PUB1")

    def test_cannot_sign_against_withdrawn_disclosure(self) -> None:
        """签约时该版公示已撤换的，不能作为签约依据。"""
        hash_v2 = content_fingerprint({"公共配套": "新的内容"})
        self.backend.submit_document("公共配套", "FACI-A", "配套v2", hash_v2,
                                     scope_plot="P1", actor=DEVELOPER, at="2026-03-01T09:00")
        self.backend.review_document("FACI-A", 1, True, "同意", actor=DISTRICT, at="2026-03-02T09:00")
        self.backend.publish_disclosure("PUB2", "BATCH1",
                                        {"建设方案": "PLAN-A:1", "公共配套": "FACI-A:1"},
                                        actor=DEVELOPER, at="2026-03-03T09:00")
        with self.assertRaisesRegex(DomainError, "已不在销售现场展示"):
            self.backend.sign_contract("H1", "HT-001", "PUB1",
                                       actor=DEVELOPER, at="2026-03-04T09:00")

    def test_disclosure_only_references_approved_versions(self) -> None:
        hash_v2 = content_fingerprint({"x": 1})
        self.backend.submit_document("建设方案", "PLAN-A", "v2", hash_v2,
                                     scope_plot="P1", actor=DEVELOPER, at="2026-03-01T09:00")
        with self.assertRaisesRegex(DomainError, "已批准版本"):
            self.backend.publish_disclosure("PUB-BAD", "BATCH1", {"建设方案": "PLAN-A:2"},
                                            actor=DEVELOPER, at="2026-03-02T09:00")

    def test_dispute_evidence_matches_contract_freeze(self) -> None:
        """还原开篇场景：签约当天现场公示与合同冻结快照一致；
        开发企业事后引用的"后来批准的方案"被识别为签约之后才出现。"""
        self.backend.sign_contract("H1", "HT-001", "PUB1",
                                   actor=DEVELOPER, at="2026-02-08T10:00")
        signed_at = "2026-02-08T10:00"

        # 签约当天销售现场正在展示的公示（区级可独立举证）
        on_display = self.backend.disclosure_on_display("BATCH1", signed_at)
        assert on_display is not None
        self.assertEqual(on_display.code, "PUB1")

        # 合同冻结的材料版本（签约事件内的快照）
        record = self.backend.house_record("H1", as_of=signed_at)
        frozen = record["contract"]["frozen_docs"]["建设方案"]
        self.assertEqual(frozen["version"], 1)
        self.assertEqual(on_display.doc_refs["建设方案"]["version"], 1)
        self.assertEqual(on_display.doc_refs["建设方案"]["content_hash"], frozen["content_hash"])

        # 之后才批准 v2，不能用来解释签约时的合同
        hash_v2 = content_fingerprint({"楼栋": ["B1"], "配套": "养老用房120平"})
        self.backend.submit_document("建设方案", "PLAN-A", "v2", hash_v2,
                                     scope_plot="P1", actor=DEVELOPER, at="2026-04-01T09:00")
        self.backend.review_document("PLAN-A", 2, True, "后来批准",
                                     actor=DISTRICT, at="2026-04-02T09:00")
        check = self.backend.cross_check_contract("H1")
        self.assertTrue(check["frozen_docs"][0]["changed_after_signing"])
        self.assertEqual(check["frozen_docs"][0]["signed_version"], 1)
        self.assertEqual(check["frozen_docs"][0]["current_version"], 2)


class PresaleSpotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = build_world()

    def _make_batch(self, mode: SaleMode, buildings=("B1",), batch="BX") -> None:
        if mode == SaleMode.PRESALE:
            hash1 = content_fingerprint({"m": "presale"})
            self.backend.submit_document("建设方案", "PL", "方案", hash1,
                                         scope_plot="P1", actor=DEVELOPER, at="2026-02-01T09:00")
            self.backend.review_document("PL", 1, True, "ok", actor=DISTRICT, at="2026-02-02T09:00")
        self.backend.create_batch(batch, "P1", list(buildings), mode,
                                  actor=DEVELOPER, at="2026-02-03T09:00")

    def test_presale_wrong_precondition_rejected(self) -> None:
        self._make_batch(SaleMode.PRESALE)
        with self.assertRaisesRegex(DomainError, "不存在前置条件"):
            self.backend.confirm_precondition("BX", "竣工验收备案", True,
                                              actor=DISTRICT, at="2026-02-04T09:00")

    def test_spot_requires_own_items_and_survey(self) -> None:
        # 现售前置里没有预售许可证
        self._make_batch(SaleMode.SPOT)
        with self.assertRaisesRegex(DomainError, "不存在前置条件"):
            self.backend.confirm_precondition("BX", "预售许可证", True,
                                              actor=DISTRICT, at="2026-02-04T09:00")
        # 四项现售前置齐全，但无已批准测绘成果 -> 仍不能开盘
        for item in ("竣工验收备案", "不动产权属证明", "建设工程规划许可证", "房屋测绘成果确认"):
            self.backend.confirm_precondition("BX", item, True, actor=DISTRICT, at="2026-02-04T09:00")
        with self.assertRaisesRegex(DomainError, "测绘成果"):
            self.backend.open_batch("BX", actor=DISTRICT, at="2026-02-05T09:00")
        # 只有覆盖该楼栋的已批准测绘成果才能开盘
        survey_hash = content_fingerprint({"building": "B1", "area": 100.0})
        self.backend.submit_document("测绘成果", "SUR-B1", "B1测绘", survey_hash,
                                     scope_buildings=["B1"], actor=SURVEYOR, at="2026-02-06T09:00")
        with self.assertRaisesRegex(DomainError, "已批准版本|测绘成果"):
            # 测绘成果尚待审，仍然不能开盘
            self.backend.open_batch("BX", actor=DISTRICT, at="2026-02-07T09:00")
        self.backend.review_document("SUR-B1", 1, True, "认可", actor=DISTRICT, at="2026-02-08T09:00")
        self.backend.open_batch("BX", actor=DISTRICT, at="2026-02-09T09:00")

    def test_presale_cannot_open_without_approved_plan(self) -> None:
        store = EventStore()
        backend = HousingSalesBackend.bootstrap(store, DISTRICT, "区管", "2026-01-01T09:00")
        backend.register_actor(DEVELOPER, "开发商", Role.DEVELOPER, actor=DISTRICT, at="2026-01-02T09:00")
        backend.register_plot("P1", "地块", actor=DISTRICT, at="2026-01-03T09:00")
        backend.register_building("B1", "P1", actor=DEVELOPER, at="2026-01-04T09:00")
        backend.create_batch("B", "P1", ["B1"], SaleMode.PRESALE,
                             actor=DEVELOPER, at="2026-02-03T09:00")
        for item in ("预售许可证", "建设工程规划许可证", "工程形象进度承诺", "预售资金监管协议"):
            backend.confirm_precondition("B", item, True, actor=DISTRICT, at="2026-02-04T09:00")
        with self.assertRaisesRegex(DomainError, "建设方案"):
            backend.open_batch("B", actor=DISTRICT, at="2026-02-05T09:00")


class InspectionRectificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = build_world()

    def test_inspector_only_confirms_own_items(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.backend.record_inspection("I1", "B1", "节能", InspectionResult.PASS, "ok",
                                           actor=INSPECTOR_A, at="2026-04-01T09:00")
        # 李负责消防，可以确认
        self.backend.record_inspection("I1", "B1", "消防", InspectionResult.PASS, "合格",
                                       actor=INSPECTOR_A, at="2026-04-01T09:00")

    def test_developer_cannot_approve_rectification(self) -> None:
        self.backend.record_inspection("I2", "B1", "消防", InspectionResult.ISSUE_FOUND,
                                       "消防通道占用", actor=INSPECTOR_A, at="2026-04-02T09:00",
                                       due_at="2026-04-20T09:00")
        self.backend.submit_rectification("R-I2", "已清理并拍照",
                                          actor=DEVELOPER, at="2026-04-10T09:00")
        with self.assertRaises(AuthorizationError):
            self.backend.review_rectification("R-I2", True, "整改合格",
                                              actor=DEVELOPER, at="2026-04-11T09:00")
        # 检查人员也不能批准整改结论
        with self.assertRaises(AuthorizationError):
            self.backend.review_rectification("R-I2", True, "整改合格",
                                              actor=INSPECTOR_A, at="2026-04-11T09:00")
        # 区级管理人员批准后，另生成一条持久化的待验收事项
        self.backend.review_rectification("R-I2", True, "复核通过",
                                          actor=DISTRICT, at="2026-04-12T09:00")
        state = self.backend._state()
        acceptance = state.acceptances["A-R-I2"]
        self.assertEqual(acceptance.status.value, "待验收")
        self.backend.complete_acceptance("A-R-I2", "验收合格",
                                         actor=DISTRICT, at="2026-05-01T09:00")
        self.assertEqual(self.backend._state().acceptances["A-R-I2"].status.value, "已验收")

    def test_overdue_is_persisted_and_re_submission_allowed(self) -> None:
        """到期未整改被持久记录，之后仍可重新报送并由区级审批。"""
        self.backend.record_inspection("I3", "B1", "配套用房", InspectionResult.ISSUE_FOUND,
                                       "养老用房未落实", actor=INSPECTOR_A, at="2026-04-02T09:00",
                                       due_at="2026-04-20T09:00")
        marked = self.backend.sweep_overdue("2026-04-21T09:00", actor=DISTRICT)
        self.assertEqual(marked, ["R-I3"])
        state = self.backend._state()
        self.assertEqual(state.rectifications["R-I3"].status.value, "到期未整改")
        # 重新报送 -> 待审 -> 区级不批准 -> 可再次报送
        self.backend.submit_rectification("R-I3", "正在协调用房",
                                          actor=DEVELOPER, at="2026-04-25T09:00")
        self.backend.review_rectification("R-I3", False, "证据不足",
                                          actor=DISTRICT, at="2026-04-26T09:00")
        self.backend.submit_rectification("R-I3", "已移交用房清单",
                                          actor=DEVELOPER, at="2026-05-02T09:00")
        self.backend.review_rectification("R-I3", True, "通过",
                                          actor=DISTRICT, at="2026-05-03T09:00")
        record = self.backend.house_record("H1")
        statuses = [e["status"] for e in record["rectifications"] if e["type"] == "RectificationReviewed"]
        self.assertIn("整改不通过", statuses)
        self.assertIn("整改通过", statuses)
        overdue_events = [e for e in record["rectifications"] if e["type"] == "RectificationOverdue"]
        self.assertTrue(overdue_events)


class ChangeScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = build_world()
        approve_presale(self.backend)
        self.backend.sign_contract("H1", "HT-001", "PUB1",
                                   actor=DEVELOPER, at="2026-02-08T10:00")
        # H2 未签约；B2 的 H3 未签约且不在方案的楼栋范围语义里仅用于验证其他楼栋
        hash_v2 = content_fingerprint({"楼栋": ["B1"], "配套": "养老用房120平"})
        self.backend.submit_document("建设方案", "PLAN-A", "方案v2", hash_v2,
                                     scope_plot="P1", actor=DEVELOPER, at="2026-03-01T09:00")
        self.backend.review_document("PLAN-A", 2, True, "同意", actor=DISTRICT, at="2026-03-02T09:00")
        self.backend.register_change("CHG1", "建设方案", "PLAN-A", 2,
                                     actor=DEVELOPER, at="2026-03-03T09:00")

    def test_change_applies_only_to_unsigned(self) -> None:
        with self.assertRaisesRegex(DomainError, "已签约"):
            self.backend.apply_change_to_unsigned("CHG1", ["H1"],
                                                  actor=DEVELOPER, at="2026-03-04T09:00")
        self.backend.apply_change_to_unsigned("CHG1", ["H2"],
                                              actor=DEVELOPER, at="2026-03-04T09:00")
        self.assertEqual(self.backend._state().changes["CHG1"].applied_to, ["H2"])

    def test_signed_house_links_via_supplement_rectification_or_dispute(self) -> None:
        self.backend.link_signed_house("H1", ChangeEffect.SUPPLEMENT, "签订补充协议增加补偿",
                                       change_code="CHG1", actor=DISTRICT, at="2026-03-05T09:00")
        self.assertEqual(self.backend.unlinked_signed_houses("CHG1"), [])
        record = self.backend.house_record("H1")
        self.assertEqual(record["signed_links"][0]["effect"], "补充协议")
        # 争议处理路径
        code = self.backend.record_dispute("H1", "购房人对配套调整有异议",
                                           actor=DEVELOPER, at="2026-03-06T09:00")
        self.backend.link_signed_house("H1", ChangeEffect.DISPUTE, f"进入{code}",
                                       change_code="CHG1", actor=DISTRICT, at="2026-03-07T09:00")
        self.backend.resolve_dispute(code, "按补充协议执行", actor=DISTRICT, at="2026-03-20T09:00")
        record = self.backend.house_record("H1")
        self.assertEqual(len(record["disputes"]), 2)

    def test_change_does_not_block_other_buildings(self) -> None:
        """资料冲突只暂停涉事楼栋；其他楼栋房源仍可签约。"""
        self.backend.suspend_building("B1", "建设方案与测绘口径冲突",
                                      actor=DISTRICT, at="2026-03-08T09:00")
        with self.assertRaisesRegex(DomainError, "暂停销售"):
            self.backend.sign_contract("H2", "HT-002", "PUB1",
                                       actor=DEVELOPER, at="2026-03-09T09:00")
        # B2 不在暂停范围，给 B2 发一个批次公示即可签约——BATCH1 含 B2
        self.backend.sign_contract("H3", "HT-003", "PUB1",
                                   actor=DEVELOPER, at="2026-03-09T10:00")
        # 解除暂停后 B1 未签约房源恢复
        self.backend.resume_building("B1", actor=DISTRICT, at="2026-03-10T09:00")
        self.backend.sign_contract("H2", "HT-002", "PUB1",
                                   actor=DEVELOPER, at="2026-03-11T09:00")


class HistoryReconstructionTest(unittest.TestCase):
    def test_as_of_reconstructs_signed_basis_and_later_changes(self) -> None:
        backend = build_world()
        approve_presale(backend)
        backend.sign_contract("H1", "HT-001", "PUB1", actor=DEVELOPER, at="2026-02-08T10:00")
        backend.record_inspection("I9", "B1", "消防", InspectionResult.ISSUE_FOUND,
                                  "隐患", actor=INSPECTOR_A, at="2026-04-01T09:00",
                                  due_at="2026-04-15T09:00")
        backend.sweep_overdue("2026-04-16T09:00", actor=DISTRICT)
        backend.submit_rectification("R-I9", "整改证据", actor=DEVELOPER, at="2026-04-18T09:00")
        backend.review_rectification("R-I9", True, "通过", actor=DISTRICT, at="2026-04-20T09:00")
        # 待验收事项尚未完成

        # 签约次日：能还原公示与合同，之后的检查/变更都不出现
        early = backend.house_record("H1", as_of="2026-02-09T09:00")
        self.assertTrue(early["signed"])
        self.assertEqual(early["contract"]["frozen_docs"]["建设方案"]["version"], 1)
        self.assertEqual(early["disclosures"][0]["code"], "PUB1")
        self.assertEqual(early["inspections"], [])
        self.assertEqual(early["pending_acceptances"], [])

        # 2026-05-01：检查、到期记录、整改通过、待验收都能还原
        later = backend.house_record("H1", as_of="2026-05-01T09:00")
        self.assertEqual(len(later["inspections"]), 1)
        overdue = [e for e in later["rectifications"] if e["type"] == "RectificationOverdue"]
        self.assertEqual(len(overdue), 1)
        accepted = [e for e in later["pending_acceptances"] if e["type"] == "PendingAcceptanceCreated"]
        self.assertEqual(len(accepted), 1)
        done = [e for e in later["pending_acceptances"] if e["type"] == "PendingAcceptanceDone"]
        self.assertEqual(done, [])

        # 完成验收后再还原，能看到完成事件
        backend.complete_acceptance("A-R-I9", "验收通过", actor=DISTRICT, at="2026-05-02T09:00")
        final = backend.house_record("H1", as_of="2026-05-03T09:00")
        done = [e for e in final["pending_acceptances"] if e["type"] == "PendingAcceptanceDone"]
        self.assertEqual(len(done), 1)

    def test_persistence_across_restart(self) -> None:
        """JSONL 落盘后重启，到期整改、待验收、冻结快照全部保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            backend = HousingSalesBackend.bootstrap(store, DISTRICT, "区管", "2026-01-01T09:00")
            backend.register_actor(DEVELOPER, "开发商", Role.DEVELOPER,
                                   actor=DISTRICT, at="2026-01-02T09:00")
            backend.register_actor(INSPECTOR_A, "检查员", Role.INSPECTOR,
                                   responsible_items={"消防"}, actor=DISTRICT, at="2026-01-02T09:00")
            backend.register_plot("P1", "地块", actor=DISTRICT, at="2026-01-03T09:00")
            backend.register_building("B1", "P1", actor=DEVELOPER, at="2026-01-04T09:00")
            backend.register_house("H1", "B1", actor=DEVELOPER, at="2026-01-05T09:00")
            approve_presale(backend, buildings=("B1",))
            backend.sign_contract("H1", "HT-X", "PUB1", actor=DEVELOPER, at="2026-02-08T10:00")
            backend.record_inspection("I1", "B1", "消防", InspectionResult.ISSUE_FOUND,
                                      "问题", actor=INSPECTOR_A, at="2026-04-01T09:00",
                                      due_at="2026-04-10T09:00")
            backend.sweep_overdue("2026-04-11T09:00", actor=DISTRICT)

            store2 = EventStore(path)  # 重新打开
            backend2 = HousingSalesBackend(store2)
            state = backend2._state()
            self.assertEqual(state.rectifications["R-I1"].status.value, "到期未整改")
            self.assertTrue(state.houses["H1"].signed)
            self.assertEqual(state.houses["H1"].contract_code, "HT-X")
            report = backend2.cross_check_contract("H1")
            self.assertEqual(report["frozen_docs"][0]["signed_version"], 1)

    def test_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            HousingSalesBackend.bootstrap(store, DISTRICT, "区管", "2026-01-01T09:00")
            lines = path.read_text(encoding="utf-8").splitlines()
            import json
            raw = json.loads(lines[0])
            raw["payload"]["name"] = "冒充的管理人员"
            path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(EventStoreError):
                EventStore(path)


class RoleGuardTest(unittest.TestCase):
    def test_developer_cannot_review_plan(self) -> None:
        backend = build_world()
        h = content_fingerprint({"a": 1})
        backend.submit_document("建设方案", "PL", "方案", h, scope_plot="P1",
                                actor=DEVELOPER, at="2026-02-01T09:00")
        with self.assertRaises(AuthorizationError):
            backend.review_document("PL", 1, True, "自己批准",
                                    actor=DEVELOPER, at="2026-02-02T09:00")

    def test_unknown_actor_rejected(self) -> None:
        backend = build_world()
        with self.assertRaises(NotFound):
            backend.register_plot("PX", "x", actor="NOBODY", at="2026-02-01T09:00")


if __name__ == "__main__":
    unittest.main()
