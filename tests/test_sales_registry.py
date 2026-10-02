"""商品住房销售管理后端记录的行为校验。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.sales_registry import (
    ConflictError,
    DutyError,
    FrozenError,
    InspectionKind,
    JsonLinesStore,
    PreconditionError,
    RecordError,
    Role,
    SaleMode,
    SalesRegistry,
    SurveyKind,
    UnknownEntityError,
)

D0 = date(2026, 1, 5)
D1 = date(2026, 2, 1)
D2 = date(2026, 3, 1)
D3 = date(2026, 4, 1)
D4 = date(2026, 5, 1)
D5 = date(2026, 6, 1)


def base_registry(store=None) -> SalesRegistry:
    registry = SalesRegistry(store=store)
    for actor_id, role in (
        ("reg-1", Role.REGULATOR),
        ("dev-1", Role.DEVELOPER),
        ("insp-1", Role.INSPECTOR),
        ("insp-2", Role.INSPECTOR),
        ("surv-1", Role.SURVEYOR),
    ):
        registry.register_actor(actor_id, role, f"{actor_id}-姓名", D0)
    registry.register_parcel("P1", "示例地块", D0)
    registry.register_building("B1", "P1", "1号楼", D0)
    registry.register_building("B2", "P1", "2号楼", D0)
    registry.register_unit("U101", "B1", "101", D0)
    registry.register_unit("U102", "B1", "102", D0)
    registry.register_unit("U201", "B2", "201", D0)
    return registry


def plan_content(areas=None, facilities=None):
    return {
        "facilities": facilities
        if facilities is not None
        else [{"name": "幼儿园", "area": 1200.0}],
        "unit_areas": areas
        if areas is not None
        else {"U101": 90.0, "U102": 85.0, "U201": 95.0},
    }


def approved_plan(registry, content=None, on=D1):
    view = registry.submit_plan("PL1", "P1", content or plan_content(), on, "dev-1")
    registry.review_plan("PL1", view.version_no, True, "reg-1", on, "同意")
    return view


def approved_survey(
    registry, building, areas, kind=SurveyKind.PREDICTED, on=D1, survey_id=None
):
    survey_id = survey_id or f"SV-{building}-{kind.value}"
    view = registry.submit_survey(survey_id, building, kind, areas, on, "surv-1")
    registry.review_survey(survey_id, view.version_no, True, "reg-1", on, "同意")
    return view


def presale_ready(registry, on=D1):
    approved_plan(registry, on=on)
    approved_survey(registry, "B1", {"U101": 90.0, "U102": 85.0}, on=on)
    approved_survey(registry, "B2", {"U201": 95.0}, on=on)


def presale_with_contract(registry, on_batch=D2, on_sign=D3):
    presale_ready(registry)
    registry.open_batch("BA1", ["B1"], SaleMode.PRESALE, on_batch, "reg-1")
    disclosure = registry.publish_disclosure("GS1", "BA1", on_batch, "dev-1")
    contract = registry.sign_contract(
        "C1",
        "U101",
        "BA1",
        "buyer-001",
        "GS1",
        on_sign,
        "dev-1",
        attachments=[{"name": "户型图", "content": {"unit": "U101", "layout": "两室"}}],
    )
    return disclosure, contract


class VersionChainTest(unittest.TestCase):
    def test_frozen_disclosure_cross_checks_with_unit_and_contract(self) -> None:
        registry = base_registry()
        disclosure, contract = presale_with_contract(registry)
        self.assertEqual(contract.disclosure_hash, disclosure.content_hash)
        self.assertEqual(contract.plan_ref, ("PL1", 1))
        self.assertEqual(contract.survey_refs["B1"], ("SV-B1-predicted", 1))
        check = registry.verify_contract("C1")
        self.assertTrue(check.ok)
        self.assertEqual(check.problems, ())
        content = check.disclosure.content
        self.assertEqual(content["plan"]["content"]["facilities"][0]["name"], "幼儿园")
        self.assertEqual(content["surveys"]["B1"]["areas"]["U101"], 90.0)
        self.assertIn("U101", content["units"])
        names = [item["name"] for item in registry.get_contract("C1").attachments]
        self.assertEqual(names, ["户型图"])

    def test_repeated_submission_reuses_approval_and_change_forms_pending(self) -> None:
        registry = base_registry()
        first = approved_plan(registry)
        self.assertEqual(first.status, "pending")
        self.assertEqual(registry.get_plan("PL1", 1).status, "approved")
        again = registry.submit_plan("PL1", "P1", plan_content(), D2, "dev-1")
        self.assertEqual(again.status, "approved")
        self.assertEqual(again.reused_from, ("PL1", 1))
        changed = registry.submit_plan(
            "PL1",
            "P1",
            plan_content(facilities=[{"name": "幼儿园", "area": 1500.0}]),
            D3,
            "dev-1",
        )
        self.assertEqual(changed.status, "pending")
        self.assertIsNone(changed.reused_from)
        registry.review_plan("PL1", changed.version_no, True, "reg-1", D3, "同意变更")
        self.assertEqual(registry.get_plan("PL1").version_no, 3)

    def test_survey_resubmission_reuses_approval(self) -> None:
        registry = base_registry()
        approved_survey(registry, "B1", {"U101": 90.0, "U102": 85.0})
        again = registry.submit_survey(
            "SV-B1-predicted", "B1", SurveyKind.PREDICTED, {"U101": 90.0, "U102": 85.0}, D2, "surv-1"
        )
        self.assertEqual(again.status, "approved")
        self.assertEqual(again.reused_from, ("SV-B1-predicted", 1))
        changed = registry.submit_survey(
            "SV-B1-predicted", "B1", SurveyKind.PREDICTED, {"U101": 90.5, "U102": 85.0}, D3, "surv-1"
        )
        self.assertEqual(changed.status, "pending")

    def test_rejected_plan_resubmission_is_pending_again(self) -> None:
        registry = base_registry()
        view = registry.submit_plan("PL1", "P1", plan_content(), D1, "dev-1")
        registry.review_plan("PL1", view.version_no, False, "reg-1", D1, "退回")
        again = registry.submit_plan("PL1", "P1", plan_content(), D2, "dev-1")
        self.assertEqual(again.status, "pending")


class PreconditionTest(unittest.TestCase):
    def test_presale_requires_approved_plan_and_predicted_survey(self) -> None:
        registry = base_registry()
        with self.assertRaisesRegex(PreconditionError, "建设方案"):
            registry.open_batch("BA1", ["B1"], SaleMode.PRESALE, D1, "reg-1")
        approved_plan(registry)
        with self.assertRaisesRegex(PreconditionError, "预测绘"):
            registry.open_batch("BA1", ["B1"], SaleMode.PRESALE, D2, "reg-1")
        approved_survey(registry, "B1", {"U101": 90.0, "U102": 85.0}, on=D2)
        registry.open_batch("BA1", ["B1"], SaleMode.PRESALE, D3, "reg-1")

    def test_presale_blocked_by_overdue_rectification(self) -> None:
        registry = base_registry()
        presale_ready(registry)
        registry.open_inspection(
            "INSP-1", "B1", InspectionKind.ROUTINE, [("外立面", "insp-1")], D2, "reg-1"
        )
        registry.confirm_item(
            "INSP-1", "外立面", "insp-1", False, "空鼓", D2, rectification_deadline=D3
        )
        with self.assertRaisesRegex(PreconditionError, "整改"):
            registry.open_batch("BA1", ["B1"], SaleMode.PRESALE, D4, "reg-1")

    def test_existing_sale_requires_final_survey_and_acceptance(self) -> None:
        registry = base_registry()
        approved_plan(registry)
        approved_survey(registry, "B1", {"U101": 90.0, "U102": 85.0})
        with self.assertRaisesRegex(PreconditionError, "实测"):
            registry.open_batch("BA2", ["B1"], SaleMode.EXISTING, D2, "reg-1")
        approved_survey(
            registry,
            "B1",
            {"U101": 90.1, "U102": 85.0},
            kind=SurveyKind.FINAL,
            on=D2,
        )
        with self.assertRaisesRegex(PreconditionError, "验收"):
            registry.open_batch("BA2", ["B1"], SaleMode.EXISTING, D3, "reg-1")
        registry.open_inspection(
            "INSP-A", "B1", InspectionKind.ACCEPTANCE, [("竣工质量", "insp-1")], D3, "reg-1"
        )
        pending = registry.pending_acceptance_items()
        self.assertEqual([item.item_key for item in pending], ["竣工质量"])
        with self.assertRaisesRegex(PreconditionError, "验收"):
            registry.open_batch("BA2", ["B1"], SaleMode.EXISTING, D4, "reg-1")
        registry.confirm_item("INSP-A", "竣工质量", "insp-1", True, "合格", D4)
        self.assertEqual(registry.pending_acceptance_items(), ())
        registry.open_batch("BA2", ["B1"], SaleMode.EXISTING, D5, "reg-1")


class DutyTest(unittest.TestCase):
    def test_inspector_confirms_only_own_items(self) -> None:
        registry = base_registry()
        registry.open_inspection(
            "INSP-1",
            "B1",
            InspectionKind.ROUTINE,
            [("结构", "insp-1"), ("消防", "insp-2")],
            D1,
            "reg-1",
        )
        with self.assertRaisesRegex(DutyError, "本人负责"):
            registry.confirm_item("INSP-1", "结构", "insp-2", True, "合格", D2)
        with self.assertRaisesRegex(DutyError, "检查人员"):
            registry.confirm_item("INSP-1", "结构", "dev-1", True, "合格", D2)
        registry.confirm_item("INSP-1", "结构", "insp-1", True, "合格", D2)
        view = registry.get_inspection("INSP-1")
        self.assertEqual(view.items[0].status, "passed")
        self.assertEqual(view.items[0].confirmed_by, "insp-1")

    def test_developer_cannot_approve_rectification(self) -> None:
        registry = base_registry()
        registry.open_inspection(
            "INSP-1", "B1", InspectionKind.ROUTINE, [("外立面", "insp-1")], D1, "reg-1"
        )
        registry.confirm_item(
            "INSP-1", "外立面", "insp-1", False, "空鼓", D1, rectification_deadline=D3
        )
        registry.submit_rectification("INSP-1", "外立面", {"photos": ["p1"]}, D2, "dev-1")
        with self.assertRaisesRegex(DutyError, "不能自批"):
            registry.close_rectification("INSP-1", "外立面", True, "dev-1", D2)
        registry.close_rectification("INSP-1", "外立面", True, "reg-1", D2, "复查合格")
        self.assertEqual(registry.get_inspection("INSP-1").items[0].status, "rectified")

    def test_rejected_rectification_can_be_resubmitted(self) -> None:
        registry = base_registry()
        registry.open_inspection(
            "INSP-1", "B1", InspectionKind.ROUTINE, [("外立面", "insp-1")], D1, "reg-1"
        )
        registry.confirm_item(
            "INSP-1", "外立面", "insp-1", False, "空鼓", D1, rectification_deadline=D3
        )
        registry.submit_rectification("INSP-1", "外立面", {"photos": ["p1"]}, D2, "dev-1")
        registry.close_rectification("INSP-1", "外立面", False, "reg-1", D2, "整改不到位")
        self.assertEqual(registry.get_inspection("INSP-1").items[0].status, "rectifying")
        registry.submit_rectification("INSP-1", "外立面", {"photos": ["p2"]}, D3, "dev-1")
        registry.close_rectification("INSP-1", "外立面", True, "reg-1", D3, "复查合格")
        self.assertEqual(registry.get_inspection("INSP-1").items[0].status, "rectified")

    def test_plan_review_requires_regulator(self) -> None:
        registry = base_registry()
        registry.submit_plan("PL1", "P1", plan_content(), D1, "dev-1")
        with self.assertRaisesRegex(DutyError, "住房管理部门"):
            registry.review_plan("PL1", 1, True, "dev-1", D1)


class RectificationPersistenceTest(unittest.TestCase):
    def test_overdue_rectifications_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            registry = base_registry(store=JsonLinesStore(path))
            registry.open_inspection(
                "INSP-1", "B1", InspectionKind.ROUTINE, [("外立面", "insp-1")], D1, "reg-1"
            )
            registry.confirm_item(
                "INSP-1", "外立面", "insp-1", False, "空鼓", D1, rectification_deadline=D2
            )
            registry.open_inspection(
                "INSP-A", "B1", InspectionKind.ACCEPTANCE, [("竣工质量", "insp-2")], D1, "reg-1"
            )
            restored = SalesRegistry.load(path)
            overdue = restored.overdue_rectifications(D3)
            self.assertEqual([item.item_key for item in overdue], ["外立面"])
            self.assertEqual(overdue[0].deadline, D2)
            pending = restored.pending_acceptance_items(D3)
            self.assertEqual([item.item_key for item in pending], ["竣工质量"])
            restored.submit_rectification("INSP-1", "外立面", {"photos": ["p1"]}, D3, "dev-1")
            restored.close_rectification("INSP-1", "外立面", True, "reg-1", D3)
            again = SalesRegistry.load(path)
            self.assertEqual(again.overdue_rectifications(D4), ())


class ChangeScopeTest(unittest.TestCase):
    def test_signed_unit_requires_agreement_for_changes(self) -> None:
        registry = base_registry()
        presale_with_contract(registry)
        changed = registry.submit_plan(
            "PL1",
            "P1",
            plan_content(facilities=[{"name": "幼儿园", "area": 1500.0}]),
            D4,
            "dev-1",
        )
        registry.review_plan("PL1", changed.version_no, True, "reg-1", D4, "同意变更")
        with self.assertRaisesRegex(FrozenError, "补充协议"):
            registry.rebase_unit("U101", D5, "reg-1", plan_ref=("PL1", 2))
        registry.rebase_unit("U102", D5, "reg-1", plan_ref=("PL1", 2))
        registry.sign_agreement(
            "XY1", "C1", {"change": "配套面积调整", "plan": ["PL1", 2]}, D5, "dev-1"
        )
        registry.rebase_unit("U101", D5, "reg-1", plan_ref=("PL1", 2), agreement_id="XY1")
        with self.assertRaisesRegex(PreconditionError, "无需补充协议"):
            registry.rebase_unit("U102", D5, "reg-1", plan_ref=("PL1", 2), agreement_id="XY1")
        registry.sign_contract("C2", "U102", "BA1", "buyer-002", "GS1", D5, "dev-1")
        with self.assertRaisesRegex(FrozenError, "不一致"):
            registry.rebase_unit("U102", D5, "reg-1", plan_ref=("PL1", 2), agreement_id="XY1")

    def test_new_disclosure_does_not_rewrite_signed_contract(self) -> None:
        registry = base_registry()
        disclosure, contract = presale_with_contract(registry)
        changed = registry.submit_plan(
            "PL1",
            "P1",
            plan_content(facilities=[{"name": "幼儿园", "area": 1500.0}]),
            D4,
            "dev-1",
        )
        registry.review_plan("PL1", changed.version_no, True, "reg-1", D4, "同意变更")
        newer = registry.publish_disclosure("GS2", "BA1", D4, "dev-1")
        self.assertNotEqual(newer.content_hash, disclosure.content_hash)
        check = registry.verify_contract("C1")
        self.assertTrue(check.ok)
        self.assertEqual(check.disclosure.content_hash, disclosure.content_hash)
        follow = registry.sign_contract("C2", "U102", "BA1", "buyer-002", "GS2", D5, "dev-1")
        self.assertEqual(follow.disclosure_hash, newer.content_hash)


class SuspensionTest(unittest.TestCase):
    def test_building_conflict_pauses_only_that_building(self) -> None:
        registry = base_registry()
        presale_ready(registry)
        registry.open_batch("BA1", ["B1", "B2"], SaleMode.PRESALE, D2, "reg-1")
        registry.publish_disclosure("GS1", "BA1", D2, "dev-1")
        approved_survey(registry, "B1", {"U101": 92.0, "U102": 85.0}, on=D3)
        conflicts = registry.check_building_consistency("B1", D3)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].unit_id, "U101")
        self.assertAlmostEqual(conflicts[0].difference, 2.0)
        self.assertFalse(registry.is_sellable("U101"))
        self.assertFalse(registry.is_sellable("U102"))
        self.assertTrue(registry.is_sellable("U201"))
        with self.assertRaisesRegex(PreconditionError, "暂停"):
            registry.sign_contract("C1", "U101", "BA1", "buyer-001", "GS1", D4, "dev-1")
        contract = registry.sign_contract("C2", "U201", "BA1", "buyer-002", "GS1", D4, "dev-1")
        self.assertEqual(contract.unit_id, "U201")
        registry.resume_building("B1", D5, "reg-1")
        self.assertTrue(registry.is_sellable("U101"))

    def test_consistent_building_is_not_suspended(self) -> None:
        registry = base_registry()
        presale_ready(registry)
        conflicts = registry.check_building_consistency("B2", D2)
        self.assertEqual(conflicts, ())
        self.assertTrue(registry.is_sellable("U201"))


class HistoryTest(unittest.TestCase):
    def test_restore_unit_by_historical_date(self) -> None:
        registry = base_registry()
        disclosure, _ = presale_with_contract(registry)
        changed = registry.submit_plan(
            "PL1",
            "P1",
            plan_content(facilities=[{"name": "幼儿园", "area": 1500.0}]),
            D4,
            "dev-1",
        )
        registry.review_plan("PL1", changed.version_no, True, "reg-1", D4, "同意变更")
        registry.publish_disclosure("GS2", "BA1", D4, "dev-1")
        registry.sign_agreement(
            "XY1", "C1", {"change": "配套面积调整"}, D5, "dev-1",
            attachments=[{"name": "补充协议一", "content": {"change": "配套面积调整"}}],
        )
        registry.rebase_unit("U101", D5, "reg-1", plan_ref=("PL1", 2), agreement_id="XY1")

        at_signing = registry.unit_history("U101", D3)
        self.assertEqual(at_signing.disclosure.content_hash, disclosure.content_hash)
        self.assertEqual(
            at_signing.disclosure.content["plan"]["content"]["facilities"][0]["area"], 1200.0
        )
        self.assertIsNotNone(at_signing.contract)
        self.assertEqual(at_signing.contract.agreements, ())
        later_kinds = [event.kind for event in at_signing.subsequent_changes]
        self.assertIn("plan_submitted", later_kinds)
        self.assertIn("disclosure_published", later_kinds)
        self.assertIn("agreement_signed", later_kinds)
        self.assertIn("unit_rebased", later_kinds)

        before_signing = registry.unit_history("U101", D2)
        self.assertIsNone(before_signing.contract)
        self.assertEqual(before_signing.disclosure.content_hash, disclosure.content_hash)

        latest = registry.unit_history("U101", D5)
        self.assertEqual(latest.contract.agreements, ("XY1",))
        self.assertEqual(latest.subsequent_changes, ())
        names = [item["name"] for item in latest.contract.attachments]
        self.assertEqual(names, ["户型图", "补充协议一"])

    def test_history_before_registration_is_rejected(self) -> None:
        registry = base_registry()
        with self.assertRaisesRegex(UnknownEntityError, "尚未登记"):
            registry.unit_history("U101", date(2025, 12, 31))

    def test_history_includes_building_inspection_conclusions(self) -> None:
        registry = base_registry()
        presale_ready(registry)
        registry.open_inspection(
            "INSP-1", "B1", InspectionKind.ROUTINE, [("外立面", "insp-1")], D2, "reg-1"
        )
        registry.confirm_item("INSP-1", "外立面", "insp-1", True, "合格", D3)
        history = registry.unit_history("U101", D4)
        self.assertEqual(len(history.inspections), 1)
        self.assertEqual(history.inspections[0].items[0].conclusion, "合格")
        earlier = registry.unit_history("U101", D2)
        self.assertEqual(earlier.inspections[0].items[0].status, "open")


class IntegrityTest(unittest.TestCase):
    def test_tampered_disclosure_is_detected_by_cross_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            registry = base_registry(store=JsonLinesStore(path))
            presale_with_contract(registry)
            lines = path.read_text(encoding="utf-8").splitlines()
            rewritten = []
            for line in lines:
                raw = json.loads(line)
                if raw["kind"] == "disclosure_published":
                    raw["data"]["content"]["surveys"]["B1"]["areas"]["U101"] = 88.0
                rewritten.append(json.dumps(raw, ensure_ascii=False, sort_keys=True))
            path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            restored = SalesRegistry.load(path)
            check = restored.verify_contract("C1")
            self.assertFalse(check.ok)
            self.assertIn("公示内容与发布时摘要不一致", check.problems)

    def test_contract_rejects_unknown_or_duplicate_targets(self) -> None:
        registry = base_registry()
        presale_with_contract(registry)
        with self.assertRaises(UnknownEntityError):
            registry.sign_contract("C9", "U999", "BA1", "buyer-009", "GS1", D4, "dev-1")
        with self.assertRaisesRegex(ConflictError, "已签约"):
            registry.sign_contract("C8", "U101", "BA1", "buyer-008", "GS1", D4, "dev-1")
        with self.assertRaisesRegex(ConflictError, "已存在"):
            registry.publish_disclosure("GS1", "BA1", D4, "dev-1")

    def test_clock_cannot_move_backwards(self) -> None:
        registry = base_registry()
        with self.assertRaisesRegex(RecordError, "业务日期"):
            registry.register_parcel("P2", "另一地块", date(2025, 1, 1))


if __name__ == "__main__":
    unittest.main()
