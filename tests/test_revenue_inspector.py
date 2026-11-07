import csv
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import revenue_inspector as inspector
from revenue_inspector import InputError, inspect_revenue, load_inputs, main, month_bounds, write_reports

MERCHANTS = [
    {"merchant_id": "1", "timezone": "America/Los_Angeles"},
    {"merchant_id": "2", "timezone": "Asia/Hong_Kong"},
]
RATES = [
    {"currency": "USD", "currency_units_per_usd": "1"},
    {"currency": "HKD", "currency_units_per_usd": "7.8"},
]
FIELDS = {
    "merchants.csv": ["merchant_id", "timezone"],
    "orders.csv": ["order_id", "merchant_id", "amount", "currency", "status", "is_test", "created_at"],
    "attributions.csv": ["order_id", "post_id"],
    "posts.csv": ["post_id", "merchant_id", "source"],
    "reported_revenue.csv": ["merchant_id", "month", "reported_ai_revenue_usd"],
    "fx_rates.csv": ["currency", "currency_units_per_usd"],
}


def order(order_id="100", merchant_id="1", amount="100", currency="USD", status="paid", is_test="0", created_at="2026-09-10T12:00:00Z"):
    return {
        "order_id": order_id,
        "merchant_id": merchant_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "is_test": is_test,
        "created_at": created_at,
    }


def post(post_id="P1", merchant_id="1", source="Reelo"):
    return {"post_id": post_id, "merchant_id": merchant_id, "source": source}


def report(merchant_id="1", month="2026-09", amount="100"):
    return {"merchant_id": merchant_id, "month": month, "reported_ai_revenue_usd": amount}


def write_dataset(root, orders, attributions, posts, reports, merchants=None, rates=None):
    datasets = {
        "merchants.csv": MERCHANTS if merchants is None else merchants,
        "orders.csv": orders,
        "attributions.csv": attributions,
        "posts.csv": posts,
        "reported_revenue.csv": reports,
        "fx_rates.csv": RATES if rates is None else rates,
    }
    for filename, rows in datasets.items():
        with (root / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS[filename])
            writer.writeheader()
            writer.writerows(rows)


def run_inspection(root):
    return inspect_revenue(load_inputs(root))


def summary_for(result, merchant_id, month):
    return next(row for row in result["summaries"] if row["merchant_id"] == merchant_id and row["month"] == month)


