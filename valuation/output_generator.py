from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any


class OutputGenerator:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _write_csv(self, path: Path, headers: list[str], rows: list[list[Any]]) -> Path:
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def _result_rows(self, valuation_result: Any) -> list[Any]:
        return [
            valuation_result.dcf_result,
            valuation_result.pe_result,
            valuation_result.ps_result,
            valuation_result.ev_ebitda_result,
            valuation_result.asset_result,
            valuation_result.comps_result,
        ]

    def save_valuation_csv(self, stock_code: str, valuation_result: Any) -> Path:
        rows: list[list[Any]] = []
        for result in self._result_rows(valuation_result):
            rows.append(
                [
                    result.method_name,
                    result.min_value,
                    result.base_value,
                    result.max_value,
                    "是" if result.is_available else "否",
                    "；".join(result.missing_data) if result.missing_data else "无",
                ]
            )
        rows.append([])
        rows.append(["推荐估值方法", valuation_result.recommended_method])
        rows.append(["推荐原因", valuation_result.recommended_reason])
        return self._write_csv(
            self.output_dir / f"{stock_code}_估值结果.csv",
            ["估值方法", "保守估值(亿元)", "基准估值(亿元)", "乐观估值(亿元)", "是否可用", "缺失数据"],
            rows,
        )

    def save_params_csv(self, stock_code: str, valuation_params: dict) -> Path:
        rows = []
        param_map = {
            "risk_free_rate": ("无风险利率", "%"),
            "market_risk_premium": ("市场风险溢价", "%"),
            "beta": ("Beta", ""),
            "cost_of_equity": ("权益成本", "%"),
            "cost_of_debt": ("债务成本", "%"),
            "tax_rate": ("税率", "%"),
            "debt_ratio": ("债务比率", "%"),
            "wacc": ("WACC", "%"),
            "perpetual_growth_rate": ("永续增长率", "%"),
            "pe_multiple": ("PE倍数", ""),
            "ps_multiple": ("PS倍数", ""),
            "ev_ebitda_multiple": ("EV/EBITDA倍数", ""),
        }
        for key, (label, unit) in param_map.items():
            rows.append([label, valuation_params.get(key, "N/A"), unit])
        return self._write_csv(self.output_dir / f"{stock_code}_估值参数.csv", ["参数名称", "数值", "单位"], rows)

    def save_formula_csv(self, stock_code: str, valuation_result: Any) -> Path:
        rows = []
        for row in valuation_result.formula_rows:
            rows.append([row.get("估值方法"), row.get("公式"), row.get("核心参数"), row.get("是否可用")])
        return self._write_csv(self.output_dir / f"{stock_code}_公式说明.csv", ["估值方法", "公式", "核心参数", "是否可用"], rows)

    def save_sensitivity_csv(self, stock_code: str, valuation_result: Any) -> Path:
        rows = []
        for row in valuation_result.sensitivity_rows:
            rows.append([row.get("WACC变动"), row.get("增长率变动"), row.get("DCF估值(亿元)")])
        return self._write_csv(self.output_dir / f"{stock_code}_敏感性分析.csv", ["WACC变动", "增长率变动", "DCF估值(亿元)"], rows)

    def save_method_recommendation_txt(self, stock_code: str, company_name: str, valuation_result: Any) -> Path:
        lines = [
            "=" * 64,
            "企业估值分析报告",
            f"公司: {company_name} ({stock_code})",
            f"行业: {valuation_result.industry}",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 64,
            "",
            "一、估值方法结果",
            "-" * 40,
        ]

        available_results = []
        for result in self._result_rows(valuation_result):
            status = "可用" if result.is_available else "不可用"
            lines.append(f"{result.method_name}: {status}")
            lines.append(f"  估值区间: {result.min_value:.2f} ~ {result.max_value:.2f} 亿元")
            if result.missing_data:
                lines.append(f"  缺失数据: {', '.join(result.missing_data)}")
            if result.is_available:
                available_results.append(result)

        lines.extend(
            [
                "",
                "二、推荐方法",
                "-" * 40,
                f"推荐方法: {valuation_result.recommended_method}",
                f"推荐理由: {valuation_result.recommended_reason}",
            ]
        )

        lines.extend(["", "三、总体估值区间", "-" * 40])
        if available_results:
            min_values = [row.min_value for row in available_results]
            base_values = [row.base_value for row in available_results]
            max_values = [row.max_value for row in available_results]
            lines.append(f"保守估值: {min(min_values):.2f} 亿元")
            lines.append(f"基准估值: {sum(base_values) / len(base_values):.2f} 亿元")
            lines.append(f"乐观估值: {max(max_values):.2f} 亿元")
        else:
            lines.append("暂无可用估值结果")

        lines.extend(["", "四、核心参数", "-" * 40])
        params = valuation_result.all_params
        for key, label in [
            ("risk_free_rate", "无风险利率"),
            ("market_risk_premium", "市场风险溢价"),
            ("beta", "Beta"),
            ("cost_of_equity", "权益成本"),
            ("cost_of_debt", "债务成本"),
            ("tax_rate", "税率"),
            ("debt_ratio", "债务比率"),
            ("wacc", "WACC"),
            ("perpetual_growth_rate", "永续增长率"),
            ("pe_multiple", "PE倍数"),
            ("ps_multiple", "PS倍数"),
            ("ev_ebitda_multiple", "EV/EBITDA倍数"),
        ]:
            lines.append(f"{label}: {params.get(key, 'N/A')}")

        lines.extend(["", "五、风险与告警", "-" * 40])
        if valuation_result.warnings:
            for warning in valuation_result.warnings:
                lines.append(f"- {warning}")
        else:
            lines.append("- 未发现明显异常")

        lines.extend(["", "注：本报告仅供研究参考，不构成投资建议。"])
        path = self.output_dir / f"{stock_code}_估值报告.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
