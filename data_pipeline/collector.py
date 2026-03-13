from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib import parse, request

import yaml

from data_pipeline.report_parser import CninfoReportParser


CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_FILE_PREFIX = "http://static.cninfo.com.cn/"
EASTMONEY_NOTICE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
COMPANY_SURVEY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"

INDUSTRY_NET_MARGIN = {
    "制造业": 0.055,
    "信息技术": 0.03,
    "金融业": 0.22,
    "房地产": 0.08,
    "零售业": 0.025,
    "能源": 0.09,
    "医药生物": 0.14,
    "其他": 0.05,
}

INDUSTRY_GROWTH = {
    "制造业": 0.08,
    "信息技术": 0.12,
    "金融业": 0.06,
    "房地产": 0.03,
    "零售业": 0.07,
    "能源": 0.05,
    "医药生物": 0.10,
    "其他": 0.08,
}

INDUSTRY_CASH_CONVERSION = {
    "制造业": 0.95,
    "信息技术": 1.05,
    "金融业": 0.90,
    "房地产": 0.75,
    "零售业": 1.00,
    "能源": 1.10,
    "医药生物": 0.92,
    "其他": 0.95,
}

INDUSTRY_EBITDA_MULTIPLIER = {
    "制造业": 1.45,
    "信息技术": 1.25,
    "金融业": 1.10,
    "房地产": 1.18,
    "零售业": 1.30,
    "能源": 1.55,
    "医药生物": 1.35,
    "其他": 1.30,
}


@dataclass
class Announcement:
    title: str
    publish_date: str
    file_url: str
    source: str
    detail_url: str = ""
    local_pdf: str = ""
    local_markdown: str = ""


@dataclass
class CompanyInfo:
    stock_code: str
    company_name: str
    industry: str
    market: str
    listing_date: str
    raw_industry: str


@dataclass
class MarketSnapshot:
    stock_code: str
    company_name: str
    market_cap_wan: float
    pe_ttm: float | None
    pb: float | None
    roe: float | None
    debt_ratio: float | None
    total_shares: float | None
    source: str


@dataclass
class FinancialData:
    year: int
    report_label: str
    report_date: str
    revenue: float
    net_profit: float
    total_assets: float
    total_equity: float
    cash_flow: float
    debt: float
    cash: float
    net_debt: float
    ebitda: float
    capex: float
    interest_expense: float
    source: str
    is_estimated: bool


@dataclass
class ValuationParams:
    risk_free_rate: float
    market_risk_premium: float
    beta: float
    wacc: float
    perpetual_growth_rate: float
    pe_multiple: float
    ps_multiple: float
    ev_ebitda_multiple: float
    cost_of_equity: float
    cost_of_debt: float
    tax_rate: float
    debt_ratio: float


