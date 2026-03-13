"""Fetch latest financial-report announcements for an A-share company."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from urllib import parse, request


CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_FILE_PREFIX = "http://static.cninfo.com.cn/"


@dataclass
class Announcement:
    title: str
    publish_time: int
    file_url: str
    source: str

    @property
    def publish_date(self) -> str:
        return datetime.fromtimestamp(self.publish_time / 1000).strftime("%Y-%m-%d")


def build_payload(stock_code: str, market: str, page_num: int, page_size: int) -> bytes:
    stock_field = f"{stock_code},{market}"
    payload = {
        "pageNum": str(page_num),
        "pageSize": str(page_size),
        "column": "szse" if market == "sz" else "sse",
        "tabName": "fulltext",
        "plate": market,
        "stock": stock_field,
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    return parse.urlencode(payload).encode("utf-8")


def request_announcements(stock_code: str, market: str, page_size: int = 30) -> list[Announcement]:
    req = request.Request(
        url=CNINFO_QUERY_URL,
        data=build_payload(stock_code, market, page_num=1, page_size=page_size),
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")

    data = json.loads(raw)
    items = data.get("announcements") or []
    if not isinstance(items, list):
        items = []
    results: list[Announcement] = []

    for item in items:
        title = item.get("announcementTitle", "").strip()
        publish_time = int(item.get("announcementTime", 0) or 0)
        adjunct_url = item.get("adjunctUrl", "").lstrip("/")
        if not title or not publish_time or not adjunct_url:
            continue
        results.append(
            Announcement(
                title=title,
                publish_time=publish_time,
                file_url=parse.urljoin(CNINFO_FILE_PREFIX, adjunct_url),
                source="cninfo",
            )
        )
    return results


def request_announcements_fallback(stock_code: str, page_size: int = 100) -> list[Announcement]:
    params = {
        "sr": "-1",
        "page_size": str(page_size),
        "page_index": "1",
        "ann_type": "A",
        "client_source": "web",
        "stock_list": stock_code,
    }
    url = f"https://np-anotice-stock.eastmoney.com/api/security/ann?{parse.urlencode(params)}"
    req = request.Request(
        url=url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        method="GET",
    )

    with request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")

    data = json.loads(raw)
    items = (((data.get("data") or {}).get("list")) or [])
    if not isinstance(items, list):
        items = []

    results: list[Announcement] = []
    for item in items:
        title = str(item.get("title", "")).strip()
        date_str = str(item.get("notice_date", "")).strip()
        art_code = str(item.get("art_code", "")).strip()
        if not title or not date_str or not art_code:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        publish_time = int(dt.timestamp() * 1000)
        file_url = f"https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
        results.append(
            Announcement(
                title=title,
                publish_time=publish_time,
                file_url=file_url,
                source="eastmoney-fallback",
            )
        )
    return results


def filter_financial_reports(items: list[Announcement]) -> list[Announcement]:
    include_keywords = (
        "年度报告",
        "半年度报告",
        "三季度报告",
        "一季度报告",
    )
    exclude_keywords = (
        "摘要",
        "英文版",
        "公告",
        "董事会",
        "监事会",
        "制度",
    )

    results: list[Announcement] = []
    for item in items:
        if not any(k in item.title for k in include_keywords):
            continue
        if any(k in item.title for k in exclude_keywords):
            continue
        results.append(item)
    return results


def save_markdown_report(stock_code: str, company_name: str, items: list[Announcement], output_path: pathlib.Path) -> None:
    lines = [
        f"# {company_name}({stock_code}) 最新财报公告抓取结果",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 公告数量: {len(items)}",
        f"- 数据来源: {items[0].source if items else 'N/A'}",
        "",
        "| 日期 | 标题 | PDF链接 |",
        "| --- | --- | --- |",
    ]

    for item in items:
        lines.append(f"| {item.publish_date} | {item.title} | [下载]({item.file_url}) |")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取A股公司最新财报公告")
    parser.add_argument("--stock", default="000977", help="股票代码，例如 000977")
    parser.add_argument("--name", default="浪潮信息", help="公司名称")
    parser.add_argument("--market", default="sz", choices=["sz", "sh"], help="交易所: sz=深交所, sh=上交所")
    parser.add_argument("--limit", type=int, default=10, help="输出条目数")
    parser.add_argument("--output", default="latest_reports.md", help="输出Markdown文件名")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        all_items = request_announcements(args.stock, args.market, page_size=50)
        if not all_items:
            all_items = request_announcements_fallback(args.stock, page_size=100)
    except Exception as exc:  # noqa: BLE001 - surface real error to user.
        print(f"抓取失败: {exc}")
        return 1

    report_items = filter_financial_reports(all_items)
    report_items.sort(key=lambda x: x.publish_time, reverse=True)
    report_items = report_items[: max(args.limit, 1)]

    if not report_items:
        print("未找到财报类公告，可能需要调整关键词或检查股票代码/交易所。")
        return 2

    output_path = pathlib.Path(args.output)
    save_markdown_report(args.stock, args.name, report_items, output_path)

    print(f"抓取成功，共 {len(report_items)} 条财报公告。")
    print(f"结果文件: {output_path.resolve()}")
    for item in report_items[:5]:
        print(f"- {item.publish_date} | {item.title}")

    time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
