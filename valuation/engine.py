from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ValuationResult:
    method_name: str
    min_value: float
    base_value: float
    max_value: float
    is_available: bool
    missing_data: list[str]
    formula: str


@dataclass
class FinalValuation:
    stock_code: str
    company_name: str
    industry: str
    dcf_result: ValuationResult
    pe_result: ValuationResult
    ps_result: ValuationResult
    ev_ebitda_result: ValuationResult
    asset_result: ValuationResult
    comps_result: ValuationResult
    recommended_method: str
    recommended_reason: str
    all_params: dict
    warnings: list[str]
    formula_rows: list[dict]
    sensitivity_rows: list[dict]


class ValuationEngine:
    def __init__(self, config: dict, industry_config: dict):
        self.config = config
        self.industry_config = industry_config

    def _latest(self, financial_data: list[dict]) -> dict | None:
        if not financial_data:
            return None
        return sorted(financial_data, key=lambda row: (row.get("year", 0), row.get("report_date", "")), reverse=True)[0]

    def _is_annual_report(self, row: dict) -> bool:
        report_label = str(row.get("report_label", ""))
        report_date = str(row.get("report_date", ""))
        return report_label.endswith("A") or report_date.endswith("-12-31")

    def _is_ttm_report(self, row: dict) -> bool:
        return str(row.get("report_label", "")).endswith("TTM")

    def _latest_matching(
        self,
        financial_data: list[dict],
        required_fields: list[str],
        *,
        prefer_ttm: bool = False,
        prefer_annual: bool = False,
    ) -> dict | None:
        rows = sorted(financial_data, key=lambda row: (row.get("year", 0), row.get("report_date", "")), reverse=True)
        if prefer_ttm:
            ttm_rows = [row for row in rows if self._is_ttm_report(row)]
            non_ttm_rows = [row for row in rows if not self._is_ttm_report(row)]
            rows = ttm_rows + non_ttm_rows
        if prefer_annual:
            annual_rows = [row for row in rows if self._is_annual_report(row)]
            if annual_rows:
                rows = annual_rows

        for row in rows:
            valid = True
            for field in required_fields:
                value = float(row.get(field, 0) or 0)
                if value <= 0:
                    valid = False
                    break
            if valid:
                return row
        return rows[0] if rows else None

    def _unavailable(self, method_name: str, missing_data: list[str], formula: str) -> ValuationResult:
        return ValuationResult(
            method_name=method_name,
            min_value=0.0,
            base_value=0.0,
            max_value=0.0,
            is_available=False,
            missing_data=missing_data,
            formula=formula,
        )

    def calculate_dcf(self, financial_data: list[dict], params: dict) -> ValuationResult:
        formula = "企业价值 = 未来5年现金流折现值 + 永续价值折现值"
        latest = self._latest_matching(financial_data, ["cash_flow"], prefer_ttm=True)
        if not latest:
            return self._unavailable("DCF现金流折现", ["财务数据"], formula)

        cash_flow_wan = float(latest.get("cash_flow", 0) or 0)
        if cash_flow_wan <= 0:
            return self._unavailable("DCF现金流折现", ["经营现金流"], formula)

        wacc = float(params.get("wacc", 10.0)) / 100
        growth_rate = float(params.get("growth_rate", 6.0)) / 100
        perpetual_growth = float(params.get("perpetual_growth_rate", 2.0)) / 100
        if wacc <= perpetual_growth:
            return self._unavailable("DCF现金流折现", ["WACC需高于永续增长率"], formula)

        base_cf_yi = cash_flow_wan / 10000
        present_values: list[float] = []
        current_cf = base_cf_yi
        for year in range(1, 6):
            current_cf = current_cf * (1 + growth_rate)
            present_values.append(current_cf / ((1 + wacc) ** year))

        terminal_cf = current_cf * (1 + perpetual_growth)
        terminal_value = terminal_cf / (wacc - perpetual_growth)
        terminal_pv = terminal_value / ((1 + wacc) ** 5)
        base_value = sum(present_values) + terminal_pv
        return ValuationResult("DCF现金流折现", round(base_value * 0.85, 2), round(base_value, 2), round(base_value * 1.15, 2), True, [], formula)

    def calculate_pe(self, financial_data: list[dict], params: dict) -> ValuationResult:
        formula = "股权价值 = 净利润 × 行业PE"
        latest = self._latest_matching(financial_data, ["net_profit"], prefer_ttm=True)
        if not latest:
            return self._unavailable("市盈率法(PE)", ["财务数据"], formula)

        net_profit_wan = float(latest.get("net_profit", 0) or 0)
        if net_profit_wan <= 0:
            return self._unavailable("市盈率法(PE)", ["净利润"], formula)

        pe_multiple = float(params.get("pe_multiple", 20))
        base_value = net_profit_wan * pe_multiple / 10000
        return ValuationResult("市盈率法(PE)", round(base_value * 0.85, 2), round(base_value, 2), round(base_value * 1.15, 2), True, [], formula)

    def calculate_ps(self, financial_data: list[dict], params: dict) -> ValuationResult:
        formula = "股权价值 = 营业收入 × 行业PS"
        latest = self._latest_matching(financial_data, ["revenue"], prefer_ttm=True)
        if not latest:
            return self._unavailable("市销率法(PS)", ["财务数据"], formula)

        revenue_wan = float(latest.get("revenue", 0) or 0)
        if revenue_wan <= 0:
            return self._unavailable("市销率法(PS)", ["营业收入"], formula)

        ps_multiple = float(params.get("ps_multiple", 2.0))
        base_value = revenue_wan * ps_multiple / 10000
        return ValuationResult("市销率法(PS)", round(base_value * 0.85, 2), round(base_value, 2), round(base_value * 1.15, 2), True, [], formula)

    def calculate_ev_ebitda(self, financial_data: list[dict], params: dict) -> ValuationResult:
        formula = "股权价值 = EBITDA × 行业EV/EBITDA - 净负债"
        latest = self._latest_matching(financial_data, ["ebitda"], prefer_ttm=True)
        if not latest:
            return self._unavailable("EV/EBITDA法", ["财务数据"], formula)

        ebitda_wan = float(latest.get("ebitda", 0) or 0)
        net_debt_wan = float(latest.get("net_debt", 0) or 0)
        if ebitda_wan <= 0:
            return self._unavailable("EV/EBITDA法", ["EBITDA"], formula)

        multiple = float(params.get("ev_ebitda_multiple", 12))
        base_value = (ebitda_wan * multiple - net_debt_wan) / 10000
        return ValuationResult("EV/EBITDA法", round(base_value * 0.85, 2), round(base_value, 2), round(base_value * 1.15, 2), True, [], formula)

    def calculate_asset_based(self, financial_data: list[dict], params: dict) -> ValuationResult:
        formula = "股权价值 = 股东权益账面值 × 调整系数"
        latest = self._latest(financial_data)
        if not latest:
            return self._unavailable("资产基础法", ["财务数据"], formula)

        total_equity_wan = float(latest.get("total_equity", 0) or 0)
        if total_equity_wan <= 0:
            return self._unavailable("资产基础法", ["股东权益"], formula)

        base_value = total_equity_wan / 10000
        return ValuationResult("资产基础法", round(base_value * 0.90, 2), round(base_value, 2), round(base_value * 1.10, 2), True, [], formula)

    def calculate_comps(self, pe_result: ValuationResult, ps_result: ValuationResult, ev_ebitda_result: ValuationResult) -> ValuationResult:
        formula = "可比公司法 = 可用市场法结果的中位基准估值"
        values = [result.base_value for result in [pe_result, ps_result, ev_ebitda_result] if result.is_available]
        if not values:
            return self._unavailable("可比公司法", ["PE/PS/EV/EBITDA至少需一个可用"], formula)

        values.sort()
        middle = len(values) // 2
        base_value = values[middle] if len(values) % 2 == 1 else (values[middle - 1] + values[middle]) / 2
        return ValuationResult("可比公司法", round(base_value * 0.90, 2), round(base_value, 2), round(base_value * 1.10, 2), True, [], formula)

    def get_recommended_method(self, industry: str, results: list[ValuationResult]) -> tuple[str, str]:
        method_map = {
            "dcf": "DCF现金流折现",
            "relative_pe": "市盈率法(PE)",
            "relative_ps": "市销率法(PS)",
            "ev_ebitda": "EV/EBITDA法",
            "asset_based": "资产基础法",
            "comps": "可比公司法",
            "relative_pb": "资产基础法",
        }
        industries = self.industry_config.get("industries", {})
        recommended = industries.get(industry, industries.get("其他", {})).get("recommended_method", ["dcf", "relative_pe"])
        available_names = {result.method_name for result in results if result.is_available}
        for item in recommended:
            mapped = method_map.get(item, item)
            if mapped in available_names:
                return mapped, f"根据{industry}行业配置及当前数据完整度推荐。"
        if available_names:
            return sorted(available_names)[0], "行业首选方法不可用，已退回到当前可用方法。"
        return "无可用方法", "缺少必要数据，所有模型均无法计算。"

    def _build_formula_rows(self, results: list[ValuationResult], params: dict) -> list[dict]:
        rows: list[dict] = []
        for result in results:
            rows.append(
                {
                    "估值方法": result.method_name,
                    "公式": result.formula,
                    "核心参数": f"WACC={params.get('wacc')}%; 永续增长率={params.get('perpetual_growth_rate')}%; PE={params.get('pe_multiple')}; PS={params.get('ps_multiple')}; EV/EBITDA={params.get('ev_ebitda_multiple')}",
                    "是否可用": "是" if result.is_available else "否",
                }
            )
        return rows

    def _dcf_base_value(self, latest: dict | None, params: dict) -> float | None:
        if not latest:
            return None
        cash_flow_wan = float(latest.get("cash_flow", 0) or 0)
        if cash_flow_wan <= 0:
            return None
        wacc = float(params.get("wacc", 10.0)) / 100
        growth_rate = float(params.get("growth_rate", 6.0)) / 100
        perpetual_growth = float(params.get("perpetual_growth_rate", 2.0)) / 100
        if wacc <= perpetual_growth:
            return None
        base_cf_yi = cash_flow_wan / 10000
        present_values: list[float] = []
        current_cf = base_cf_yi
        for year in range(1, 6):
            current_cf = current_cf * (1 + growth_rate)
            present_values.append(current_cf / ((1 + wacc) ** year))
        terminal_cf = current_cf * (1 + perpetual_growth)
        terminal_value = terminal_cf / (wacc - perpetual_growth)
        terminal_pv = terminal_value / ((1 + wacc) ** 5)
        return round(sum(present_values) + terminal_pv, 2)

    def _build_sensitivity_rows(self, financial_data: list[dict], params: dict) -> list[dict]:
        latest = self._latest_matching(financial_data, ["cash_flow"], prefer_ttm=True)
        rows: list[dict] = []
        for wacc_delta in (-1.0, 0.0, 1.0):
            for growth_delta in (-1.0, 0.0, 1.0):
                scenario = dict(params)
                scenario["wacc"] = round(float(params.get("wacc", 10.0)) + wacc_delta, 2)
                scenario["growth_rate"] = round(float(params.get("growth_rate", 6.0)) + growth_delta, 2)
                value = self._dcf_base_value(latest, scenario)
                rows.append(
                    {
                        "WACC变动": f"{wacc_delta:+.1f}%",
                        "增长率变动": f"{growth_delta:+.1f}%",
                        "DCF估值(亿元)": "N/A" if value is None else value,
                    }
                )
        return rows

    def _build_warnings(self, financial_data: list[dict], params: dict, base_warnings: list[str] | None) -> list[str]:
        warnings = list(base_warnings or [])
        latest = self._latest(financial_data)
        if not latest:
            warnings.append("缺少标准化财务数据，估值结果不可用。")
            return warnings
        if float(params.get("wacc", 10.0)) <= float(params.get("perpetual_growth_rate", 2.0)):
            warnings.append("WACC 小于或等于永续增长率，DCF结果已自动禁用。")
        if float(latest.get("debt", 0) or 0) > float(latest.get("total_assets", 0) or 0):
            warnings.append("总负债高于总资产，输入数据存在异常，请人工复核。")
        if float(latest.get("net_profit", 0) or 0) <= 0:
            warnings.append("净利润非正值，盈利类估值方法参考意义有限。")
        if self._is_ttm_report(latest):
            warnings.append("当前收益类估值优先使用TTM滚动十二个月口径。")
        if latest.get("is_estimated"):
            warnings.append("当前标准化财务数据含估算口径，结果适合研究用途。")
        return warnings

    def run_full_valuation(self, company_info: dict, financial_data: list[dict], valuation_params: dict, base_warnings: list[str] | None = None) -> FinalValuation:
        params = {
            "risk_free_rate": valuation_params.get("risk_free_rate", 2.5),
            "market_risk_premium": valuation_params.get("market_risk_premium", 6.0),
            "beta": valuation_params.get("beta", 1.0),
            "wacc": valuation_params.get("wacc", 10.0),
            "growth_rate": valuation_params.get("growth_rate", 6.0),
            "perpetual_growth_rate": valuation_params.get("perpetual_growth_rate", 2.0),
            "pe_multiple": valuation_params.get("pe_multiple", 20),
            "ps_multiple": valuation_params.get("ps_multiple", 2.0),
            "ev_ebitda_multiple": valuation_params.get("ev_ebitda_multiple", 12),
            "cost_of_equity": valuation_params.get("cost_of_equity"),
            "cost_of_debt": valuation_params.get("cost_of_debt"),
            "tax_rate": valuation_params.get("tax_rate"),
            "debt_ratio": valuation_params.get("debt_ratio"),
        }

        dcf_result = self.calculate_dcf(financial_data, params)
        pe_result = self.calculate_pe(financial_data, params)
        ps_result = self.calculate_ps(financial_data, params)
        ev_ebitda_result = self.calculate_ev_ebitda(financial_data, params)
        asset_result = self.calculate_asset_based(financial_data, params)
        comps_result = self.calculate_comps(pe_result, ps_result, ev_ebitda_result)
        all_results = [dcf_result, pe_result, ps_result, ev_ebitda_result, asset_result, comps_result]
        recommended_method, recommended_reason = self.get_recommended_method(company_info.get("industry", "其他"), all_results)

        return FinalValuation(
            stock_code=company_info.get("stock_code", ""),
            company_name=company_info.get("company_name", ""),
            industry=company_info.get("industry", "其他"),
            dcf_result=dcf_result,
            pe_result=pe_result,
            ps_result=ps_result,
            ev_ebitda_result=ev_ebitda_result,
            asset_result=asset_result,
            comps_result=comps_result,
            recommended_method=recommended_method,
            recommended_reason=recommended_reason,
            all_params=params,
            warnings=self._build_warnings(financial_data, params, base_warnings),
            formula_rows=self._build_formula_rows(all_results, params),
            sensitivity_rows=self._build_sensitivity_rows(financial_data, params),
        )