class RevenueInspectorTests(unittest.TestCase):
    def test_month_bounds_use_local_timezone_and_dst(self):
        start, end = month_bounds("2026-09", "America/Los_Angeles")
        self.assertEqual(start, datetime(2026, 9, 1, 7, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 1, 7, tzinfo=timezone.utc))
        start, end = month_bounds("2026-10", "Europe/London")
        self.assertEqual(start, datetime(2026, 9, 30, 23, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 1, 0, tzinfo=timezone.utc))

    def test_calculates_multiple_merchants_and_currencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order(), order("200", "2", "780", "HKD")],
                [{"order_id": "100", "post_id": "P1"}, {"order_id": "200", "post_id": "P2"}],
                [post(), post("P2", "2")],
                [report(), report("2", amount="100")],
            )
            result = run_inspection(root)
            self.assertEqual(summary_for(result, "1", "2026-09")["calculated_usd"], "100.00")
            self.assertEqual(summary_for(result, "2", "2026-09")["calculated_usd"], "100.00")
            self.assertEqual(summary_for(result, "2", "2026-09")["status"], "一致")

    def test_multi_post_and_exact_duplicate_attributions_count_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order()],
                [
                    {"order_id": "100", "post_id": "P1"},
                    {"order_id": "100", "post_id": "P1"},
                    {"order_id": "100", "post_id": "P2"},
                ],
                [post(), post("P2")],
                [report()],
            )
            result = run_inspection(root)
            self.assertEqual(summary_for(result, "1", "2026-09")["calculated_usd"], "100.00")
            codes = {(issue["code"], issue["severity"]) for issue in result["issues"]}
            self.assertIn(("DUPLICATE_ATTRIBUTION", "WARN"), codes)
            self.assertIn(("MULTI_POST_ATTRIBUTION", "INFO"), codes)

    def test_invalid_attributions_are_errors_and_do_not_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order(), order("101")],
                [{"order_id": "100", "post_id": "P_OTHER"}, {"order_id": "101", "post_id": "MISSING"}],
                [post("P_OTHER", "2")],
                [report(amount="100")],
            )
            result = run_inspection(root)
            codes = {issue["code"] for issue in result["issues"]}
            self.assertIn("CROSS_MERCHANT_ATTRIBUTION", codes)
            self.assertIn("UNKNOWN_POST", codes)
            self.assertEqual(summary_for(result, "1", "2026-09")["calculated_usd"], "0.00")
            self.assertEqual(summary_for(result, "1", "2026-09")["status"], "数据异常+金额差异")

    def test_missing_currency_rate_is_error_not_silent_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order(currency="EUR")],
                [{"order_id": "100", "post_id": "P1"}],
                [post()],
                [report(amount="0")],
            )
            result = run_inspection(root)
            summary = summary_for(result, "1", "2026-09")
            self.assertEqual(summary["status"], "数据异常")
            self.assertTrue(any(issue["code"] == "MISSING_FX_RATE" for issue in result["issues"]))

    def test_filters_refunded_test_and_orders_outside_local_month(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [
                    order(),
                    order("101", status="refunded"),
                    order("102", is_test="1"),
                    order("103", created_at="2026-09-01T05:00:00Z"),
                    order("104", created_at="2026-10-01T03:00:00Z"),
                ],
                [{"order_id": order_id, "post_id": "P1"} for order_id in ["100", "101", "102", "103", "104"]],
                [post()],
                [report(amount="200")],
            )
            result = run_inspection(root)
            self.assertEqual(summary_for(result, "1", "2026-09")["calculated_usd"], "200.00")
            excluded = {row["order_id"]: row["exclusion_reason"] for row in result["order_details"] if not row["included"]}
            self.assertIn("已退款或非 paid", excluded["101"])
            self.assertIn("测试订单", excluded["102"])
            self.assertIn("不属于商家当地报表月份", excluded["103"])

    def test_duplicate_order_id_is_data_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order(), order()],
                [{"order_id": "100", "post_id": "P1"}],
                [post()],
                [report(amount="0")],
            )
            result = run_inspection(root)
            self.assertTrue(any(issue["code"] == "DUPLICATE_ORDER_ID" for issue in result["issues"]))

    def test_report_files_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            output = root / "output"
            data.mkdir()
            write_dataset(data, [order()], [{"order_id": "100", "post_id": "P1"}], [post()], [report()])
            self.assertEqual(main(["--data-dir", str(data), "--output-dir", str(output)]), 0)
            for filename in ["inspection_report.html", "merchant_summary.csv", "order_details.csv", "data_issues.csv"]:
                self.assertTrue((output / filename).is_file())
            self.assertEqual((output / "merchant_summary.csv").read_bytes()[:3], b"\xef\xbb\xbf")
            write_dataset(data, [order()], [{"order_id": "100", "post_id": "P1"}], [post()], [report(amount="120")])
            self.assertEqual(main(["--data-dir", str(data), "--output-dir", str(output)]), 1)

    def test_missing_required_column_returns_two(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(root, [order()], [{"order_id": "100", "post_id": "P1"}], [post()], [report()])
            (root / "orders.csv").write_text("order_id,amount\n100,100\n", encoding="utf-8")
            self.assertEqual(main(["--data-dir", str(root), "--output-dir", str(root / "out")]), 2)
            with self.assertRaises(InputError):
                load_inputs(root)

    def test_html_escapes_dynamic_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = {
                "generated_at": "2026-11-07T00:00:00Z",
                "summaries": [],
                "order_details": [],
                "issues": [{"severity": "ERROR", "code": "X", "object_id": "<script>alert(1)</script>", "message": "<b>bad</b>"}],
            }
            write_reports(result, root)
            html = (root / "inspection_report.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
            self.assertIn("&lt;b&gt;bad&lt;/b&gt;", html)

    def test_month_status_is_independent_of_report_row_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports = [report(amount="100"), report(month="2026-10", amount="0")]
            write_dataset(
                root,
                [order(), order("200", amount="80", currency="EUR", created_at="2026-10-10T12:00:00Z")],
                [{"order_id": "100", "post_id": "P1"}, {"order_id": "200", "post_id": "P2"}],
                [post(), post("P2")],
                reports,
            )
            data = load_inputs(root)
            first = inspect_revenue(data)
            data["reported_revenue"].reverse()
            second = inspect_revenue(data)
            for result in [first, second]:
                self.assertEqual(summary_for(result, "1", "2026-09")["status"], "一致")
                self.assertEqual(summary_for(result, "1", "2026-10")["status"], "数据异常")

    def test_invalid_report_amount_does_not_pollute_other_month(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order()],
                [{"order_id": "100", "post_id": "P1"}],
                [post()],
                [report(amount="100"), report(month="2026-10", amount="bad")],
            )
            data = load_inputs(root)
            for reports in [data["reported_revenue"], reversed(data["reported_revenue"])]:
                data["reported_revenue"] = list(reports)
                result = inspect_revenue(data)
                self.assertEqual(summary_for(result, "1", "2026-09")["status"], "一致")
                self.assertTrue(any(issue["code"] == "INVALID_REPORTED_AMOUNT" for issue in result["issues"]))

    def test_invalid_report_month_does_not_pollute_valid_month(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order()],
                [{"order_id": "100", "post_id": "P1"}],
                [post()],
                [report(amount="100"), report(month="bad", amount="0")],
            )
            data = load_inputs(root)
            for reports in [data["reported_revenue"], reversed(data["reported_revenue"])]:
                data["reported_revenue"] = list(reports)
                result = inspect_revenue(data)
                self.assertEqual(summary_for(result, "1", "2026-09")["status"], "一致")
                self.assertTrue(any(issue["code"] == "INVALID_MONTH_OR_TIMEZONE" for issue in result["issues"]))

    def test_duplicate_report_month_does_not_pollute_other_month(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order()],
                [{"order_id": "100", "post_id": "P1"}],
                [post()],
                [report(amount="100"), report(amount="100"), report(month="2026-10", amount="0")],
            )
            result = run_inspection(root)
            self.assertEqual(summary_for(result, "1", "2026-09")["status"], "数据异常")
            self.assertEqual(summary_for(result, "1", "2026-10")["status"], "一致")
            self.assertTrue(any(issue["code"] == "DUPLICATE_REPORT_KEY" for issue in result["issues"]))

    def test_out_of_month_invalid_order_fields_do_not_pollute_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [
                    order(amount="bad"),
                    order("101", is_test="bad"),
                ],
                [{"order_id": "100", "post_id": "P1"}, {"order_id": "101", "post_id": "P1"}],
                [post()],
                [report(month="2026-10", amount="0")],
            )
            result = run_inspection(root)
            self.assertEqual(summary_for(result, "1", "2026-10")["status"], "一致")
            self.assertFalse(any(issue["code"] in {"INVALID_ORDER_AMOUNT", "INVALID_IS_TEST"} for issue in result["issues"]))

    def test_attribution_error_is_scoped_to_order_month(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [order()],
                [{"order_id": "100", "post_id": "MISSING"}],
                [],
                [report(month="2026-10", amount="0")],
            )
            result = run_inspection(root)
            self.assertEqual(summary_for(result, "1", "2026-10")["status"], "一致")
            self.assertTrue(any(issue["code"] == "UNKNOWN_POST" for issue in result["issues"]))

    def test_empty_report_input_fails_instead_of_passing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(root, [order()], [{"order_id": "100", "post_id": "P1"}], [post()], [])
            result = run_inspection(root)
            self.assertTrue(any(issue["code"] == "NO_REPORTS" and issue["severity"] == "ERROR" for issue in result["issues"]))
            output = root / "output"
            self.assertEqual(main(["--data-dir", str(root), "--output-dir", str(output)]), 1)
            self.assertIn("需处理", (output / "inspection_report.html").read_text(encoding="utf-8"))

    def test_irrelevant_orders_do_not_require_fx_rate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root,
                [
                    order(currency="EUR", status="refunded"),
                    order("101", currency="EUR", created_at="2026-08-01T12:00:00Z"),
                ],
                [{"order_id": "100", "post_id": "P1"}, {"order_id": "101", "post_id": "P1"}],
                [post()],
                [report(amount="0")],
            )
            result = run_inspection(root)
            self.assertEqual(summary_for(result, "1", "2026-09")["status"], "一致")
            self.assertFalse(any(issue["code"] == "MISSING_FX_RATE" for issue in result["issues"]))

    def test_html_uses_product_friendly_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            write_dataset(data, [order()], [{"order_id": "100", "post_id": "P1"}], [post()], [report()])
            write_reports(run_inspection(data), root)
            html = (root / "inspection_report.html").read_text(encoding="utf-8")
            self.assertIn("首页金额（USD）", html)
            self.assertIn("是否计入", html)
            self.assertIn("<td>是</td>", html)

    def test_core_report_is_computed_by_standalone_sql(self):
        data = load_inputs(MODULE_DIR / "sample_data")
        connection = inspector.build_database(data)
        rows = {row["merchant_id"]: row for row in inspector.query_merchant_report(connection)}
        self.assertEqual(rows["101"]["calculated_usd"], "1600.0")
        self.assertEqual(rows["202"]["calculated_usd"], "250.0")
        sql_text = inspector.SQL_PATH.read_text(encoding="utf-8")
        self.assertIn("SUM(", sql_text)
        self.assertIn("JOIN posts", sql_text)
        self.assertIn("valid_attributions", sql_text)

    def test_order_details_sum_matches_sql_report(self):
        result = run_inspection(MODULE_DIR / "sample_data")
        for summary in result["summaries"]:
            detail_total = sum(
                Decimal(row["included_usd"])
                for row in result["order_details"]
                if row["merchant_id"] == summary["merchant_id"]
                and row["report_month"] == summary["month"]
                and row["included"]
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            self.assertEqual(str(detail_total), summary["calculated_usd"])

    def test_bundled_sample_is_reproducible(self):
        result = run_inspection(MODULE_DIR / "sample_data")
        expected = {
            "101": ("1600.00", "1980.00"),
            "202": ("250.00", "0.00"),
            "303": ("270.00", "0.00"),
            "404": ("192.00", "0.00"),
        }
        self.assertEqual(len(result["summaries"]), 4)
        for merchant_id, (calculated, difference) in expected.items():
            month = "2026-10" if merchant_id == "404" else "2026-09"
            summary = summary_for(result, merchant_id, month)
            self.assertEqual(summary["calculated_usd"], calculated)
            self.assertEqual(summary["difference_usd"], difference)
        mismatches = {row["merchant_id"] for row in result["summaries"] if row["difference_usd"] != "0.00"}
        self.assertEqual(mismatches, {"101"})
        self.assertFalse(any(issue["severity"] == "ERROR" for issue in result["issues"]))
        self.assertTrue(any(issue["code"] == "MULTI_POST_ATTRIBUTION" and issue["object_id"] == "1002" for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()
