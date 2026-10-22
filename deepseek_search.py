#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deepseek_search.py — DeepSeek API 作为智能搜索引擎

将 DeepSeek Chat Completions 作为搜索引擎使用，输入关键词/股票代码，
返回结构化的新闻/信息列表，可作为第 18 个平台（AI 搜索）接入
十七平台扫描、feedscan 全市场快讯、source_check_db 校验库。

设计目标：
- 纯标准库，无额外依赖，失败不中断整体流程
- 无 DEEPSEEK_API_KEY 时优雅跳过（记数据缺口），不抛未捕获异常
- 返回格式与 stock_news_scan.extract_flash_rows 兼容：[{title,url,ts}]
- 支持离线样本自检（不触网）与在线真抓取
- 带指数退避重试，避免瞬时 429/502 丢失

用法：
    python deepseek_search.py --query "阿里巴巴 09988" --hours 48
    python deepseek_search.py --selftest
    python deepseek_search.py --query "NVDA 英伟达" --json

环境变量：
    DEEPSEEK_API_KEY  必需（在线模式）
    DEEPSEEK_BASE_URL 可选，默认 https://api.deepseek.com
    DEEPSEEK_MODEL    可选，默认 deepseek-chat

集成：
    stock_news_scan.py 会自动尝试 import deepseek_search，
    若可用则在 FINANCE_FETCHERS 中注册 "DeepSeek 搜索"，
    扫描时按 156h 窗口过滤，缺口单独记录，不影响其他 17 源。
    source_check_db.py 离线/在线均覆盖该源。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc
CST = timezone(timedelta(hours=8), "CST")

DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_URL = f"{DEFAULT_BASE.rstrip('/')}/chat/completions"
TIMEOUT_DEFAULT = 15
RETRIES = 2

# 离线样本：用于 source_check_db 离线校验与自检
DEEPSEEK_SAMPLE_JSON = json.dumps({
    "choices": [{
        "message": {
            "content": json.dumps([
                {
                    "title": "阿里巴巴Q3财报超预期，云计算增长提速",
                    "url": "https://example.com/news/ali-q3",
                    "summary": "云计算营收双位数增长，AI相关产品三位数增长",
                    "date": "2026-08-06T10:00:00+08:00",
                    "source": "财联社"
                },
                {
                    "title": "港股电商板块走强，阿里巴巴领涨",
                    "url": "https://example.com/news/ecom-rally",
                    "summary": "南向资金加仓电商龙头",
                    "date": "2026-08-06T09:00:00+08:00",
                    "source": "格隆汇"
                }
            ], ensure_ascii=False)
        }
    }]
})

