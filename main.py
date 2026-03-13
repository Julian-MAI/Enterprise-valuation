from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from data_pipeline.collector import DataCollector
from valuation.engine import ValuationEngine
from valuation.output_generator import OutputGenerator


def load_config(config_dir: Path) -> tuple[dict, dict]:
    with (config_dir / "valuation_params.yaml").open("r", encoding="utf-8") as file:
        valuation_config = yaml.safe_load(file)
    with (config_dir / "industry_config.yaml").open("r", encoding="utf-8") as file:
        industry_config = yaml.safe_load(file)
    return valuation_config, industry_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股企业估值程序")
    parser.add_argument("--stock", default="", help="股票代码，例如 000977")
    parser.add_argument("--name", default="", help="公司名称，可选")
    return parser.parse_args()


def prompt_user_input(args: argparse.Namespace) -> tuple[str, str]:
    stock_code = args.stock.strip()
    company_name = args.name.strip()

    if not stock_code:
        print("请输入要估值的A股股票代码，例如 000001")
        stock_code = input("股票代码: ").strip()
    if not stock_code:
        return "", company_name

    if not company_name:
        print("请输入公司名称，可直接回车跳过")
        company_name = input("公司名称: ").strip()

    return stock_code, company_name


def main() -> int:
    args = parse_args()
    stock_code, company_name = prompt_user_input(args)
    if not stock_code:
        print("错误：股票代码不能为空")
        return 1

    base_dir = Path(__file__).parent
    config_dir = base_dir / "config"
    company_root = base_dir / "data"

    print("=" * 56)
    print("企业估值程序开始运行")
    print(f"股票代码: {stock_code}")
    print("=" * 56)

    valuation_config, industry_config = load_config(config_dir)
    collector = DataCollector(str(config_dir))

    print("[1/4] 抓取公司原始数据...")
    data_result = collector.save_company_data(stock_code, company_root)
    company_info = data_result["company_info"]
    if company_name:
        company_info.company_name = company_name
    print(f"  公司: {company_info.company_name}")
    print(f"  行业: {company_info.industry}")
    print(f"  公司目录: {data_result['company_dir']}")

    print("[2/4] 标准化财务数据...")
    financial_data = [asdict(item) for item in data_result["financial_data"]]
    valuation_params = asdict(data_result["valuation_params"])
    valuation_params.setdefault("growth_rate", 6.0)
    print(f"  财务年份数: {len(financial_data)}")

    print("[3/4] 执行估值模型...")
    engine = ValuationEngine(valuation_config, industry_config)
    valuation_result = engine.run_full_valuation(
        company_info=asdict(company_info),
        financial_data=financial_data,
        valuation_params=valuation_params,
        base_warnings=data_result.get("warnings", []),
    )
    print(f"  推荐方法: {valuation_result.recommended_method}")

    print("[4/4] 输出结果文件...")
    output_dir = data_result["company_dir"]
    output = OutputGenerator(output_dir)
    output.save_valuation_csv(stock_code, valuation_result)
    output.save_params_csv(stock_code, valuation_params)
    output.save_formula_csv(stock_code, valuation_result)
    output.save_sensitivity_csv(stock_code, valuation_result)
    output.save_method_recommendation_txt(stock_code, company_info.company_name, valuation_result)

    print("=" * 56)
    print("估值完成")
    print("=" * 56)
    print(f"输出目录: {output_dir}")
    print("已生成文件:")
    print("- 财报公告.md")
    print("- reports_pdf/")
    print("- reports_md/")
    print("- 财报解析结果.csv")
    print("- 原始行情快照.json")
    print("- 标准化财务数据.csv")
    print("- 估值参数.csv")
    print(f"- {stock_code}_估值结果.csv")
    print(f"- {stock_code}_公式说明.csv")
    print(f"- {stock_code}_敏感性分析.csv")
    print(f"- {stock_code}_估值报告.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
