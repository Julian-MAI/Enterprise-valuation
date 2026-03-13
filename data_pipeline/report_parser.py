from __future__ import annotations

import contextlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib import parse, request

import akshare as ak
import pdfplumber


CNINFO_DETAIL_API = "http://www.cninfo.com.cn/new/announcement/bulletin_detail"

REPORT_TYPE_MAP = {
    "年报": "A",
    "半年报": "H1",
    "一季报": "Q1",
    "三季报": "Q3",
}


@dataclass
class ReportDocument:
    report_key: str
    report_type: str
    year: int
    title: str
    publish_date: str
    announcement_id: str
    detail_url: str
    pdf_url: str
    pdf_path: Path
    markdown_path: Path
    metrics: dict


class CninfoReportParser:
    def __init__(self, stock_code: str, company_name: str):
        self.stock_code = stock_code
        self.company_name = company_name

    def _request_json(self, url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
        merged_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if headers:
            merged_headers.update(headers)
        req = request.Request(url, data=data, headers=merged_headers)
        with request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8-sig", errors="ignore")
        return json.loads(raw)

    def _clean_title(self, title: str) -> bool:
        excluded = ("摘要", "英文", "更新前", "取消", "说明")
        return not any(item in title for item in excluded)

    def _extract_announcement_id(self, detail_url: str) -> str:
        parsed = parse.urlparse(detail_url)
        query = parse.parse_qs(parsed.query)
        values = query.get("announcementId", [""])
        return values[0]

    def _resolve_pdf_url(self, announcement_id: str, publish_date: str) -> str:
        payload = parse.urlencode(
            {"announceId": announcement_id, "flag": "true", "announceTime": publish_date}
        ).encode("utf-8")
        data = self._request_json(
            CNINFO_DETAIL_API,
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"http://www.cninfo.com.cn/new/disclosure/detail?announcementId={announcement_id}&announcementTime={publish_date}",
            },
        )
        return str(data.get("fileUrl", "")).strip()

    def _numeric_value(self, text: object) -> float | None:
        if text is None:
            return None
        cleaned = re.sub(r"[^0-9.\-]", "", str(text))
        if not cleaned or cleaned in {"-", ".", "-."}:
            return None
        return float(cleaned)

    def _value_to_wan(self, value: object, label: str) -> float | None:
        numeric = self._numeric_value(value)
        if numeric is None:
            return None
        normalized_label = label.replace(" ", "")
        if "亿元" in normalized_label:
            return round(numeric * 10000, 2)
        if "万元" in normalized_label:
            return round(numeric, 2)
        if "元" in normalized_label:
            return round(numeric / 10000, 2)
        if abs(numeric) >= 1_000_000:
            return round(numeric / 10000, 2)
        return round(numeric, 2)

    def _yuan_to_wan(self, value: object) -> float | None:
        numeric = self._numeric_value(value)
        if numeric is None:
            return None
        return round(numeric / 10000, 2)

    def _normalize_table_records(self, table: list[list[object]]) -> list[dict]:
        records: list[dict] = []
        current: dict | None = None

        def has_number(row: list[object]) -> bool:
            return any(cell and re.search(r"\d", str(cell)) for cell in row[3:])

        def label_part(row: list[object]) -> str:
            return "".join(str(cell).strip() for cell in row[:3] if cell and str(cell).strip())

        for row in table:
            fragment = label_part(row)
            if has_number(row):
                if current:
                    records.append(current)
                current = {"label": fragment, "row": row}
            elif current and fragment:
                current["label"] += fragment

        if current:
            records.append(current)
        return records

    def _find_metric_row(self, records: list[dict], aliases: tuple[str, ...], exclude_aliases: tuple[str, ...] = ()) -> dict | None:
        for record in records:
            label = record["label"].replace(" ", "")
            if any(alias in label for alias in aliases) and not any(exclude in label for exclude in exclude_aliases):
                return record
        return None

    def _resolve_debt_wan(
        self,
        extracted_debt_wan: float | None,
        total_assets_wan: float | None,
        total_equity_wan: float | None,
    ) -> float | None:
        balance_sheet_debt = None
        if total_assets_wan is not None and total_equity_wan is not None:
            balance_sheet_debt = round(max(total_assets_wan - total_equity_wan, 0), 2)
        if balance_sheet_debt is not None:
            return balance_sheet_debt
        return extracted_debt_wan

    def _extract_debt_wan(self, full_text: str) -> float | None:
        match = re.search(r"负债合计\s+([0-9,\.]+)", full_text)
        if not match:
            return None
        value = self._numeric_value(match.group(1))
        if value is None:
            return None
        return round(value / 10000, 2)

    def _extract_ebitda_wan(self, full_text: str, debt_wan: float | None) -> float | None:
        if debt_wan is None:
            return None
        match = re.search(r"EBITDA全部债务比\s+([0-9.]+)%", full_text)
        if not match:
            return None
        ratio = self._numeric_value(match.group(1))
        if ratio is None:
            return None
        return round(debt_wan * ratio / 100, 2)

    def _extract_value_from_patterns(self, full_text: str, patterns: list[str], *, divisor: float = 10000) -> float | None:
        for pattern in patterns:
            match = re.search(pattern, full_text, re.S)
            if not match:
                continue
            value = self._numeric_value(match.group(1))
            if value is None:
                continue
            return round(value / divisor, 2) if divisor != 1 else round(value, 2)
        return None

    def _extract_section(self, full_text: str, start_markers: list[str], end_markers: list[str]) -> str:
        chapter_anchor = max(
            full_text.rfind("二、财务报表"),
            full_text.rfind("四、季度财务报表"),
            full_text.rfind("（一） 财务报表"),
        )
        search_base = full_text[chapter_anchor:] if chapter_anchor >= 0 else full_text
        start_index = -1
        for marker in start_markers:
            found = search_base.find(marker)
            if found >= 0:
                start_index = found
                break
        if start_index < 0:
            return search_base

        absolute_start = (chapter_anchor if chapter_anchor >= 0 else 0) + start_index

        end_index = len(full_text)
        for marker in end_markers:
            found = full_text.find(marker, absolute_start + 1)
            if found >= 0:
                end_index = min(end_index, found)
        return full_text[absolute_start:end_index]

    def _sum_values(self, *values: float | None) -> float | None:
        valid_values = [value for value in values if value is not None]
        if not valid_values:
            return None
        return round(sum(valid_values), 2)

    def _extract_split_line_value(self, full_text: str, prefix: str, suffix: str, *, divisor: float = 10000) -> float | None:
        lines = [line.strip() for line in full_text.splitlines()]
        for index, line in enumerate(lines):
            if prefix not in line:
                continue
            for offset in (1, 2):
                if index + offset >= len(lines):
                    continue
                number_line = lines[index + offset]
                tail_index = index + offset + 1
                if tail_index >= len(lines):
                    continue
                if suffix not in lines[tail_index]:
                    continue
                numbers = re.findall(r"-?\d[\d,]*\.?\d*", number_line)
                if not numbers:
                    continue
                value = self._numeric_value(numbers[0])
                if value is None:
                    continue
                return round(value / divisor, 2) if divisor != 1 else round(value, 2)
        return None

    def _extract_balance_sheet_metrics(self, full_text: str) -> dict:
        balance_section = self._extract_section(
            full_text,
            ["1、合并资产负债表", "1、合并资产负债表\n", "合并资产负债表"],
            ["2、母公司资产负债表", "2、合并利润表", "2、利润表"],
        )
        cash_wan = self._extract_value_from_patterns(
            balance_section,
            [r"货币资金\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        trading_assets_wan = self._extract_value_from_patterns(
            balance_section,
            [r"交易性金融资产\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        short_debt_wan = self._extract_value_from_patterns(
            balance_section,
            [r"短期借款\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        current_portion_wan = self._extract_value_from_patterns(
            balance_section,
            [r"一年内到期的非流动负债\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        long_debt_wan = self._extract_value_from_patterns(
            balance_section,
            [r"长期借款\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        interest_bearing_debt_wan = self._sum_values(short_debt_wan, current_portion_wan, long_debt_wan)

        net_debt_wan = None
        if interest_bearing_debt_wan is not None:
            liquid_assets_wan = sum(value for value in [cash_wan, trading_assets_wan] if value is not None)
            net_debt_wan = round(interest_bearing_debt_wan - liquid_assets_wan, 2)

        return {
            "cash_wan": cash_wan,
            "trading_assets_wan": trading_assets_wan,
            "interest_bearing_debt_wan": interest_bearing_debt_wan,
            "net_debt_wan": net_debt_wan,
        }

    def _extract_profit_bridge_metrics(self, full_text: str) -> dict:
        profit_section = self._extract_section(
            full_text,
            ["2、合并利润表", "3、合并利润表", "合并利润表"],
            ["3、合并现金流量表", "4、母公司利润表", "4、合并现金流量表"],
        )
        cashflow_section = self._extract_section(
            full_text,
            ["3、合并现金流量表", "5、合并现金流量表", "合并现金流量表"],
            ["4、母公司资产负债表", "6、母公司现金流量表", "7、合并所有者权益变动表"],
        )
        profit_total_wan = self._extract_value_from_patterns(
            profit_section,
            [r"四、利润总额[^\n]*?\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        tax_expense_wan = self._extract_value_from_patterns(
            profit_section,
            [r"减：所得税费用\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        interest_expense_wan = self._extract_value_from_patterns(
            profit_section,
            [
                r"其中：利息费用\s+([0-9,\.\-]+)\s+[0-9,\.\-]+",
                r"利息支出\s+([0-9,\.\-]+)\s+[0-9,\.\-]+",
            ],
        )
        capex_wan = self._extract_value_from_patterns(
            cashflow_section,
            [r"购建固定资产、无形资产和其他长\s*期资产支付的现金\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        if capex_wan is None:
            capex_wan = self._extract_split_line_value(
                cashflow_section,
                "购建固定资产、无形资产和其他长",
                "期资产支付的现金",
            )
        fixed_dep_wan = self._extract_value_from_patterns(
            full_text,
            [r"固定资产折旧、油气资产折\s*耗、生产性生物资产折旧\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        use_right_dep_wan = self._extract_value_from_patterns(
            full_text,
            [r"使用权资产折旧\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        intangible_amort_wan = self._extract_value_from_patterns(
            full_text,
            [r"无形资产摊销\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        deferred_amort_wan = self._extract_value_from_patterns(
            full_text,
            [r"长期待摊费用摊销\s+([0-9,\.\-]+)\s+[0-9,\.\-]+"],
        )
        depreciation_amortization_wan = self._sum_values(
            fixed_dep_wan,
            use_right_dep_wan,
            intangible_amort_wan,
            deferred_amort_wan,
        )

        ebitda_wan = None
        if profit_total_wan is not None and interest_expense_wan is not None and depreciation_amortization_wan is not None:
            ebitda_wan = round(profit_total_wan + interest_expense_wan + depreciation_amortization_wan, 2)

        return {
            "profit_total_wan": profit_total_wan,
            "tax_expense_wan": tax_expense_wan,
            "interest_expense_wan": interest_expense_wan,
            "capex_wan": capex_wan,
            "depreciation_amortization_wan": depreciation_amortization_wan,
            "ebitda_wan": ebitda_wan,
        }

    def _report_sort_key(self, item: dict) -> tuple[str, int]:
        priority_map = {"Q3": 4, "H1": 3, "Q1": 2, "A": 1}
        return item["publish_date"], priority_map.get(item["report_type"], 0)

    def _same_period_previous_key(self, item: dict) -> str | None:
        if item["report_type"] == "A":
            return None
        return f"{item['year'] - 1}{item['report_type']}"

    def _annual_key(self, year: int) -> str:
        return f"{year}A"

    def _find_key_table_records(self, pdf: pdfplumber.PDF) -> tuple[list[dict], str]:
        full_text_parts: list[str] = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text_parts.append(text)
            if "主要会计数据" not in text and "主要财务数据" not in text and "营业收入" not in text:
                continue
            for table in page.extract_tables() or []:
                records = self._normalize_table_records(table)
                if any("营业收入" in record["label"] for record in records):
                    return records, "\n".join(full_text_parts + [t.extract_text() or "" for t in pdf.pages[len(full_text_parts) :]])
        return [], "\n".join(full_text_parts)

    def _parse_annual_metrics(self, pdf: pdfplumber.PDF) -> dict:
        records, full_text = self._find_key_table_records(pdf)
        revenue = self._find_metric_row(records, ("营业收入",), ("营业收入构成",))
        net_profit = self._find_metric_row(records, ("归属于上市公司股东的净利润",), ("扣除非经常性损益",))
        cash_flow = self._find_metric_row(records, ("经营活动产生的现金流量净额",))
        total_assets = self._find_metric_row(records, ("总资产",), ("占总资产比",))
        total_equity = self._find_metric_row(records, ("归属于上市公司股东的净资产", "股东的净资产"), ("净资产收益率",))

        revenue_wan = self._value_to_wan(revenue["row"][3], revenue["label"]) if revenue else None
        net_profit_wan = self._value_to_wan(net_profit["row"][3], net_profit["label"]) if net_profit else None
        cash_flow_wan = self._value_to_wan(cash_flow["row"][3], cash_flow["label"]) if cash_flow else None
        total_assets_wan = self._value_to_wan(total_assets["row"][3], total_assets["label"]) if total_assets else None
        total_equity_wan = self._value_to_wan(total_equity["row"][3], total_equity["label"]) if total_equity else None

        extracted_debt_wan = self._extract_debt_wan(full_text)
        debt_wan = self._resolve_debt_wan(extracted_debt_wan, total_assets_wan, total_equity_wan)
        balance_metrics = self._extract_balance_sheet_metrics(full_text)
        bridge_metrics = self._extract_profit_bridge_metrics(full_text)
        ebitda_wan = bridge_metrics.get("ebitda_wan") or self._extract_ebitda_wan(full_text, extracted_debt_wan or debt_wan)
        return {
            "revenue_wan": revenue_wan,
            "net_profit_wan": net_profit_wan,
            "cash_flow_wan": cash_flow_wan,
            "total_assets_wan": total_assets_wan,
            "total_equity_wan": total_equity_wan,
            "debt_wan": debt_wan,
            "ebitda_wan": ebitda_wan,
            "cash_wan": balance_metrics.get("cash_wan"),
            "net_debt_wan": balance_metrics.get("net_debt_wan"),
            "capex_wan": bridge_metrics.get("capex_wan"),
            "interest_expense_wan": bridge_metrics.get("interest_expense_wan"),
            "profit_total_wan": bridge_metrics.get("profit_total_wan"),
            "tax_expense_wan": bridge_metrics.get("tax_expense_wan"),
            "depreciation_amortization_wan": bridge_metrics.get("depreciation_amortization_wan"),
        }

    def _parse_interim_metrics(self, pdf: pdfplumber.PDF) -> dict:
        records, full_text = self._find_key_table_records(pdf)
        revenue = self._find_metric_row(records, ("营业收入",))
        net_profit = self._find_metric_row(records, ("归属于上市公司股东的净利润",))
        cash_flow = self._find_metric_row(records, ("经营活动产生的现金流量净额",))
        total_assets = self._find_metric_row(records, ("总资产",), ("占总资产比",))
        total_equity = self._find_metric_row(records, ("归属于上市公司股东的所有者权益", "所有者权益", "股东权益"), ("净资产收益率",))
        total_assets_wan = round((self._numeric_value(total_assets["row"][3]) or 0) / 10000, 2) if total_assets else None
        total_equity_wan = round((self._numeric_value(total_equity["row"][3]) or 0) / 10000, 2) if total_equity else None
        debt_wan = self._resolve_debt_wan(self._extract_debt_wan(full_text), total_assets_wan, total_equity_wan)
        balance_metrics = self._extract_balance_sheet_metrics(full_text)
        bridge_metrics = self._extract_profit_bridge_metrics(full_text)
        return {
            "revenue_wan": round((self._numeric_value(revenue["row"][13]) or 0) / 10000, 2) if revenue else None,
            "net_profit_wan": round((self._numeric_value(net_profit["row"][13]) or 0) / 10000, 2) if net_profit else None,
            "cash_flow_wan": round((self._numeric_value(cash_flow["row"][13]) or 0) / 10000, 2) if cash_flow else None,
            "total_assets_wan": total_assets_wan,
            "total_equity_wan": total_equity_wan,
            "debt_wan": debt_wan,
            "ebitda_wan": bridge_metrics.get("ebitda_wan"),
            "cash_wan": balance_metrics.get("cash_wan"),
            "net_debt_wan": balance_metrics.get("net_debt_wan"),
            "capex_wan": bridge_metrics.get("capex_wan"),
            "interest_expense_wan": bridge_metrics.get("interest_expense_wan"),
            "profit_total_wan": bridge_metrics.get("profit_total_wan"),
            "tax_expense_wan": bridge_metrics.get("tax_expense_wan"),
            "depreciation_amortization_wan": bridge_metrics.get("depreciation_amortization_wan"),
        }

    def _parse_pdf_metrics(self, report_type: str, pdf_bytes: bytes) -> dict:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if report_type == "A":
                return self._parse_annual_metrics(pdf)
            return self._parse_interim_metrics(pdf)

    def _extract_markdown_text(self, pdf_bytes: bytes, title: str, publish_date: str, pdf_url: str) -> str:
        lines = [f"# {title}", "", f"- 公告日期: {publish_date}", f"- PDF链接: {pdf_url}", ""]
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                lines.append(f"## 第{idx}页")
                lines.append("")
                lines.append(text if text else "[该页未提取到文本]")
                lines.append("")
        return "\n".join(lines)

    def _report_candidates(self) -> list[dict]:
        now_year = datetime.now().year
        start_date = f"{now_year - 3}0101"
        end_date = f"{now_year}1231"
        rows: list[dict] = []
        for category, report_type in REPORT_TYPE_MAP.items():
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    data_frame = ak.stock_zh_a_disclosure_report_cninfo(
                        symbol=self.stock_code,
                        market="沪深京",
                        category=category,
                        start_date=start_date,
                        end_date=end_date,
                    )
            except Exception:
                continue

            for item in data_frame.to_dict("records"):
                title = str(item.get("公告标题", "")).strip()
                detail_url = str(item.get("公告链接", "")).strip()
                publish_date = str(item.get("公告时间", "")).strip()
                if not title or not detail_url or not publish_date:
                    continue
                if not self._clean_title(title):
                    continue
                year_match = re.search(r"(20\d{2})年", title)
                year = int(year_match.group(1)) if year_match else int(publish_date[:4])
                rows.append(
                    {
                        "report_type": report_type,
                        "report_key": f"{year}{report_type}",
                        "year": year,
                        "title": title,
                        "publish_date": publish_date,
                        "detail_url": detail_url,
                        "announcement_id": self._extract_announcement_id(detail_url),
                    }
                )

        rows.sort(key=self._report_sort_key, reverse=True)
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in rows:
            if item["report_key"] in seen:
                continue
            seen.add(item["report_key"])
            deduped.append(item)
        return deduped

    def _selected_reports(self) -> list[dict]:
        candidates = self._report_candidates()
        if not candidates:
            return []

        candidate_map = {item["report_key"]: item for item in candidates}
        selected: list[dict] = []
        seen: set[str] = set()

        latest = candidates[0]
        selected.append(latest)
        seen.add(latest["report_key"])

        prior_same_key = self._same_period_previous_key(latest)
        if prior_same_key and prior_same_key in candidate_map:
            selected.append(candidate_map[prior_same_key])
            seen.add(prior_same_key)

        latest_annual_key = self._annual_key(latest["year"] if latest["report_type"] == "A" else latest["year"] - 1)
        if latest_annual_key in candidate_map and latest_annual_key not in seen:
            selected.append(candidate_map[latest_annual_key])
            seen.add(latest_annual_key)

        annuals = [item for item in candidates if item["report_type"] == "A"]
        for item in annuals:
            if item["report_key"] in seen:
                continue
            selected.append(item)
            seen.add(item["report_key"])
            if len(selected) >= 4:
                return selected

        for item in candidates:
            if item["report_key"] in seen:
                continue
            selected.append(item)
            seen.add(item["report_key"])
            if len(selected) >= 4:
                break
        return selected

    def download_and_parse_reports(self, company_dir: Path) -> tuple[list[ReportDocument], list[str]]:
        warnings: list[str] = []
        documents: list[ReportDocument] = []
        pdf_dir = company_dir / "reports_pdf"
        md_dir = company_dir / "reports_md"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        md_dir.mkdir(parents=True, exist_ok=True)

        for item in self._selected_reports():
            announcement_id = item["announcement_id"]
            if not announcement_id:
                warnings.append(f"未能解析公告ID: {item['title']}")
                continue
            try:
                pdf_url = self._resolve_pdf_url(announcement_id, item["publish_date"])
                if not pdf_url:
                    warnings.append(f"未能获取PDF链接: {item['title']}")
                    continue
                req = request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with request.urlopen(req, timeout=60) as resp:
                    pdf_bytes = resp.read()
            except Exception as exc:
                warnings.append(f"下载财报失败: {item['title']} | {exc}")
                continue

            safe_title = re.sub(r"[\\/:*?\"<>|]", "_", item["title"])
            pdf_path = pdf_dir / f"{item['report_key']}_{safe_title}.pdf"
            md_path = md_dir / f"{item['report_key']}_{safe_title}.md"
            pdf_path.write_bytes(pdf_bytes)

            try:
                markdown = self._extract_markdown_text(pdf_bytes, item["title"], item["publish_date"], pdf_url)
                md_path.write_text(markdown, encoding="utf-8")
                metrics = self._parse_pdf_metrics(item["report_type"], pdf_bytes)
            except Exception as exc:
                warnings.append(f"解析财报失败: {item['title']} | {exc}")
                continue

            documents.append(
                ReportDocument(
                    report_key=item["report_key"],
                    report_type=item["report_type"],
                    year=item["year"],
                    title=item["title"],
                    publish_date=item["publish_date"],
                    announcement_id=announcement_id,
                    detail_url=item["detail_url"],
                    pdf_url=pdf_url,
                    pdf_path=pdf_path,
                    markdown_path=md_path,
                    metrics=metrics,
                )
            )

        documents.sort(key=lambda item: item.publish_date, reverse=True)
        return documents, warnings