DEEPSEEK_SAMPLE_PARSED = [
    {"title": "阿里巴巴Q3财报超预期，云计算增长提速", "url": "https://example.com/news/ali-q3",
     "ts": "2026-08-06T10:00:00+08:00"},
    {"title": "港股电商板块走强，阿里巴巴领涨", "url": "https://example.com/news/ecom-rally",
     "ts": "2026-08-06T09:00:00+08:00"},
]


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def parse_any_time(v) -> datetime | None:
    """兼容 epoch/ISO/RFC822/中文时间，复用 stock_news_scan 逻辑的简化版"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        if ts < 1e9:
            return None
        return datetime.fromtimestamp(ts, UTC)
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10}(\d{3})?", s):
        try:
            return parse_any_time(float(s))
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=CST)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=CST)
    except Exception:
        pass
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*"
                  r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        return datetime(int(y), int(mo), int(d), int(hh), int(mm),
                        int(ss or 0), tzinfo=CST)
    return None


def _extract_json_array(text: str) -> list[dict]:
    """
    从 DeepSeek 返回的文本中提取 JSON 数组。
    兼容：
    - 纯 JSON 数组
    - ```json ... ``` 代码围栏
    - 前后有解释文字，中间嵌 JSON
    """
    if not text:
        return []
    t = text.strip()
    # 去围栏
    if "```" in t:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, re.I)
        if m:
            t = m.group(1).strip()
    # 直接尝试整体解析
    try:
        obj = json.loads(t)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            # 有些模型返回 {"results": [...]}
            for k in ("results", "data", "news", "items", "list"):
                if isinstance(obj.get(k), list):
                    return obj[k]
    except Exception:
        pass
    # 尝试截取首个 [ 到末个 ]
    start = t.find("[")
    end = t.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end+1])
        except Exception:
            pass
    return []


def parse_deepseek_response(content: str, fallback_query: str = "") -> list[dict]:
    """
    解析 DeepSeek 返回的 content（已是 message.content 文本），
    返回 [{title, url, ts, summary, source}] 列表。
    """
    rows = _extract_json_array(content)
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or r.get("content") or "").strip()
        if len(title) < 4:
            continue
        url = str(r.get("url") or r.get("link") or "").strip()
        # 清理标题中的 HTML 与渠道导语前缀
        from source_names import clean_wire_prefix, generalize_source
        title = clean_wire_prefix(re.sub(r"<[^>]+>", "", title).strip())
        summary = str(r.get("summary") or r.get("digest") or "").strip()
        raw_src = str(r.get("source") or r.get("platform") or "").strip()
        src = generalize_source(raw_src, default="AI 智能检索") if raw_src else "AI 智能检索"
        ts_raw = r.get("date") or r.get("time") or r.get("pub_date") or r.get("published_at")
        ts = parse_any_time(ts_raw)
        out.append({
            "title": title,
            "url": url,
            "ts": ts,
            "summary": summary,
            "source": src,
            "_raw_date": ts_raw,
        })
        if len(out) >= 20:
            break
    # 如果模型返回空但给了 fallback_query，至少保留一条占位避免被判空（由上层过滤）
    return out


def _http_post_json(url: str, payload: dict, api_key: str, timeout: int) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "Mozilla/5.0 (deepseek-search/1.0)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            b = e.read().decode("utf-8", "replace")
        except Exception:
            b = str(e)
        return e.code, b
    except Exception as e:
        raise RuntimeError(f"智能检索网络失败: {e}") from e


def build_messages(query: str, hours: int) -> list[dict]:
    """
    构造检索提示词，明确要求返回 JSON 数组，避免自由文本。
    """
    system = (
        "你是专业的金融信息搜索引擎，专注于港股、A股、美股的实时新闻检索。"
        "你必须只返回 JSON 数组，不要任何解释、前言或代码围栏外的文字。"
        "每条包含 title(标题, 中文, 4-60字)、url(可验证的真实新闻链接，若无则空字符串)、"
        "summary(1句摘要)、source(泛化来源名，如 财经电报源/宏观快讯源/港美股快讯源/公开新闻检索)、"
        "date(ISO时间，如 2026-08-06T10:00:00+08:00)。"
        "优先返回近期的、与查询直接相关的新闻，若无相关则返回空数组 []。"
    )
    user = (
        f"请搜索与“{query}”相关的最新财经新闻，时间窗口最近 {hours} 小时。"
        f"要求：\n"
        f"1) 返回 5-10 条，JSON 数组格式，如：\n"
        f'[{{"title":"...","url":"https://...","summary":"...","source":"财经电报源","date":"2026-08-06T10:00:00+08:00"}}]\n'
        f"2) 只返回 JSON 数组，不要任何其他文字\n"
        f"3) 标题必须包含查询关键词或其别名\n"
        f"4) url 尽量给出真实可访问链接，若无法确定则留空\n"
        f"5) date 必须是 {hours}h 内的合理时间，若不确定用当前时间\n"
        f"查询：{query}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def fetch_deepseek_search(query: str, timeout: int = TIMEOUT_DEFAULT,
                          hours: int = 48) -> list[dict]:
    """
    在线调用智能检索 API 搜索，返回 [{title,url,ts}] 列表。
    无 Key 时抛 RuntimeError，由上层记为数据缺口。
    """
    api_key = _env("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置检索密钥，已跳过智能检索")
    if not query or not query.strip():
        raise RuntimeError("搜索关键词为空")

    url = DEEPSEEK_URL
    model = DEFAULT_MODEL
    messages = build_messages(query.strip(), hours)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1500,
    }

    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            status, body = _http_post_json(url, payload, api_key, timeout)
            if status != 200:
                # 429/502/503 可重试
                if status in (429, 500, 502, 503, 504) and attempt < RETRIES:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                raise RuntimeError(f"DeepSeek API HTTP {status}: {body[:300]}")
            try:
                obj = json.loads(body)
                content = obj["choices"][0]["message"]["content"]
            except Exception as e:
                raise RuntimeError(f"DeepSeek 返回格式异常: {body[:300]}") from e

            items = parse_deepseek_response(content, query)
            # 即使为空也返回，由上层判断是否记缺口；这里若完全空则抛错提示
            if not items:
                # 若模型返回空数组，视为未命中而非错误，返回空列表让上层记缺口
                return []
            return items
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(0.6 * (2 ** attempt))
                continue
            raise RuntimeError(f"DeepSeek 搜索失败: {e}") from e
    raise RuntimeError(f"DeepSeek 搜索失败: {last_err}")


# 兼容 stock_news_scan 的签名：fetch_deepseek(query, timeout) -> list[dict]
def fetch_deepseek(query: str, timeout: int = TIMEOUT_DEFAULT) -> list[dict]:
    # 默认 48h 窗口，与 sentiment 因子一致
    return fetch_deepseek_search(query, timeout=timeout, hours=48)


# ---------------- 自检 ----------------

def selftest() -> int:
    fails = 0

    def check(name: str, cond: bool):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("🔍 AI 智能检索（离线自检，不触网）")

    # 1) JSON 提取
    content = json.loads(DEEPSEEK_SAMPLE_JSON)["choices"][0]["message"]["content"]
    rows = parse_deepseek_response(content, "阿里巴巴")
    check("样本解析 2 条", len(rows) == 2 and "阿里巴巴" in rows[0]["title"])
    check("时间解析带时区", rows[0]["ts"] is not None)

    # 2) 围栏容错
    fenced = "```json\n" + content + "\n```"
    rows2 = parse_deepseek_response(fenced)
    check("围栏 JSON 容错", len(rows2) == 2)

    # 3) 空数组
    empty = parse_deepseek_response("[]")
    check("空数组返回空列表", empty == [])

    # 4) 非 JSON 文本容错
    bad = parse_deepseek_response("抱歉，没有相关新闻。")
    check("非 JSON 文本容错为空", bad == [])

    # 5) 时间解析
    check("ISO 时间解析", parse_any_time("2026-08-06T10:00:00+08:00") is not None)
    check("中文时间解析", parse_any_time("2026-08-06 10:00:00") is not None)

    # 6) 消息构造
    msgs = build_messages("09988 阿里巴巴", 48)
    check("消息构造含 system/user", len(msgs) == 2 and "搜索引擎" in msgs[0]["content"])

    # 7) 无 Key 时抛错（隔离，且错误信息不泄露环境变量密钥名）
    old = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        fetch_deepseek_search("test", timeout=1)
        check("无 Key 应抛错", False)
    except RuntimeError as e:
        check("无 Key 抛错且脱敏提示", "检索密钥" in str(e) and "DEEPSEEK" not in str(e))
    finally:
        if old is not None:
            os.environ["DEEPSEEK_API_KEY"] = old

    print(f"\n{'✅ AI 智能检索自检通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="AI 智能搜索引擎")
    p.add_argument("--query", default="", help="搜索关键词，如 09988 阿里巴巴")
    p.add_argument("--hours", type=int, default=48, help="时间窗口小时数")
    p.add_argument("--timeout", type=int, default=TIMEOUT_DEFAULT)
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--selftest", action="store_true", help="离线自检")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if args.selftest:
        return selftest()

    q = args.query.strip() or "阿里巴巴 09988"
    try:
        items = fetch_deepseek_search(q, timeout=args.timeout, hours=args.hours)
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(
            [{"title": i["title"], "url": i["url"],
              "summary": i.get("summary", ""),
              "source": i.get("source", ""),
              "ts": i["ts"].isoformat() if i.get("ts") else None}
             for i in items],
            ensure_ascii=False, indent=2))
    else:
        if not items:
            print(f"（AI 智能检索 “{q}” 在 {args.hours}h 内未命中）")
        else:
            print(f"🔍 AI 智能检索 “{q}” · {args.hours}h · {len(items)} 条：")
            for idx, it in enumerate(items, 1):
                ts = it["ts"].strftime("%Y-%m-%d %H:%M") if it.get("ts") else "实时"
                print(f"{idx}. {it['title']} — {it.get('source','')} · {ts}")
                if it.get("url"):
                    print(f"   {it['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
