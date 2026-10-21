#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/inference_quickstart.py — 把推理 API 接进你自己的项目

三个最小可运行示例（爬虫 → 摘要 / 量化 → 解读 / 流式对话）：

    # 1) 没有 Key：自动启动本地 Mock，先把链路跑通
    python examples/inference_quickstart.py

    # 2) 接真实模型：只改三个环境变量
    INFERENCE_BASE_URL=https://api.siliconflow.cn/v1 \
    INFERENCE_API_KEY=sk-xxx \
    INFERENCE_MODEL=Qwen/Qwen2.5-7B-Instruct \
    python examples/inference_quickstart.py

    # 3) 接本地 Ollama / vLLM（无需 Key）
    INFERENCE_PROVIDER=ollama python examples/inference_quickstart.py

    # 4) 接 Hugging Face（Router / 专用 Endpoint / Serverless 均可）
    HF_TOKEN=hf_xxx INFERENCE_PROVIDER=huggingface \
    INFERENCE_MODEL=Qwen/Qwen2.5-7B-Instruct \
    python examples/inference_quickstart.py

代码里的 demo_crawler / demo_quant 就是「你自己的爬虫 / 量化模块」的替身：
把 CRAWL_ITEMS / QUANT_FACTORS 换成你项目里的真实输出即可，其余不用改。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inference_api as ia  # noqa: E402


# —— 假装这是你的爬虫模块输出（stock_news_scan / newsnow / 自建 collector 均可）——
CRAWL_ITEMS = [
    {"platform": "金十数据", "title": "港股科技股午后集体走强，恒生科技指数涨超 2%"},
    {"platform": "东方财富", "title": "半导体设备厂商 9 月在手订单环比回暖"},
    {"platform": "雪球", "title": "新能源车企 9 月交付量普遍超预期，价格战趋缓"},
    {"platform": "微博", "title": "市场关注下周美联储议息与内地三季度 GDP"},
]

# —— 假装这是你的量化模块输出（动量 / 波动 / 估值 / 资金流）——
QUANT_FACTORS = {
    "momentum_48h": 0.62,
    "volatility_atr_pct": 18.0,
    "pe_percentile": 35,
    "fund_flow_5d": "+2.3 亿",
    "ma_signal": "MA5 上穿 MA20",
}


def build_client(force_mock: bool = False):
    """返回 (client, mock_server_or_None)。未配置时自动用本地 Mock。"""
    if force_mock or not ia.is_configured():
        server = ia.MockServerHandle().start()
        config = ia.resolve_config(
            base_url=server.base_url, api_key="mock-key",
            model="mock-inference-1", timeout=20)
        return ia.InferenceClient(config), server
    return ia.InferenceClient(ia.resolve_config()), None


def demo_crawler(client: ia.InferenceClient) -> str:
    """爬虫数据 → 结构化摘要：直接拼进 prompt 即可。"""
    lines = [f"- [{i['platform']}] {i['title']}" for i in CRAWL_ITEMS]
    prompt = (
        "你是港股研究员。请把下面 4 条爬虫快讯压缩成 3 条要点摘要，"
        "每条不超过 40 字，并标注可能影响的板块：\n\n" + "\n".join(lines)
    )
    result = client.chat_result(prompt, max_tokens=400, temperature=0.3)
    return result["content"]


def demo_quant(client: ia.InferenceClient) -> str:
    """量化因子 → 多空解读：把 dict 序列化后交给模型。"""
    prompt = (
        "你是量化策略助手。根据以下因子读数，给出 2 条多空判断与 1 条仓位建议，"
        "不超过 120 字：\n" + json.dumps(QUANT_FACTORS, ensure_ascii=False)
    )
    return client.chat(prompt, max_tokens=300, temperature=0.2)


def demo_stream(client: ia.InferenceClient) -> None:
    """流式对话：App 里可以边收边渲染。"""
    print("  ", end="", flush=True)
    for piece in client.chat_stream("用一句话给出今日港股开盘提示", max_tokens=120):
        print(piece, end="", flush=True)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="推理 API 接入最小示例")
    parser.add_argument("--mock", action="store_true", help="强制使用本地 Mock 端点")
    args = parser.parse_args()

    client, server = build_client(args.mock)
    try:
        print("=" * 72)
        print("🔌 推理 API 接入示例")
        print("   endpoint :", client.config.endpoint() or "（未配置）")
        print("   model    :", client.config.model)
        print("   key      :", ia.mask_key(client.config.api_key) or "keyless / 未配置")
        print("   mode     :", "本地 Mock（离线演示）" if server else "真实推理 API")
        print("=" * 72)

        health = client.health(deep=True)
        print(f"\n🩺 连通性：{'✅ ok' if health.get('ok') else '❌ ' + health.get('error', '')}"
              f"（{health.get('latency_ms', 0)}ms）")

        print("\n🕷  场景 1 · 爬虫快讯 → 要点摘要")
        print("-" * 72)
        print(demo_crawler(client))

        print("\n📈 场景 2 · 量化因子 → 多空解读")
        print("-" * 72)
        print(demo_quant(client))

        print("\n🌊 场景 3 · SSE 流式输出")
        print("-" * 72)
        demo_stream(client)

        print("\n" + "=" * 72)
        print("换掉三个环境变量即可切到真实模型；App 端只调 /api/inference/chat 或网关。")
        return 0
    except ia.InferenceError as exc:
        print(f"\n❌ 调用失败（{exc.kind}）：{exc}", file=sys.stderr)
        print("   检查 INFERENCE_BASE_URL / INFERENCE_API_KEY / INFERENCE_MODEL，"
              "或加 --mock 先跑通链路。", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