class DataCollector:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.config = self._load_yaml(self.config_dir / "valuation_params.yaml")
        self.industry_config = self._load_yaml(self.config_dir / "industry_config.yaml")

    def _load_yaml(self, path: Path) -> dict:
        with path.open("r", encoding="utf-8") as file:
            return yaml.safe_load(file)

    def _request_json(self, url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
        merged_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if headers:
            merged_headers.update(headers)
        req = request.Request(url=url, data=data, headers=merged_headers)
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8-sig", errors="ignore")
        return json.loads(raw)

    def _market_from_stock_code(self, stock_code: str) -> str:
        if stock_code.startswith(("600", "601", "603", "605", "688")):
            return "sh"
        return "sz"

    def _code_with_market(self, stock_code: str, market: str) -> str:
        return f"{'SH' if market == 'sh' else 'SZ'}{stock_code}"

    def _secid(self, stock_code: str, market: str) -> str:
        return f"{'1' if market == 'sh' else '0'}.{stock_code}"

    def _guess_industry(self, text: str) -> str:
        industries = self.industry_config.get("industries", {})
        for industry, config in industries.items():
            for keyword in config.get("keywords", []):
                if keyword and keyword in text:
                    return industry
        return self.industry_config.get("default_industry", "其他")

    def _scaled_metric(self, value: object, divisor: float) -> float | None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric <= 0:
            return None
        return numeric / divisor

    def get_company_info(self, stock_code: str) -> CompanyInfo:
        market = self._market_from_stock_code(stock_code)
        code = self._code_with_market(stock_code, market)
        company_name = f"股票{stock_code}"
        raw_industry = ""
        listing_date = ""

        try:
            data = self._request_json(f"{COMPANY_SURVEY_URL}?code={code}")
            base = ((data.get("jbzl") or [{}])[0])
            company_name = base.get("SECURITY_NAME_ABBR") or base.get("ORG_NAME") or company_name
            raw_industry = base.get("EM2016") or base.get("INDUSTRYCSRC") or ""
            listing_date = base.get("LISTING_DATE") or ""
        except Exception:
            pass

        industry = self._guess_industry(raw_industry or company_name)
        return CompanyInfo(
            stock_code=stock_code,
            company_name=company_name,
            industry=industry,
            market=market,
            listing_date=listing_date,
            raw_industry=raw_industry,
        )

    def get_market_snapshot(self, company_info: CompanyInfo) -> MarketSnapshot:
        fields = "f57,f58,f116,f117,f162,f167,f173,f188,f84"
        url = f"{QUOTE_URL}?secid={self._secid(company_info.stock_code, company_info.market)}&fields={fields}"
        data = self._request_json(url)
        info = data.get("data") or {}
        return MarketSnapshot(
            stock_code=company_info.stock_code,
            company_name=company_info.company_name,
            market_cap_wan=float(info.get("f116") or 0) / 10000,
            pe_ttm=self._scaled_metric(info.get("f162"), 100),
            pb=self._scaled_metric(info.get("f167"), 100),
            roe=self._scaled_metric(info.get("f173"), 1),
            debt_ratio=self._scaled_metric(info.get("f188"), 1),
            total_shares=self._scaled_metric(info.get("f84"), 1),
            source="eastmoney-quote",
        )

    def _fetch_cninfo_announcements(self, stock_code: str, market: str) -> list[Announcement]:
        payload = parse.urlencode(
            {
                "pageNum": "1",
                "pageSize": "30",
                "column": "szse" if market == "sz" else "sse",
                "tabName": "fulltext",
                "plate": market,
                "stock": f"{stock_code},{market}",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
        ).encode("utf-8")
        data = self._request_json(
            CNINFO_QUERY_URL,
            data=payload,
            headers={
                "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        results: list[Announcement] = []
        for item in data.get("announcements") or []:
            title = str(item.get("announcementTitle", "")).strip()
            publish_time = int(item.get("announcementTime", 0) or 0)
            adjunct_url = str(item.get("adjunctUrl", "")).lstrip("/")
            if not title or not publish_time or not adjunct_url:
                continue
            results.append(
                Announcement(
                    title=title,
                    publish_date=datetime.fromtimestamp(publish_time / 1000).strftime("%Y-%m-%d"),
                    file_url=parse.urljoin(CNINFO_FILE_PREFIX, adjunct_url),
                    source="cninfo",
                )
            )
        return results

    def _fetch_fallback_announcements(self, stock_code: str) -> list[Announcement]:
        url = f"{EASTMONEY_NOTICE_URL}?{parse.urlencode({'sr': '-1', 'page_size': '50', 'page_index': '1', 'ann_type': 'A', 'client_source': 'web', 'stock_list': stock_code})}"
        data = self._request_json(url)
        results: list[Announcement] = []
        for item in ((data.get("data") or {}).get("list")) or []:
            title = str(item.get("title", "")).strip()
            art_code = str(item.get("art_code", "")).strip()
            notice_date = str(item.get("notice_date", "")).strip()
            if not title or not art_code or not notice_date:
                continue
            results.append(
                Announcement(
                    title=title,
                    publish_date=notice_date[:10],
                    file_url=f"https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf",
                    source="eastmoney-fallback",
                )
            )
        return results

    def collect_announcements(self, stock_code: str, market: str) -> list[Announcement]:
        include_keywords = ("年度报告", "半年度报告", "三季度报告", "一季度报告")
        exclude_keywords = ("摘要", "英文", "董事会", "监事会", "制度", "公告")
        try:
            announcements = self._fetch_cninfo_announcements(stock_code, market)
            if not announcements:
                announcements = self._fetch_fallback_announcements(stock_code)
        except Exception:
            announcements = self._fetch_fallback_announcements(stock_code)

        filtered: list[Announcement] = []
        for item in announcements:
            if not any(keyword in item.title for keyword in include_keywords):
                continue
            if any(keyword in item.title for keyword in exclude_keywords):
                continue
            filtered.append(item)
        filtered.sort(key=lambda item: item.publish_date, reverse=True)
        return filtered[:10]

    def _estimated_financial_data(self, company_info: CompanyInfo, snapshot: MarketSnapshot) -> tuple[list[FinancialData], list[str]]:
        warnings: list[str] = []
        if snapshot.market_cap_wan <= 0:
            return [], ["未抓取到有效市值，无法构建标准化财务数据。"]

        industry = company_info.industry
        margin = INDUSTRY_NET_MARGIN.get(industry, INDUSTRY_NET_MARGIN["其他"])
        growth = INDUSTRY_GROWTH.get(industry, INDUSTRY_GROWTH["其他"])
        cash_conversion = INDUSTRY_CASH_CONVERSION.get(industry, INDUSTRY_CASH_CONVERSION["其他"])
        ebitda_multiplier = INDUSTRY_EBITDA_MULTIPLIER.get(industry, INDUSTRY_EBITDA_MULTIPLIER["其他"])

        pe_ttm = snapshot.pe_ttm or float(self.config["industry_multiples"]["pe"].get(industry, 20))
        pb = snapshot.pb or 1.5
        debt_ratio = min(max(snapshot.debt_ratio or 45.0, 5.0), 90.0)

        net_profit_wan = snapshot.market_cap_wan / pe_ttm if pe_ttm else 0
        total_equity_wan = snapshot.market_cap_wan / pb if pb else snapshot.market_cap_wan / 1.5
        revenue_wan = net_profit_wan / margin if margin > 0 else 0
        cash_flow_wan = net_profit_wan * cash_conversion
        ebitda_wan = net_profit_wan * ebitda_multiplier
        debt_wan = total_equity_wan * debt_ratio / max(100 - debt_ratio, 1)
        total_assets_wan = total_equity_wan + debt_wan

        warnings.append("标准化财务数据当前由市场行情指标和行业参数推算生成，建议后续接入财报正文解析以提升精度。")
        if snapshot.pe_ttm is None:
            warnings.append("未抓到实时PE，已使用行业默认PE回填净利润。")
        if snapshot.pb is None:
            warnings.append("未抓到实时PB，已使用默认PB回填净资产。")
        if snapshot.debt_ratio is None:
            warnings.append("未抓到资产负债率，已使用默认负债率估算。")

        latest_year = datetime.now().year - 1
        rows: list[FinancialData] = []
        for offset in range(3):
            factor = (1 + growth) ** offset
            rows.append(
                FinancialData(
                    year=latest_year - offset,
                    report_label=f"{latest_year - offset}估算",
                    report_date=f"{latest_year - offset}-12-31",
                    revenue=round(revenue_wan / factor, 2),
                    net_profit=round(net_profit_wan / factor, 2),
                    total_assets=round(total_assets_wan / factor, 2),
                    total_equity=round(total_equity_wan / factor, 2),
                    cash_flow=round(cash_flow_wan / factor, 2),
                    debt=round(debt_wan / factor, 2),
                    cash=round(total_assets_wan * 0.12 / factor, 2),
                    net_debt=round((debt_wan - total_assets_wan * 0.12) / factor, 2),
                    ebitda=round(ebitda_wan / factor, 2),
                    capex=round(revenue_wan * 0.01 / factor, 2),
                    interest_expense=round(debt_wan * float(self.config["wacc"].get("cost_of_debt", 4.5)) / 100 / factor, 2),
                    source=snapshot.source,
                    is_estimated=True,
                )
            )
        return rows, warnings

    def _build_ttm_row(self, documents: list, parsed_report_rows: list[dict]) -> tuple[FinancialData | None, list[str]]:
        warnings: list[str] = []
        if not documents:
            return None, warnings

        latest = sorted(documents, key=lambda item: item.publish_date, reverse=True)[0]
        if latest.report_type == "A":
            return None, warnings

        document_map = {item.report_key: item for item in documents}
        latest_annual_key = f"{latest.year - 1}A"
        prior_same_key = f"{latest.year - 1}{latest.report_type}"
        annual_doc = document_map.get(latest_annual_key)
        prior_same_doc = document_map.get(prior_same_key)
        if annual_doc is None or prior_same_doc is None:
            warnings.append("缺少上年同期或最近年报，无法合成TTM口径。")
            return None, warnings

        latest_metrics = latest.metrics
        annual_metrics = annual_doc.metrics
        prior_metrics = prior_same_doc.metrics

        def combine_flow(field: str) -> float | None:
            latest_value = latest_metrics.get(field)
            annual_value = annual_metrics.get(field)
            prior_value = prior_metrics.get(field)
            if latest_value is None or annual_value is None or prior_value is None:
                return None
            return round(float(annual_value) + float(latest_value) - float(prior_value), 2)

        ttm_revenue = combine_flow("revenue_wan")
        ttm_net_profit = combine_flow("net_profit_wan")
        ttm_cash_flow = combine_flow("cash_flow_wan")
        ttm_capex = combine_flow("capex_wan")
        ttm_interest_expense = combine_flow("interest_expense_wan")
        ttm_ebitda = combine_flow("ebitda_wan")
        if ttm_ebitda is None and annual_metrics.get("ebitda_wan") is not None:
            ttm_ebitda = round(float(annual_metrics.get("ebitda_wan") or 0), 2)
            warnings.append("TTM EBITDA 暂无法完整由中报/季报推导，已回退使用最近年报 EBITDA。")

        required_values = [ttm_revenue, ttm_net_profit, ttm_cash_flow]
        if any(value is None for value in required_values):
            warnings.append("TTM 口径关键流量字段不完整，未生成TTM标准化记录。")
            return None, warnings

        ttm_row = FinancialData(
            year=latest.year,
            report_label=f"{latest.year}TTM",
            report_date=latest.publish_date,
            revenue=ttm_revenue,
            net_profit=ttm_net_profit,
            total_assets=round(float(latest_metrics.get("total_assets_wan") or 0), 2),
            total_equity=round(float(latest_metrics.get("total_equity_wan") or 0), 2),
            cash_flow=ttm_cash_flow,
            debt=round(float(latest_metrics.get("debt_wan") or 0), 2),
            cash=round(float(latest_metrics.get("cash_wan") or 0), 2),
            net_debt=round(float(latest_metrics.get("net_debt_wan") or 0), 2),
            ebitda=round(float(ttm_ebitda or 0), 2),
            capex=round(float(ttm_capex or 0), 2),
            interest_expense=round(float(ttm_interest_expense or 0), 2),
            source="cninfo-ttm",
            is_estimated=False,
        )
        parsed_report_rows.insert(
            0,
            {
                "报告期": ttm_row.report_label,
                "标题": "TTM滚动十二个月合成",
                "公告日期": latest.publish_date,
                "PDF文件": "-",
                "正文文件": "-",
                "营业收入(万元)": ttm_row.revenue,
                "净利润(万元)": ttm_row.net_profit,
                "总资产(万元)": ttm_row.total_assets,
                "股东权益(万元)": ttm_row.total_equity,
                "经营现金流(万元)": ttm_row.cash_flow,
                "总负债(万元)": ttm_row.debt,
                "现金及交易性金融资产(万元)": ttm_row.cash,
                "净负债(万元)": ttm_row.net_debt,
                "资本开支(万元)": ttm_row.capex,
                "利息费用(万元)": ttm_row.interest_expense,
                "EBITDA(万元)": ttm_row.ebitda,
            },
        )
        warnings.append("已基于最新报告、上年同期和最近年报合成TTM口径。")
        return ttm_row, warnings

    def get_financial_data(self, company_info: CompanyInfo, snapshot: MarketSnapshot, company_dir: Path) -> tuple[list[FinancialData], list[Announcement], list[dict], list[str]]:
        parser = CninfoReportParser(company_info.stock_code, company_info.company_name)
        documents, warnings = parser.download_and_parse_reports(company_dir)

        real_rows: list[FinancialData] = []
        announcements: list[Announcement] = []
        parsed_report_rows: list[dict] = []
        for document in documents:
            metrics = document.metrics
            announcements.append(
                Announcement(
                    title=document.title,
                    publish_date=document.publish_date,
                    file_url=document.pdf_url,
                    source="cninfo-pdf",
                    detail_url=document.detail_url,
                    local_pdf=document.pdf_path.name,
                    local_markdown=document.markdown_path.name,
                )
            )
            parsed_report_rows.append(
                {
                    "报告期": document.report_key,
                    "标题": document.title,
                    "公告日期": document.publish_date,
                    "PDF文件": document.pdf_path.name,
                    "正文文件": document.markdown_path.name,
                    "营业收入(万元)": metrics.get("revenue_wan"),
                    "净利润(万元)": metrics.get("net_profit_wan"),
                    "总资产(万元)": metrics.get("total_assets_wan"),
                    "股东权益(万元)": metrics.get("total_equity_wan"),
                    "经营现金流(万元)": metrics.get("cash_flow_wan"),
                    "总负债(万元)": metrics.get("debt_wan"),
                    "现金及交易性金融资产(万元)": round(float(metrics.get("cash_wan") or 0) + float(metrics.get("trading_assets_wan") or 0), 2) if metrics.get("cash_wan") is not None or metrics.get("trading_assets_wan") is not None else None,
                    "净负债(万元)": metrics.get("net_debt_wan"),
                    "资本开支(万元)": metrics.get("capex_wan"),
                    "利息费用(万元)": metrics.get("interest_expense_wan"),
                    "EBITDA(万元)": metrics.get("ebitda_wan"),
                }
            )
            if metrics.get("revenue_wan") is None or metrics.get("net_profit_wan") is None or metrics.get("total_assets_wan") is None or metrics.get("total_equity_wan") is None:
                warnings.append(f"财报已下载但关键字段抽取不完整: {document.title}")
                continue
            real_rows.append(
                FinancialData(
                    year=document.year,
                    report_label=document.report_key,
                    report_date=document.publish_date,
                    revenue=round(float(metrics.get("revenue_wan") or 0), 2),
                    net_profit=round(float(metrics.get("net_profit_wan") or 0), 2),
                    total_assets=round(float(metrics.get("total_assets_wan") or 0), 2),
                    total_equity=round(float(metrics.get("total_equity_wan") or 0), 2),
                    cash_flow=round(float(metrics.get("cash_flow_wan") or 0), 2),
                    debt=round(float(metrics.get("debt_wan") or 0), 2),
                    cash=round(float(metrics.get("cash_wan") or 0) + float(metrics.get("trading_assets_wan") or 0), 2),
                    net_debt=round(float(metrics.get("net_debt_wan") or 0), 2),
                    ebitda=round(float(metrics.get("ebitda_wan") or 0), 2),
                    capex=round(float(metrics.get("capex_wan") or 0), 2),
                    interest_expense=round(float(metrics.get("interest_expense_wan") or 0), 2),
                    source="cninfo-pdf",
                    is_estimated=False,
                )
            )

        if real_rows:
            ttm_row, ttm_warnings = self._build_ttm_row(documents, parsed_report_rows)
            warnings.extend(ttm_warnings)
            if ttm_row is not None:
                real_rows.insert(0, ttm_row)
            real_rows.sort(key=lambda row: (row.year, row.report_date), reverse=True)
            if len(real_rows) >= 4:
                return real_rows[:4], announcements, parsed_report_rows, warnings

            estimated_rows, estimate_warnings = self._estimated_financial_data(company_info, snapshot)
            warnings.extend(estimate_warnings)
            existing_labels = {row.report_label for row in real_rows}
            for row in estimated_rows:
                if row.report_label in existing_labels:
                    continue
                real_rows.append(row)
                if len(real_rows) >= 4:
                    break
            warnings.append("部分年度未成功从PDF提取，已用估算值补足缺失期间。")
            real_rows.sort(key=lambda row: (row.year, row.report_date), reverse=True)
            return real_rows[:4], announcements, parsed_report_rows, warnings

        estimated_rows, estimate_warnings = self._estimated_financial_data(company_info, snapshot)
        warnings.extend(estimate_warnings)
        warnings.append("未成功解析到可用财报PDF，已回退为估算口径。")
        return estimated_rows, announcements, parsed_report_rows, warnings

    def _fetch_treasury_rate(self) -> float | None:
        try:
            data = self._request_json("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.0003002&fields=f2")
            diff = ((data.get("data") or {}).get("diff")) or []
            if not diff:
                return None
            value = float(diff[0].get("f2") or 0)
            if value > 0:
                return value
        except Exception:
            return None
        return None

    def get_valuation_params(self, company_info: CompanyInfo, snapshot: MarketSnapshot) -> ValuationParams:
        industry = company_info.industry
        risk_free_rate = self._fetch_treasury_rate() or float(self.config["risk_free_rate"]["default"])
        market_risk_premium = float(self.config["market_risk_premium"]["default"])
        beta_method = self.config["beta"].get("method", "industry")
        if beta_method == "custom":
            beta = float(self.config["beta"].get("custom", 1.0))
        else:
            beta = float(self.config["beta"]["industry_unlevered_beta"].get(industry, 1.0))

        cost_of_equity = risk_free_rate + beta * market_risk_premium
        cost_of_debt = float(self.config["wacc"].get("cost_of_debt", 4.5))
        tax_rate = float(self.config["wacc"].get("tax_rate", 25.0))
        debt_ratio = min(max(snapshot.debt_ratio or 45.0, 5.0), 90.0) / 100
        default_wacc = float(self.config["wacc"].get("default", 10.0))
        computed_wacc = cost_of_equity * (1 - debt_ratio) + cost_of_debt * (1 - tax_rate / 100) * debt_ratio
        wacc = max(computed_wacc, default_wacc * 0.8)

        perpetual_range = self.config["perpetual_growth_rate"]["ranges"].get(industry, [1.5, 2.5])
        perpetual_growth_rate = sum(perpetual_range) / len(perpetual_range)

        return ValuationParams(
            risk_free_rate=round(risk_free_rate, 2),
            market_risk_premium=round(market_risk_premium, 2),
            beta=round(beta, 2),
            wacc=round(wacc, 2),
            perpetual_growth_rate=round(perpetual_growth_rate, 2),
            pe_multiple=float(self.config["industry_multiples"]["pe"].get(industry, 20)),
            ps_multiple=float(self.config["industry_multiples"]["ps"].get(industry, 2.0)),
            ev_ebitda_multiple=float(self.config["industry_multiples"]["ev_ebitda"].get(industry, 12)),
            cost_of_equity=round(cost_of_equity, 2),
            cost_of_debt=round(cost_of_debt, 2),
            tax_rate=round(tax_rate, 2),
            debt_ratio=round(debt_ratio * 100, 2),
        )

    def save_company_data(self, stock_code: str, base_dir: Path) -> dict:
        base_dir.mkdir(parents=True, exist_ok=True)
        company_info = self.get_company_info(stock_code)
        snapshot = self.get_market_snapshot(company_info)

        company_dir = base_dir / stock_code
        company_dir.mkdir(parents=True, exist_ok=True)

        financial_data, announcements, parsed_report_rows, warnings = self.get_financial_data(company_info, snapshot, company_dir)
        if not announcements:
            announcements = self.collect_announcements(stock_code, company_info.market)
        valuation_params = self.get_valuation_params(company_info, snapshot)
        if valuation_params.wacc <= float(self.config["wacc"].get("default", 10.0)) * 0.8:
            warnings.append("市场隐含WACC偏低，已按配置下限修正，以避免DCF估值失真。")

        self._save_announcements_md(company_dir, company_info, announcements)
        self._save_snapshot_json(company_dir, snapshot)
        self._save_financial_csv(company_dir, financial_data)
        self._save_params_csv(company_dir, valuation_params)
        self._save_parsed_reports_csv(company_dir, parsed_report_rows)

        return {
            "company_dir": company_dir,
            "company_info": company_info,
            "snapshot": snapshot,
            "announcements": announcements,
            "financial_data": financial_data,
            "valuation_params": valuation_params,
            "warnings": warnings,
        }

    def _save_announcements_md(self, company_dir: Path, company_info: CompanyInfo, announcements: list[Announcement]) -> None:
        lines = [
            f"# {company_info.company_name}({company_info.stock_code}) 财报公告",
            "",
            f"- 行业: {company_info.industry}",
            f"- 原始行业标签: {company_info.raw_industry or 'N/A'}",
            f"- 抓取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 公告数量: {len(announcements)}",
            "",
            "| 日期 | 标题 | 数据源 | PDF链接 | 本地PDF | 正文MD |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in announcements:
            local_pdf = item.local_pdf if item.local_pdf else "-"
            local_md = item.local_markdown if item.local_markdown else "-"
            lines.append(f"| {item.publish_date} | {item.title} | {item.source} | [下载]({item.file_url}) | {local_pdf} | {local_md} |")
        (company_dir / "财报公告.md").write_text("\n".join(lines), encoding="utf-8")

    def _save_snapshot_json(self, company_dir: Path, snapshot: MarketSnapshot) -> None:
        (company_dir / "原始行情快照.json").write_text(
            json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save_financial_csv(self, company_dir: Path, financial_data: list[FinancialData]) -> None:
        lines = ["年份,报告期,公告日期,营业收入(万元),净利润(万元),总资产(万元),股东权益(万元),经营现金流(万元),总负债(万元),现金及交易性金融资产(万元),净负债(万元),资本开支(万元),利息费用(万元),EBITDA(万元),数据源,是否估算"]
        for row in financial_data:
            lines.append(
                f"{row.year},{row.report_label},{row.report_date},{row.revenue},{row.net_profit},{row.total_assets},{row.total_equity},{row.cash_flow},{row.debt},{row.cash},{row.net_debt},{row.capex},{row.interest_expense},{row.ebitda},{row.source},{'是' if row.is_estimated else '否'}"
            )
        (company_dir / "标准化财务数据.csv").write_text("\n".join(lines), encoding="utf-8")

    def _save_parsed_reports_csv(self, company_dir: Path, parsed_report_rows: list[dict]) -> None:
        lines = ["报告期,标题,公告日期,PDF文件,正文文件,营业收入(万元),净利润(万元),总资产(万元),股东权益(万元),经营现金流(万元),总负债(万元),现金及交易性金融资产(万元),净负债(万元),资本开支(万元),利息费用(万元),EBITDA(万元)"]
        for row in parsed_report_rows:
            lines.append(
                f"{row.get('报告期','')},{row.get('标题','')},{row.get('公告日期','')},{row.get('PDF文件','')},{row.get('正文文件','')},{row.get('营业收入(万元)','')},{row.get('净利润(万元)','')},{row.get('总资产(万元)','')},{row.get('股东权益(万元)','')},{row.get('经营现金流(万元)','')},{row.get('总负债(万元)','')},{row.get('现金及交易性金融资产(万元)','')},{row.get('净负债(万元)','')},{row.get('资本开支(万元)','')},{row.get('利息费用(万元)','')},{row.get('EBITDA(万元)','')}"
            )
        (company_dir / "财报解析结果.csv").write_text("\n".join(lines), encoding="utf-8")

    def _save_params_csv(self, company_dir: Path, params: ValuationParams) -> None:
        lines = [
            "参数名称,数值,单位",
            f"无风险利率,{params.risk_free_rate},%",
            f"市场风险溢价,{params.market_risk_premium},%",
            f"Beta,{params.beta},",
            f"权益成本,{params.cost_of_equity},%",
            f"债务成本,{params.cost_of_debt},%",
            f"税率,{params.tax_rate},%",
            f"债务比率,{params.debt_ratio},%",
            f"WACC,{params.wacc},%",
            f"永续增长率,{params.perpetual_growth_rate},%",
            f"PE倍数,{params.pe_multiple},",
            f"PS倍数,{params.ps_multiple},",
            f"EV/EBITDA倍数,{params.ev_ebitda_multiple},",
        ]
        (company_dir / "估值参数.csv").write_text("\n".join(lines), encoding="utf-8")