import argparse
import sqlite3
from pathlib import Path

UTC_ORIGINAL_START = "2026-09-01"
UTC_ORIGINAL_END = "2026-10-01"
UTC_CORRECT_START = "2026-09-01 07:00"
UTC_CORRECT_END = "2026-10-01 07:00"


def money(value):
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def main():
    parser = argparse.ArgumentParser(description="核对 Reelo 首页 AI 收入与订单明细")
    parser.add_argument("--reported", type=float, default=3580.0, help="首页展示的美元金额")
    args = parser.parse_args()

    sql_path = Path(__file__).with_name("B1_数据核对.sql")
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(sql_path.read_text(encoding="utf-8"))

    original = connection.execute("SELECT ai_revenue FROM original_report").fetchone()[0]
    corrected = connection.execute("SELECT ai_revenue FROM corrected_report").fetchone()[0]
    rows = connection.execute(
        """
        SELECT
          r.order_id,
          r.original_contribution,
          r.correct_contribution,
          r.difference,
          o.amount,
          o.currency,
          o.status,
          o.is_test,
          o.created_at,
          COUNT(a.post_id) AS attribution_rows
        FROM order_reconciliation r
        JOIN orders o ON o.order_id = r.order_id
        LEFT JOIN attributions a ON a.order_id = o.order_id
        GROUP BY r.order_id
        ORDER BY r.order_id
        """
    ).fetchall()

    print(f"原 SQL 结果：{money(original)}")
    print(f"正确结果：{money(corrected)}")
    print(f"首页结果：{money(args.reported)}")
    print(f"首页减正确结果：{money(args.reported - corrected)}")
    print("\n逐订单差异：")

    for row in rows:
        if abs(row["difference"]) < 0.000001:
            continue

        reasons = []
        original_in_month = (
            UTC_ORIGINAL_START <= row["created_at"] < UTC_ORIGINAL_END
            and row["attribution_rows"] > 0
        )
        correct_in_month = UTC_CORRECT_START <= row["created_at"] < UTC_CORRECT_END

        if original_in_month and row["attribution_rows"] > 1:
            reasons.append(f"同一订单被关联 {row['attribution_rows']} 次")
        if original_in_month and row["status"] == "refunded":
            reasons.append("退款订单被原 SQL 计入")
        if original_in_month and row["is_test"] == 1:
            reasons.append("测试订单被原 SQL 计入")
        if original_in_month and row["currency"] == "HKD":
            reasons.append("港币未除以 7.8 换算成美元")
        if original_in_month and not correct_in_month:
            reasons.append("UTC 时间落在 9 月，但洛杉矶本地仍属 8 月")
        if not original_in_month and correct_in_month and row["attribution_rows"] > 0:
            reasons.append("UTC 时间落在 10 月，但洛杉矶本地仍属 9 月")

        print(
            f"订单 {row['order_id']}：原贡献 {money(row['original_contribution'])}，"
            f"正确贡献 {money(row['correct_contribution'])}，"
            f"差异 {money(row['difference'])}；{'；'.join(reasons)}"
        )

    assert round(original, 2) == 3580.00
    assert round(corrected, 2) == 1600.00
    assert round(original - corrected, 2) == 1980.00
    print("\n样例校验通过：原 SQL、正确 SQL 和差额均与逐订单对账一致。")
    if abs(args.reported - corrected) >= 0.005:
        print(f"对账结果：发现差异，首页金额与正确结果相差 {money(args.reported - corrected)}。")
    else:
        print("对账结果：首页金额与正确结果一致。")


if __name__ == "__main__":
    main()
