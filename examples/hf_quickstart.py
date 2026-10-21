#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/hf_quickstart.py — 把 Hugging Face 数据集与情绪引擎接进你自己的项目

三个最小可运行示例（爬虫快讯 → 情绪画像 / HF 数据集 → 基线评估 / Hub 检索）：

    # 1) 没有 Key、没有网络：自动启动本地 Mock，先把链路跑通
    python examples/hf_quickstart.py

    # 2) 免下载读 HF 上的真实数据集（公开数据集不需要 Key）
    python examples/hf_quickstart.py --live

    # 3) 接 Hugging Face 模型（FinBERT / 中文 tone）
    HF_TOKEN=hf_xxx python examples/hf_quickstart.py --live

    # 4) 强制离线 Mock（CI / 演示）
    python examples/hf_quickstart.py --mock
    python examples/hf_quickstart.py --no-llm

代码里的 CRAWL_ITEMS 就是「你自己的爬虫 / 快讯模块」的替身：
换成 stock_news_scan / newsnow_sources 的真实输出即可，其余不用改。

为什么不用 `pip install datasets`：本仓库坚持零第三方依赖，
所有取数都走 datasets-server 的 HTTP 接口（单次最多 100 行切片）。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hf_hub as hh  # noqa: E402


# —— 假装这是你的爬虫 / 快讯模块输出（stock_news_scan / newsnow 均可）——
CRAWL_ITEMS = [
    {"platform": "金十数据", "title": "港股科技股午后集体走强，恒生科技指数涨超 2%"},
    {"platform": "东方财富", "title": "半导体设备厂商 9 月在手订单环比回暖"},
    {"platform": "公司公告", "title": "控股股东拟减持不超过 2% 股份，套现约 3 亿元"},
    {"platform": "雪球", "title": "新能源车企 9 月交付量普遍超预期，价格战趋缓"},
    {"platform": "微博", "title": "市场关注下周美联储议息与内地三季度 GDP"},
    {"platform": "路透", "title": "Quarterly results miss estimates; company cuts full-year guidance"},
]

# —— 常用基线数据集（含中文子集，可直接当情绪引擎的黄金标签）——
BENCHMARK_DATASET = "Kenpache/multilingual-financial-sentiment"
NEWS_DATASET = ("NickyNicky/"
                "finance-financialmodelingprep-stock-news-sentiments-rss-feed")


def use_mock(force: bool = False):
    """返回 (mock_handle_or_None)。--mock 或未要求联网时启动本地 Mock。"""
    if force or not use_mock._live:
        server = hh.start_mock_server()
        os.environ["HF_HUB_BASE"] = server.root
        os.environ["HF_DATASETS_SERVER"] = f"{server.root}/datasets-server"
        os.environ["HF_TASK_API"] = f"{server.root}/task/models"
        return server
    return None


use_mock._live = False


def demo_news_sentiment() -> None:
    """场景一：爬虫快讯 → 情绪画像（有 Token 走 HF 模型，否则本地词典）。"""
    print("\n" + "=" * 64)
    print("① 爬虫快讯 → Hugging Face 情绪画像")
    print("=" * 64)
    result = hh.market_sentiment(CRAWL_ITEMS, engine="auto")
    print(hh.render_sentiment_md(result))
    print(f"\n引擎：{result['engine']}"
          f"{' · 模型 ' + result['model'] if result.get('model') else ''}")
    if result.get("note"):
        print(f"备注：{result['note']}")


def demo_dataset_rows() -> None:
    """场景二：免下载读一个 HF 数据集的切片。"""
    print("\n" + "=" * 64)
    print("② datasets-server 免下载取数（不用 pip install datasets）")
    print("=" * 64)
    try:
        size = hh.dataset_size(BENCHMARK_DATASET)
        rows = hh.dataset_rows(BENCHMARK_DATASET, length=3)
    except hh.HFError as exc:
        print(f"取数失败[{exc.kind}]：{exc}")
        return
    print(f"数据集：{BENCHMARK_DATASET}")
    print(f"总行数：{size.get('rows')} · features：{rows.get('features')}")
    for idx, rec in enumerate(rows.get("records") or [], 1):
        preview = " | ".join(f"{k}={str(v)[:60]}" for k, v in list(rec.items())[:3])
        print(f"  [{idx}] {preview}")


