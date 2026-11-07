import argparse
import csv
import html
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
CENT = Decimal("0.01")
SQL_PATH = Path(__file__).with_name("sql") / "revenue_report.sql"
INPUT_SCHEMAS = {
    "merchants.csv": ["merchant_id", "timezone"],
    "orders.csv": ["order_id", "merchant_id", "amount", "currency", "status", "is_test", "created_at"],
    "attributions.csv": ["order_id", "post_id"],
    "posts.csv": ["post_id", "merchant_id", "source"],
    "reported_revenue.csv": ["merchant_id", "month", "reported_ai_revenue_usd"],
    "fx_rates.csv": ["currency", "currency_units_per_usd"],
}


class InputError(ValueError):
    pass


def read_csv_required(path, required_columns):
    path = Path(path)
    if not path.is_file():
        raise InputError(f"缺少输入文件：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in required_columns if column not in (reader.fieldnames or [])]
        if missing:
            raise InputError(f"{path.name} 缺少必填列：{', '.join(missing)}")
        rows = []
        for row in reader:
            if not any((value or "").strip() for value in row.values()):
                continue
            rows.append({column: (row.get(column) or "").strip() for column in required_columns})
        return rows


def parse_utc(value):
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def month_bounds(month, timezone_name):
    try:
        year, month_number = map(int, month.split("-"))
        if len(month) != 7 or not 1 <= month_number <= 12:
            raise ValueError
        zone = ZoneInfo(timezone_name)
        local_start = datetime(year, month_number, 1, tzinfo=zone)
        local_end = datetime(year + (month_number == 12), month_number % 12 + 1, 1, tzinfo=zone)
        return local_start.astimezone(UTC), local_end.astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise InputError(
            f"无效月份或时区：{month} / {timezone_name}。IANA 时区缺失时可运行 python -m pip install tzdata"
        ) from exc


def load_inputs(data_dir):
    data_dir = Path(data_dir)
    return {
        filename.removesuffix(".csv"): read_csv_required(data_dir / filename, columns)
        for filename, columns in INPUT_SCHEMAS.items()
    }


def money(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def decimal_value(value):
    try:
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise InvalidOperation
        return parsed
    except (InvalidOperation, ValueError):
        return None


def boolean_value(value):
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    return None


def build_database(data):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SQL_PATH.read_text(encoding="utf-8"))

    merchants = {}
    for row in data["merchants"]:
        if row["merchant_id"] and row["merchant_id"] not in merchants:
            merchants[row["merchant_id"]] = row["timezone"]
    connection.executemany(
        "INSERT INTO merchants (merchant_id, timezone) VALUES (?, ?)",
        merchants.items(),
    )

    posts = {}
    for row in data["posts"]:
        if row["post_id"] and row["post_id"] not in posts:
            posts[row["post_id"]] = (row["post_id"], row["merchant_id"], row["source"])
    connection.executemany(
        "INSERT INTO posts (post_id, merchant_id, source) VALUES (?, ?, ?)",
        posts.values(),
    )

    orders = {}
    for row in data["orders"]:
        order_id = row["order_id"]
        if not order_id or order_id in orders:
            continue
        amount = decimal_value(row["amount"])
        if amount is None or amount < 0:
            amount = None
        try:
            created_utc = parse_utc(row["created_at"]).isoformat()
        except ValueError:
            created_utc = None
        is_test = boolean_value(row["is_test"])
        orders[order_id] = (
            order_id,
            row["merchant_id"],
            row["amount"],
            float(amount) if amount is not None else None,
            row["currency"].upper(),
            row["status"],
            int(is_test) if is_test is not None else None,
            created_utc,
        )
    connection.executemany(
        """
        INSERT INTO orders (
          order_id, merchant_id, original_amount, amount, currency, status, is_test, created_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        orders.values(),
    )

    rates = {}
    for row in data["fx_rates"]:
        currency = row["currency"].upper()
        rate = decimal_value(row["currency_units_per_usd"])
        if currency and currency not in rates and rate is not None and rate > 0:
            rates[currency] = float(rate)
    connection.executemany(
        "INSERT INTO fx_rates (currency, currency_units_per_usd) VALUES (?, ?)",
        rates.items(),
    )

    connection.executemany(
        "INSERT INTO attributions (order_id, post_id) VALUES (?, ?)",
        [(row["order_id"], row["post_id"]) for row in data["attributions"]],
    )

    seen_periods = set()
    for row in data["reported_revenue"]:
        merchant_id = row["merchant_id"]
        month = row["month"]
        key = (merchant_id, month)
        if key in seen_periods or merchant_id not in merchants:
            continue
        try:
            start_utc, end_utc = month_bounds(month, merchants[merchant_id])
        except InputError:
            continue
        reported = decimal_value(row["reported_ai_revenue_usd"])
        if reported is None or reported < 0:
            continue
        seen_periods.add(key)
        connection.execute(
            """
            INSERT INTO report_periods (
              merchant_id, month, timezone, start_utc, end_utc, reported_usd
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                merchant_id,
                month,
                merchants[merchant_id],
                start_utc.isoformat(),
                end_utc.isoformat(),
                format(reported, "f"),
            ),
        )
    connection.commit()
    return connection


def query_merchant_report(connection):
    rows = connection.execute(
        """
        SELECT merchant_id, month, timezone, reported_usd, calculated_usd
        FROM merchant_report
        ORDER BY merchant_id, month
        """
    ).fetchall()
    return [
        {
            "merchant_id": row["merchant_id"],
            "month": row["month"],
            "timezone": row["timezone"],
            "reported_usd": str(row["reported_usd"]),
            "calculated_usd": str(row["calculated_usd"]),
        }
        for row in rows
    ]


def inspect_revenue(data):
    issues = []
    merchant_errors = set()
    month_errors = set()

    def add_issue(severity, code, object_id, message, merchant_id=None, month=None):
        issues.append({
            "severity": severity,
            "code": code,
            "object_id": str(object_id),
            "message": message,
        })
        if severity == "ERROR" and merchant_id is not None:
            if month is None:
                merchant_errors.add(merchant_id)
            else:
                month_errors.add((merchant_id, month))

    merchants = {}
    for row in data["merchants"]:
        merchant_id = row["merchant_id"]
        if not merchant_id:
            add_issue("ERROR", "INVALID_MERCHANT_ID", "(empty)", "merchant_id 不能为空")
            continue
        if merchant_id in merchants:
            add_issue("ERROR", "DUPLICATE_MERCHANT_ID", merchant_id, "商家 ID 重复，保留首条", merchant_id)
            continue
        merchants[merchant_id] = row["timezone"]

    orders = {}
    for row in data["orders"]:
        order_id = row["order_id"]
        merchant_id = row["merchant_id"]
        if not order_id:
            add_issue("ERROR", "INVALID_ORDER_ID", "(empty)", "order_id 不能为空", merchant_id)
            continue
        if order_id in orders:
            add_issue("ERROR", "DUPLICATE_ORDER_ID", order_id, "订单 ID 重复，保留首条", merchant_id)
            continue
        orders[order_id] = row
        if merchant_id not in merchants:
            add_issue("ERROR", "UNKNOWN_MERCHANT", order_id, f"订单引用未知商家 {merchant_id}", merchant_id)

    def order_month(order_row):
        try:
            zone = ZoneInfo(merchants[order_row["merchant_id"]])
            return parse_utc(order_row["created_at"]).astimezone(zone).strftime("%Y-%m")
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            return None

    posts = {}
    for row in data["posts"]:
        post_id = row["post_id"]
        if not post_id:
            add_issue("ERROR", "INVALID_POST_ID", "(empty)", "post_id 不能为空")
            continue
        if post_id in posts:
            add_issue("ERROR", "DUPLICATE_POST_ID", post_id, "贴文 ID 重复，保留首条", row["merchant_id"])
            continue
        posts[post_id] = row

    rates = {}
    for row in data["fx_rates"]:
        currency = row["currency"].upper()
        rate = decimal_value(row["currency_units_per_usd"])
        if currency in rates:
            add_issue("ERROR", "DUPLICATE_FX_RATE", currency, "币种汇率重复，保留首条")
            continue
        if rate is None or rate <= 0:
            add_issue("ERROR", "INVALID_FX_RATE", currency, "汇率必须是大于 0 的有限数值")
            continue
        rates[currency] = rate

    valid_attributions = {}
    seen_attributions = set()
    for row in data["attributions"]:
        order_id = row["order_id"]
        post_id = row["post_id"]
        pair = (order_id, post_id)
        order_row = orders.get(order_id)
        merchant_id = order_row["merchant_id"] if order_row else None
        attribution_month = order_month(order_row) if order_row else None
        if pair in seen_attributions:
            add_issue("WARN", "DUPLICATE_ATTRIBUTION", f"{order_id}/{post_id}", "完全重复的归因行已忽略", merchant_id)
            continue
        seen_attributions.add(pair)
        if order_row is None:
            add_issue("ERROR", "UNKNOWN_ORDER", f"{order_id}/{post_id}", "归因引用未知订单")
            continue
        post_row = posts.get(post_id)
        if post_row is None:
            add_issue("ERROR", "UNKNOWN_POST", f"{order_id}/{post_id}", "归因引用未知贴文", merchant_id, attribution_month)
            continue
        if post_row["source"] != "Reelo":
            add_issue("ERROR", "INVALID_POST_SOURCE", f"{order_id}/{post_id}", "贴文来源不是 Reelo", merchant_id, attribution_month)
            continue
        if post_row["merchant_id"] != merchant_id:
            add_issue("ERROR", "CROSS_MERCHANT_ATTRIBUTION", f"{order_id}/{post_id}", "订单与贴文不属于同一商家", merchant_id, attribution_month)
            continue
        valid_attributions.setdefault(order_id, set()).add(post_id)

    for order_id, post_ids in valid_attributions.items():
        if len(post_ids) > 1:
            add_issue(
                "INFO",
                "MULTI_POST_ATTRIBUTION",
                order_id,
                f"订单关联 {len(post_ids)} 条 Reelo 贴文，总收入仅计一次",
                orders[order_id]["merchant_id"],
            )

    reports = []
    seen_reports = set()
    for row in data["reported_revenue"]:
        key = (row["merchant_id"], row["month"])
        if key in seen_reports:
            add_issue("ERROR", "DUPLICATE_REPORT_KEY", f"{key[0]}/{key[1]}", "商家月报重复，保留首条", key[0], key[1])
            continue
        seen_reports.add(key)
        reports.append(row)

    if not reports:
        add_issue("ERROR", "NO_REPORTS", "reported_revenue.csv", "没有商家月报，未执行任何对账")

    reported_keys = {(row["merchant_id"], row["month"]) for row in reports}
    for merchant_id in merchants:
        if not any(key[0] == merchant_id for key in reported_keys):
            add_issue("WARN", "MISSING_REPORT", merchant_id, "商家没有任何首页月报输入", merchant_id)

    connection = build_database(data)
    sql_report_rows = {
        (row["merchant_id"], row["month"]): row for row in query_merchant_report(connection)
    }

    summaries = []
    order_details = []
    for report in reports:
        merchant_id = report["merchant_id"]
        month = report["month"]
        if merchant_id not in merchants:
            add_issue("ERROR", "UNKNOWN_REPORT_MERCHANT", f"{merchant_id}/{month}", "月报引用未知商家", merchant_id)
            continue
        timezone_name = merchants[merchant_id]
        try:
            start_utc, end_utc = month_bounds(month, timezone_name)
        except InputError as exc:
            add_issue("ERROR", "INVALID_MONTH_OR_TIMEZONE", f"{merchant_id}/{month}", str(exc), merchant_id, month)
            continue
        reported = decimal_value(report["reported_ai_revenue_usd"])
        if reported is None or reported < 0:
            add_issue("ERROR", "INVALID_REPORTED_AMOUNT", f"{merchant_id}/{month}", "首页金额必须是非负有限数值", merchant_id, month)
            continue
        reported = reported.quantize(CENT, rounding=ROUND_HALF_UP)
        zone = ZoneInfo(timezone_name)
        merchant_orders = [row for row in orders.values() if row["merchant_id"] == merchant_id]
        for order_row in sorted(merchant_orders, key=lambda row: row["order_id"]):
            order_id = order_row["order_id"]
            exclusion_reasons = []
            amount = decimal_value(order_row["amount"])
            amount_invalid = amount is None or amount < 0
            if amount_invalid:
                amount = None
            try:
                created_utc = parse_utc(order_row["created_at"])
                created_local = created_utc.astimezone(zone)
                in_month = start_utc <= created_utc < end_utc
            except ValueError:
                add_issue("ERROR", "INVALID_ORDER_TIME", f"{merchant_id}/{month}/{order_id}", "订单时间不是有效 ISO 8601 时间", merchant_id, month)
                created_utc = None
                created_local = None
                in_month = False
                exclusion_reasons.append("订单时间无效")
            if not in_month and created_utc is not None:
                exclusion_reasons.append("不属于商家当地报表月份")

            is_test = boolean_value(order_row["is_test"])
            if in_month or created_utc is None:
                if amount_invalid:
                    add_issue("ERROR", "INVALID_ORDER_AMOUNT", f"{merchant_id}/{month}/{order_id}", "订单金额必须是非负有限数值", merchant_id, month)
                    exclusion_reasons.append("订单金额无效")
                if is_test is None:
                    add_issue("ERROR", "INVALID_IS_TEST", f"{merchant_id}/{month}/{order_id}", "is_test 仅支持 0/1/true/false/yes/no", merchant_id, month)
                    exclusion_reasons.append("测试标识无效")
            is_paid = order_row["status"] == "paid"
            if not is_paid:
                exclusion_reasons.append("已退款或非 paid")
            if is_test:
                exclusion_reasons.append("测试订单")

            post_ids = valid_attributions.get(order_id, set())
            if not post_ids:
                exclusion_reasons.append("无有效 Reelo 归因")
            currency = order_row["currency"].upper()
            rate = rates.get(currency)
            requires_rate = in_month and is_paid and is_test is False and bool(post_ids) and amount is not None
            if rate is None and requires_rate:
                add_issue("ERROR", "MISSING_FX_RATE", f"{merchant_id}/{month}/{order_id}/{currency}", "订单币种缺少有效汇率", merchant_id, month)
                exclusion_reasons.append("缺少币种汇率")

            included = requires_rate and rate is not None
            included_usd = amount / rate if included else Decimal("0")
            order_details.append({
                "merchant_id": merchant_id,
                "report_month": month,
                "order_id": order_id,
                "utc_time": created_utc.isoformat() if created_utc else "",
                "local_time": created_local.isoformat() if created_local else "",
                "amount": order_row["amount"],
                "currency": currency,
                "currency_units_per_usd": format(rate, "f") if rate is not None else "",
                "post_ids": ";".join(sorted(post_ids)),
                "attribution_count": len(post_ids),
                "included": included,
                "included_usd": format(included_usd, "f"),
                "exclusion_reason": "；".join(exclusion_reasons),
            })

        calculated = Decimal(sql_report_rows[(merchant_id, month)]["calculated_usd"]).quantize(CENT, rounding=ROUND_HALF_UP)
        difference = (reported - calculated).quantize(CENT, rounding=ROUND_HALF_UP)
        has_error = merchant_id in merchant_errors or (merchant_id, month) in month_errors
        if has_error and difference != 0:
            status = "数据异常+金额差异"
        elif has_error:
            status = "数据异常"
        elif difference != 0:
            status = "金额差异"
        else:
            status = "一致"
        summaries.append({
            "merchant_id": merchant_id,
            "month": month,
            "timezone": timezone_name,
            "reported_usd": money(reported),
            "calculated_usd": money(calculated),
            "difference_usd": money(difference),
            "status": status,
        })

    summaries.sort(key=lambda row: (row["merchant_id"], row["month"]))
    order_details.sort(key=lambda row: (row["merchant_id"], row["report_month"], row["order_id"]))
    connection.close()
    return {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "summaries": summaries,
        "order_details": order_details,
        "issues": issues,
    }


def write_csv(path, fieldnames, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_reports(result, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_fields = ["merchant_id", "month", "timezone", "reported_usd", "calculated_usd", "difference_usd", "status"]
    detail_fields = [
        "merchant_id", "report_month", "order_id", "utc_time", "local_time", "amount", "currency",
        "currency_units_per_usd", "post_ids", "attribution_count", "included", "included_usd", "exclusion_reason",
    ]
    issue_fields = ["severity", "code", "object_id", "message"]
    write_csv(output_dir / "merchant_summary.csv", summary_fields, result["summaries"])
    write_csv(output_dir / "order_details.csv", detail_fields, result["order_details"])
    write_csv(output_dir / "data_issues.csv", issue_fields, result["issues"])

    def cell(value):
        return f"<td>{html.escape(str(value))}</td>"

    summary_labels = ["商家", "月份", "时区", "首页金额（USD）", "重算金额（USD）", "差额（USD）", "状态"]
    issue_labels = ["级别", "类型", "对象", "说明"]
    detail_labels = [
        "商家", "报表月份", "订单", "UTC 时间", "商家本地时间", "原金额", "币种", "每美元对应币种数",
        "有效贴文", "归因数", "是否计入", "计入美元（未舍入）", "排除原因",
    ]
    severity_labels = {"INFO": "提示", "WARN": "警告", "ERROR": "错误"}
    summary_rows = "".join(
        "<tr>" + "".join(cell(row[field]) for field in summary_fields) + "</tr>"
        for row in result["summaries"]
    ) or '<tr><td colspan="7">无商家月报</td></tr>'
    issue_rows = "".join(
        "<tr>"
        + cell(severity_labels.get(row["severity"], row["severity"]))
        + "".join(cell(row[field]) for field in issue_fields[1:])
        + "</tr>"
        for row in result["issues"]
    ) or '<tr><td colspan="4">无数据质量发现</td></tr>'
    detail_rows = "".join(
        "<tr>" + "".join(
            cell("是" if row["included"] else "否") if field == "included" else cell(row[field])
            for field in detail_fields
        ) + "</tr>"
        for row in result["order_details"]
    ) or f'<tr><td colspan="{len(detail_fields)}">无订单明细</td></tr>'
    difference_count = sum(row["difference_usd"] != "0.00" for row in result["summaries"])
    error_count = sum(issue["severity"] == "ERROR" for issue in result["issues"])
    overall = "通过" if difference_count == 0 and error_count == 0 else "需处理"
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Reelo AI 收入巡检报告</title>
<style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:32px;color:#1f2937;line-height:1.5}}
h1{{color:#111827}} h2{{margin-top:32px;border-bottom:2px solid #e5e7eb;padding-bottom:8px}}
.cards{{display:flex;gap:16px;flex-wrap:wrap}} .card{{background:#f3f4f6;padding:16px 24px;border-radius:8px}}
.status{{font-weight:700;color:{'#047857' if overall == '通过' else '#b91c1c'}}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border:1px solid #d1d5db;padding:7px;text-align:left;vertical-align:top}}
th{{background:#e5e7eb;position:sticky;top:0}} tr:nth-child(even){{background:#f9fafb}}
.table-wrap{{overflow:auto;max-height:560px}} small{{color:#4b5563}}
</style>
</head>
<body>
<h1>Reelo AI 收入巡检报告</h1>
<p>生成时间：{html.escape(result['generated_at'])}　总体状态：<span class="status">{overall}</span></p>
<div class="cards"><div class="card">商家月报<br><strong>{len(result['summaries'])}</strong></div><div class="card">金额差异<br><strong>{difference_count}</strong></div><div class="card">ERROR<br><strong>{error_count}</strong></div></div>
<p><small>difference_usd = 首页报表金额 − 按指标重算金额；INFO/WARN 为提示，ERROR 表示金额可信度受影响。</small></p>
<h2>商家月报汇总</h2><div class="table-wrap"><table><thead><tr>{''.join(f'<th>{label}</th>' for label in summary_labels)}</tr></thead><tbody>{summary_rows}</tbody></table></div>
<h2>数据质量发现</h2><div class="table-wrap"><table><thead><tr>{''.join(f'<th>{label}</th>' for label in issue_labels)}</tr></thead><tbody>{issue_rows}</tbody></table></div>
<h2>订单明细</h2><div class="table-wrap"><table><thead><tr>{''.join(f'<th>{label}</th>' for label in detail_labels)}</tr></thead><tbody>{detail_rows}</tbody></table></div>
</body></html>"""
    (output_dir / "inspection_report.html").write_text(document, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="按商家当地月份巡检 Reelo AI 归因收入")
    parser.add_argument("--data-dir", required=True, help="六份输入 CSV 所在目录")
    parser.add_argument("--output-dir", required=True, help="HTML/CSV 报告输出目录")
    args = parser.parse_args(argv)
    try:
        result = inspect_revenue(load_inputs(args.data_dir))
        write_reports(result, args.output_dir)
    except InputError as exc:
        print(f"输入错误：{exc}")
        return 2

    for row in result["summaries"]:
        print(
            f"{row['merchant_id']} / {row['month']}：报表 ${row['reported_usd']}，"
            f"重算 ${row['calculated_usd']}，差额 ${row['difference_usd']}，{row['status']}"
        )
    difference_count = sum(row["difference_usd"] != "0.00" for row in result["summaries"])
    error_count = sum(issue["severity"] == "ERROR" for issue in result["issues"])
    print(f"商家月报 {len(result['summaries'])} 个，金额差异 {difference_count} 个，ERROR {error_count} 个。")
    print(f"报告目录：{Path(args.output_dir).resolve()}")
    return 1 if difference_count or error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