def demo_benchmark() -> None:
    """场景三：用 HF 公开标注数据集给情绪引擎打基线。"""
    print("\n" + "=" * 64)
    print("③ 用 HF 数据集当黄金标签：情绪引擎基线")
    print("=" * 64)
    try:
        report = hh.benchmark(BENCHMARK_DATASET, length=60,
                              group_field="language", engine="auto")
    except hh.HFError as exc:
        print(f"基线评估失败[{exc.kind}]：{exc}")
        return
    print(hh.render_benchmark_md(report))
    print("\n读法：准确率/macro-F1 掉下来，通常意味着词典该补词、"
          "或模型口径和数据集不一致（语气 vs 价格影响）。")


def demo_search() -> None:
    """场景四：在 Hub 上找新东西。"""
    print("\n" + "=" * 64)
    print("④ Hub 检索（找新模型 / 新数据集）")
    print("=" * 64)
    for kind, query in (("dataset", "financial sentiment"),
                        ("model", "time series forecasting")):
        try:
            items = hh.search(kind, query, limit=3)
        except hh.HFError as exc:
            print(f"{kind} 检索失败[{exc.kind}]：{exc}")
            continue
        print(f"\n[{kind}] {query}")
        for item in items:
            print(f"  - {item['id']}  ⬇{item.get('downloads', 0):,} "
                  f"♥{item.get('likes', 0)}  {item.get('pipeline_tag') or ''}")


def demo_llm_engine() -> None:
    """场景五：--engine llm —— 用大模型端点（DeepSeek 等）做零样本情绪判别。

    没有真 Key 也能跑：hf_hub 自带一个 OpenAI 兼容的「分类器 Mock」，
    它会按编号回标签，方便离线验证「提示词 → 调用 → 解析 → 聚合」全链路。
    有真 Key 时（INFERENCE_* / DEEPSEEK_API_KEY）直接用真端点。
    """
    print("\n" + "=" * 64)
    print("⑤ 大模型端点引擎（--engine llm，走 inference_api.py）")
    print("=" * 64)
    server = None
    real = hh.llm_available()
    if not real:
        server = hh.start_llm_mock_server()
        os.environ["INFERENCE_BASE_URL"] = f"{server.root}/v1"
        os.environ["INFERENCE_API_KEY"] = "mock-key-123456"
        os.environ["INFERENCE_MODEL"] = "mock-classifier"
        print("🧪 未检测到推理端点，已启动 OpenAI 兼容 Mock（配置 "
              "DEEPSEEK_API_KEY / INFERENCE_* 后这里会直连真端点）")
    else:
        info = hh.llm_describe()
        print(f"🌐 推理端点：{info.get('provider')} · {info.get('model')} · "
              f"{info.get('base_url')} · key {info.get('key_masked')}")
    try:
        result = hh.market_sentiment(CRAWL_ITEMS, engine="llm")
        print(hh.render_sentiment_md(result))
        print(f"\n引擎：{result['engine']} · 模型 {result.get('model')}")
        lex = hh.market_sentiment(CRAWL_ITEMS, engine="lexicon")
        print(f"对照 · 本地词典：{lex['aggregate']['label']}"
              f"（{lex['aggregate']['score']:+.2f}）")
    finally:
        if server:
            server.close()
            for key in ("INFERENCE_BASE_URL", "INFERENCE_API_KEY",
                        "INFERENCE_MODEL"):
                os.environ.pop(key, None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="直连 Hugging Face（公开数据集无需 Key；"
                             "想用模型则设置 HF_TOKEN）")
    parser.add_argument("--mock", action="store_true", help="强制使用本地 Mock")
    parser.add_argument("--skip-network", action="store_true",
                        help="跳过需要联网的场景（只跑情绪画像）")
    parser.add_argument("--no-llm", action="store_true",
                        help="跳过场景五（大模型端点引擎）")
    args = parser.parse_args()

    server = None
    if not args.live or args.mock:
        server = use_mock(force=args.mock)
        print(f"🧪 使用本地 Mock：{server.root}（改用 --live 直连 Hugging Face）")
    else:
        print(f"🌐 直连 Hugging Face  Token={'已配置' if hh.has_token() else '未配置（模型调用会自动降级为本地词典）'}")

    try:
        demo_news_sentiment()
        if not args.skip_network:
            demo_dataset_rows()
            demo_benchmark()
            demo_search()
        if not args.no_llm:
            demo_llm_engine()
    finally:
        if server:
            server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
