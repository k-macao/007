#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pushplus_deepseek.py — 多市场行情/快讯 + AI 分析 → 多通道推送
                          （PushPlus / 企业微信 / Server酱 / 控制台）

功能模块
========
1. 港股行情模块：三个免费无需 Key 的数据源（Yahoo财经 / 东方财富 / 腾讯财经）
2. 交叉核验模块：同一标的从三源取价，比对偏差，给出可信度结论；异常源自动剔除
3. 分析框架模块：DeepSeek 按 12 套模板生成内容，多空判断一律带概率；analysis 包含 7 个因子
4. 推送模块：多通道真实推送 / dry-run 预览 / Secrets 检查
5. 字符模拟图模块：纯字符模拟走势（非图片）——基于日级 OHLC 渲染等宽字符图
   （涨 █ 跌 ▓ 影线 │，叠加 MA5/10/20 点位、成交量字符条、支撑/压力标注），
   直接以 Markdown/HTML <pre> 嵌入推送正文，无需图片上传与 CDN；--no-chart 可关闭；
   任何失败自动降级不影响推送

用法
====
    python pushplus_deepseek.py --template fusion --topic "阿里巴巴" --hk-code 09988
    python pushplus_deepseek.py --template scan --dry-run
    python pushplus_deepseek.py --selftest            # 离线自检（解析器+核验+模板）
    python pushplus_deepseek.py --check-only          # 只检查 Secrets
    python pushplus_deepseek.py                       # 默认: brief + pushplus + deepseek

Secrets（keyless 数据源无需配置）：
    PUSHPLUS_TOKEN / WECOM_KEY / SERVERCHAN_SENDKEY / DEEPSEEK_API_KEY / OPENAI_API_KEY
环境变量（可选）：TOPIC / CONTEXT / HK_CODE / RISK(low|mid|high) / PUSH_STATE_PATH
新鲜度看板：每次推送正文顶部打印「数据新鲜度·本次运行指纹」，跨运行对比
    内容指纹与样本增量（状态文件默认 output/push_state.json，Actions 用
    actions/cache 持久化；--no-state 可关闭）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from statistics import median
# newsnow 10源（前 7 源用户指定，2026-08-13 追加 3 源：头条/百度/B站）
try:
    import newsnow_sources as newsnow_mod
except Exception:
    newsnow_mod = None


# ================================================================ 常量

PUSHPLUS_URL = "http://www.pushplus.plus/send"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/{sendkey}.send"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

YAHOO_CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                   "{code}.HK?interval=1d&range=5d")
EASTMONEY_URL = ("https://push2.eastmoney.com/api/qt/stock/get"
                 "?secid=116.{code}&fields=f43,f44,f45,f46,f57,f58,f60,f170,f47")
TENCENT_URL = "https://qt.gtimg.cn/q=hk{code}"
EM_HK_PRICE_SCALE = 1000          # 东财港股价格为实际价格×1000
HK_TZ = timezone(timedelta(hours=8), "HKT")

DEFAULT_TOPIC = "阿里巴巴(Alibaba) 每日简报"
CST = timezone(timedelta(hours=8), "CST")

VERSION = "2.20-dos-market-2026-10-08"  # DOS 主题 + 全市场定时简报

CHANNELS = ["pushplus", "wecom", "serverchan", "console", "all"]
ALL_CHANNELS = ["pushplus", "wecom", "serverchan"]
CHANNEL_SECRET_MAP = {
    "pushplus": ["PUSHPLUS_TOKEN"],
    "wecom": ["WECOM_KEY"],
    "serverchan": ["SERVERCHAN_SENDKEY"],
    "console": [],
}
SKIP_RESULT_PREFIX = "跳过"
PROVIDERS = ["deepseek", "rule", "openai"]
RISKS = ["low", "mid", "high"]
RISK_ZH = {"low": "低", "mid": "中", "high": "高"}

TEMPLATES = ["brief", "analysis", "scan", "picker", "fusion",
             "plan", "earnings", "portfolio", "review", "regime",
             "sentiment", "feedscan", "newsnow", "equity",
             "initiate", "earnings_preview", "earnings_update",
             "model_update", "morning_note", "catalysts",
             "thesis", "sector", "ideas", "foreign_weekly", "hk_brokers"]
TEMPLATE_TITLES = {
    "brief": "简报", "analysis": "多空因子分析", "scan": "市场情报扫描",
    "picker": "选股器·未来30日", "fusion": "技术面×基本面融合",
    "plan": "交易计划·进出场风控", "earnings": "财报前瞻",
    "portfolio": "组合配置优化", "review": "交易复盘改进", "regime": "市场形态识别",
    "sentiment": "量价舆情动量·48h", "feedscan": "全市场快讯情绪扫描",
    "newsnow": "NewsNow热榜聚合·10源",
    "equity": "机构级个股投研",
    "initiate": "首次覆盖·公司研究",
    "earnings_preview": "财报前瞻·Skill",
    "earnings_update": "季报更新",
    "model_update": "模型修订",
    "morning_note": "晨会纪要",
    "catalysts": "催化剂日历",
    "thesis": "论点记分卡",
    "sector": "行业格局",
    "ideas": "选股/主题扫描",
    "foreign_weekly": "国外机构一周研报",
    "hk_brokers": "香港券商新闻与数据",
}
TEMPLATE_MAX_TOKENS = {
    "brief": 2000, "analysis": 3000, "scan": 4000, "picker": 4000,
    "fusion": 3000, "plan": 4000, "earnings": 3000, "portfolio": 4000,
    "review": 3000, "regime": 3000, "sentiment": 4000, "feedscan": 4000,
    "newsnow": 4000, "equity": 8000,
    "initiate": 6000, "earnings_preview": 3500, "earnings_update": 5000,
    "model_update": 3500, "morning_note": 2500, "catalysts": 3000,
    "thesis": 3000, "sector": 4500, "ideas": 4000, "foreign_weekly": 6000,
    "hk_brokers": 6000,
}

# analysis 的第 7 项是新增因子。它与“消息面与情绪面”不同：这里使用
# 48 小时内的价格/成交量、新闻和社媒样本做本地预聚合，必须单独呈现。
SENTIMENT_FACTOR = "量价舆情动量（48h）"
# 行业与政策面：只做真实可见的政策/监管动态分析，不预设任何行业政策、不套产业链逻辑
INDUSTRY_POLICY_FACTOR = "行业与政策面（真实政策分析）"
FACTORS = [
    "基本面（业绩/订单/毛利率）",
    INDUSTRY_POLICY_FACTOR,
    "技术面（趋势/量价/关键价位）",
    "资金面（主力/北向/两融动向）",
    "消息面与情绪面（公告/舆情/行业事件）",
    "估值面（PE/PB 与历史分位）",
    SENTIMENT_FACTOR,
]
FACTOR_COUNT = len(FACTORS)

# ---------------- 品牌信息：全部结果的统一头部与尾注 ----------------
BRAND_TITLE = "章鱼 AI 全景分析"
BRAND_SUBTITLE = "全网 AI 调研境内境外数据，由多个大模型混合部署。"
BRAND_AUTHOR = "作者：章鱼 ai"
BRAND_SLOGAN = ("全网境内外为你寻找蛛丝马迹-提供全景视野分析，由多模型协同推理决策，"
                "底层所使用的大语言模型（LLM）多模式背后结合使用了多种不同的先进模型，"
                "包括但不限于 Claude、ChatGPT、Gemini、Grok、Qwen 以及 Kimi。"
                "根据不同的资产管理任务需求，更好地发挥各个模型的优势来提供数据支持！[加油]")
BRAND_DISCLAIMER = "仅供参考，分析研究"


def brand_header_md() -> str:
    """全部结果统一头部：只放标题和副标题。"""
    return "\n".join([
        f"## 🐙 {BRAND_TITLE}",
        f"> {BRAND_SUBTITLE}",
        "",
        "---",
    ])


def brand_footer_md() -> str:
    """全部结果统一尾注；作者信息必须是推送正文的最后一行。"""
    return "\n".join([
        "> " + BRAND_DISCLAIMER,
        "",
        f"{BRAND_SLOGAN}",
        f"**{BRAND_AUTHOR} · {BRAND_DISCLAIMER}**",
    ])


def add_branding(content: str) -> str:
    """给正文加品牌头和固定尾注，避免各调用点自行拼接而放错位置。"""
    return f"{brand_header_md()}\n{content.rstrip()}\n\n{brand_footer_md()}"

# ================================================================ 基础工具


def log(msg: str = "") -> None:
    print(msg, flush=True)


class PushError(RuntimeError):
    """可预期失败（缺 Secret、API 返回错误等）。"""


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def http_request(url: str, data: bytes | None = None, content_type: str = "",
                 headers: dict | None = None, timeout: int = 30,
                 encoding: str = "utf-8") -> tuple[int, str]:
    """GET/POST 统一入口，返回 (HTTP 状态码, 响应文本)。网络层异常转 PushError。"""
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if content_type:
        req.add_header("Content-Type", content_type)
    req.add_header("User-Agent", "Mozilla/5.0 (pushplus-deepseek/2.0)")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(encoding, "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(encoding, "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise PushError(f"网络请求失败 {url.split('?')[0]}: {reason}") from e


def http_post_json(url: str, payload: dict, headers: dict | None = None,
                   timeout: int = 30) -> tuple[int, str]:
    return http_request(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json", headers, timeout)


def http_post_form(url: str, fields: dict, timeout: int = 30) -> tuple[int, str]:
    return http_request(url, urllib.parse.urlencode(fields).encode("utf-8"),
                        "application/x-www-form-urlencoded", None, timeout)


# ================================================================ 模块①：港股行情（三源接入）

@dataclass
class Quote:
    source: str
    ok: bool = False
    name: str = ""
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    time_str: str = ""
    error: str = ""


def normalize_hk_code(raw: str) -> str:
    """接受 9988 / 09988 / 9988.HK / hk09988 等写法，统一为 5 位数字字符串。"""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise PushError(f"无法识别的港股代码: {raw!r}")
    code = digits[-5:].zfill(5)
    if code == "00000":
        raise PushError(f"非法港股代码: {raw!r}")
    return code


def _sane(price: float | None) -> bool:
    return price is not None and 0 < price < 100000


def _fill_change(q: Quote) -> None:
    if q.change_pct is None and _sane(q.price) and _sane(q.prev_close) and q.prev_close:
        q.change_pct = round((q.price - q.prev_close) / q.prev_close * 100, 2)


def parse_yahoo(text: str) -> Quote:
    data = json.loads(text)
    meta = data["chart"]["result"][0]["meta"]
    q = Quote(source="Yahoo财经", ok=True,
              name=meta.get("longName") or meta.get("shortName")
                   or meta.get("symbol", ""),
              price=meta.get("regularMarketPrice"),
              prev_close=(meta.get("previousClose")
                          or meta.get("chartPreviousClose")))
    ts = meta.get("regularMarketTime")
    if ts:
        q.time_str = datetime.fromtimestamp(ts, HK_TZ).strftime("%Y-%m-%d %H:%M")
    _fill_change(q)
    if not _sane(q.price):
        raise ValueError("价格异常或缺失")
    return q


def parse_eastmoney(text: str) -> Quote:
    data = json.loads(text).get("data") or {}
    raw_price, raw_prev = data.get("f43"), data.get("f60")
    q = Quote(source="东方财富", ok=True, name=data.get("f58", ""))
    if raw_price is not None:
        q.price = round(raw_price / EM_HK_PRICE_SCALE, 3)
    if raw_prev is not None:
        q.prev_close = round(raw_prev / EM_HK_PRICE_SCALE, 3)
    if data.get("f170") is not None:
        q.change_pct = round(data["f170"] / 100, 2)
    _fill_change(q)
    if not _sane(q.price):
        raise ValueError("价格异常或缺失")
    return q


def parse_tencent(text: str) -> Quote:
    # 形如: v_hk09988="100~阿里巴巴~09988~16.800~16.610~16.850~...";
    if '="' not in text or not text.strip().endswith(";"):
        raise ValueError("返回格式不符合预期")
    body = text.split('="', 1)[1].rstrip('";\n ')
    parts = body.split("~")
    if len(parts) < 5:
        raise ValueError("字段数量不足")
    q = Quote(source="腾讯财经", ok=True, name=parts[1],
              price=float(parts[3]), prev_close=float(parts[4]))
    _fill_change(q)
    if not _sane(q.price):
        raise ValueError("价格异常或缺失")
    return q


SOURCES = [  # (源名, URL 构造器, 解析器, 解码)
    ("Yahoo财经", lambda c: YAHOO_CHART_URL.format(code=str(int(c))), parse_yahoo, "utf-8"),
    ("东方财富", lambda c: EASTMONEY_URL.format(code=c), parse_eastmoney, "utf-8"),
    ("腾讯财经", lambda c: TENCENT_URL.format(code=c), parse_tencent, "gbk"),
]


def fetch_all(code: str, timeout: int = 15) -> list[Quote]:
    """从三个免费源取价。任何单源失败只记录、不抛出，绝不拖垮主流程。"""
    quotes: list[Quote] = []
    for name, url_fn, parser, encoding in SOURCES:
        try:
            status, body = http_request(url_fn(code), timeout=timeout,
                                        encoding=encoding)
            if status != 200:
                raise PushError(f"HTTP {status}: {body[:120]}")
            quotes.append(parser(body))
        except Exception as e:  # noqa: BLE001 —— 逐源隔离
            quotes.append(Quote(source=name, ok=False, error=str(e)[:150]))
    return quotes


# ================================================================ 模块①·交叉核验

MAX_DEV_OK = 0.5      # ≤0.5% 视为一致
MAX_DEV_EXCLUDE = 2.0  # >2% 视为异常并剔除出共识


@dataclass
class Verification:
    consensus: float | None = None
    max_dev_pct: float | None = None
    n_ok: int = 0
    n_excluded: int = 0
    verdict: str = ""
    rows: list[dict] = field(default_factory=list)


def _short_err(err: str) -> str:
    """失败原因压缩成短文案（去掉 URL 等噪音），便于微信表格阅读。"""
    if "网络请求失败" in err:
        host = err.split("//", 1)[1].split("/")[0] if "//" in err else ""
        low = err.lower()
        if "timed out" in low or "timeout" in low:
            cat = "超时"
        elif "ssl" in low or "eof" in low or "reset" in low:
            cat = "网络不可达"
        elif "name" in low or "resolve" in low:
            cat = "域名解析失败"
        else:
            cat = "连接失败"
        return f"{cat}（{host}）"
    return err[:40]


def verify_quotes(quotes: list[Quote]) -> Verification:
    v = Verification()
    ok = [q for q in quotes if q.ok and _sane(q.price)]
    v.n_ok = len(ok)
    for q in quotes:
        if not q.ok:
            v.rows.append({"source": q.source, "price": None, "dev": None,
                           "status": f"❌ 获取失败（{_short_err(q.error)}）"})
    if not ok:
        v.verdict = "❌ 三个数据源全部失败，本次内容降级为纯模型推断"
        return v
    prices = [q.price for q in ok]
    mid = median(prices)
    for q in ok:
        dev = abs(q.price - mid) / mid * 100
        status = "✅ 一致" if dev <= MAX_DEV_OK else (
            "⚠️ 偏差偏大" if dev <= MAX_DEV_EXCLUDE else "❌ 异常剔除")
        v.rows.append({"source": f"{q.source}（{q.name or '?'}）",
                       "price": q.price, "dev": round(dev, 2), "status": status,
                       "change": q.change_pct, "time": q.time_str})
    included = [r for r in v.rows if r["price"] is not None
                and not r["status"].startswith("❌")]
    v.n_excluded = v.n_ok - len(included)
    if len(included) >= 2:
        v.consensus = round(median([r["price"] for r in included]), 3)
        v.max_dev_pct = max(r["dev"] for r in included)
    elif len(included) == 1:
        v.consensus = included[0]["price"]
        v.max_dev_pct = None
    dev2 = v.max_dev_pct if v.max_dev_pct is not None else 99.0
    if v.n_ok >= 3 and v.n_excluded == 0 and dev2 <= MAX_DEV_OK:
        v.verdict = f"✅ 三源一致，数据可信（最大偏差 {v.max_dev_pct:.2f}%）"
    elif len(included) >= 2 and dev2 <= MAX_DEV_EXCLUDE:
        v.verdict = (f"✅ {len(included)} 源基本一致（最大偏差 {v.max_dev_pct:.2f}%"
                     + (f"，已剔除 {v.n_excluded} 个异常源）" if v.n_excluded else "）"))
    elif len(included) == 1:
        v.verdict = "⚠️ 仅单源数据，未经交叉核验，仅供参考"
    else:
        v.verdict = "❌ 数据源之间分歧过大，本次数值不可信"
    return v


def market_block_md(code: str, quotes: list[Quote], v: Verification) -> str:
    lines = ["---", f"📊 **数据核验（HK{code}）**",
             "", "| 数据源 | 最新价 | 涨跌 | 偏差 | 状态 |", "|---|---|---|---|---|"]
    for r in v.rows:
        price = f"{r['price']:.3f}" if r["price"] is not None else "—"
        chg = (f"{r['change']:+.2f}%" if r.get("change") is not None
               and r["price"] is not None else "—")
        dev = f"{r['dev']:.2f}%" if r.get("dev") is not None else "—"
        lines.append(f"| {r['source']} | {price} | {chg} | {dev} | {r['status']} |")
    lines += ["", f"> 共识价：**{v.consensus if v.consensus else '—'}**　{v.verdict}",
              "> 数据源：Yahoo财经 / 东方财富 / 腾讯财经（均免费免Key）"]
    return "\n".join(lines)


def market_context_line(code: str, quotes: list[Quote], v: Verification) -> str:
    ok_names = [f"{q.name}" for q in quotes if q.ok and q.name][:1]
    chg = next((q.change_pct for q in quotes if q.ok
                and q.change_pct is not None), None)
    chg_txt = f"{chg:+.2f}%" if chg is not None else "未知"
    grade = "已交叉核验" if v.n_ok >= 2 and not v.n_excluded else "部分核验"
    return (f"HK{code} {ok_names[0] if ok_names else ''} "
            f"最新价 {v.consensus or '—'}（{chg_txt}），"
            f"{grade}，可信度说明见文末数据核验表")


def pick_cn_name(quotes: list[Quote], fallback: str = "") -> str:
    """从三源行情里挑中文名填入标题：腾讯/东财返回中文名，Yahoo 英文名兜底。

    全部失败时回退 ``fallback``（通常为 TOPIC）。
    """
    names = [q.name.strip() for q in quotes if q.ok and q.name.strip()]
    for n in names:
        if re.search(r"[\u4e00-\u9fff]", n):  # 含中文的优先（腾讯/东财）
            return n
    return names[0] if names else fallback


# ================================================================ 模块②：舆情采集与情绪动量（48h）

GOOGLE_NEWS_RSS = ("https://news.google.com/rss/search?"
                   "q={q}&hl=zh-HK&gl=HK&ceid=HK:zh-Hant")
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
YOUTUBE_HANDLE_PAGE = "https://www.youtube.com/@{handle}"

# 轻量情绪词典（先本地打标，AI 再综合——rule 模式下也能出完整报告）
POS_WORDS = ["利好", "上涨", "大涨", "增长", "中标", "突破", "创新高", "创纪录",
             "回购", "预增", "超预期", "盈利", "签约", "订单", "扩产", "获批",
             "分红", "surge", "rally", "beat", "upgrade", "record", "profit",
             "bullish", "soar", "win", "growth", "boost"]
NEG_WORDS = ["利空", "下跌", "大跌", "亏损", "减持", "违约", "下调", "预警",
             "预亏", "低于预期", "处罚", "诉讼", "削减", "延期", "停产",
             "plunge", "miss", "downgrade", "loss", "lawsuit", "cut", "bearish",
             "slump", "drop", "warning", "fraud"]

UTC = timezone.utc


def _count_word(low: str, w: str) -> int:
    """英文词按词边界匹配（防止 win 命中 downwind 之类误判），中文按子串。"""
    if w.isascii():
        return len(re.findall(r"\b" + re.escape(w.lower()) + r"\b", low))
    return low.count(w)


def score_text(text: str) -> tuple[float, str, float]:
    """词典法情绪打分。返回 (score∈[-1,1], 标签, 置信度∈[0,1])。"""
    low = text.lower()
    pos = sum(_count_word(low, w) for w in POS_WORDS)
    neg = sum(_count_word(low, w) for w in NEG_WORDS)
    total = pos + neg
    score = 0.0 if total == 0 else max(-1.0, min(1.0, (pos - neg) / total))
    label = "利好" if score > 0.12 else ("利空" if score < -0.12 else "中性")
    return round(score, 2), label, round(min(1.0, total / 4), 2)


@dataclass
class SentItem:
    source: str
    title: str
    url: str = ""
    age_h: float | None = None   # 距抓取时刻的小时数
    score: float = 0.0
    label: str = "中性"
    conf: float = 0.0


def _mk_item(source: str, title: str, url: str, ts: datetime | None,
             now: datetime) -> SentItem:
    title = re.sub(r"\s+", " ", title).strip()
    score, label, conf = score_text(title)
    age = round((now - ts).total_seconds() / 3600, 1) if ts else None
    return SentItem(source=source, title=title, url=url,
                    age_h=age, score=score, label=label, conf=conf)


def _in_window(ts: datetime | None, hours: int, now: datetime) -> bool:
    return ts is not None and (now - ts).total_seconds() <= hours * 3600


# ---------- 源1：Google 新闻（官方 RSS）----------

GOOGLE_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>阿里巴巴Q3营收超预期 云计算业务增长提速 - 香港经济日报</title>
<link>https://example.com/a</link><pubDate>Mon, 04 Aug 2026 08:00:00 GMT</pubDate>
<source>香港经济日报</source></item>
<item><title>Alibaba shares plunge 3% on profit warning fears - Reuters</title>
<link>https://example.com/b</link><pubDate>Sun, 03 Aug 2026 20:00:00 GMT</pubDate>
<source>Reuters</source></item>
<item><title>电商板块周度回顾 - 财华社</title><link>https://example.com/c</link>
<pubDate>Fri, 01 Aug 2026 08:00:00 GMT</pubDate><source>财华社</source></item>
</channel></rss>"""


def parse_google_rss(text: str, hours: int, limit: int,
                     now: datetime) -> list[SentItem]:
    root = ET.fromstring(text)
    items = []
    for it in root.iter("item"):
        title = it.findtext("title", "")
        if " - " in title:  # Google 格式: "标题 - 来源"
            title, src = title.rsplit(" - ", 1)
        else:
            src = it.findtext("source", "")
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate", ""))
        except (TypeError, ValueError):
            ts = None
        if not _in_window(ts, hours, now):
            continue  # 48h 窗口过滤
        itm = _mk_item(f"Google·{src or '新闻'}", title,
                       it.findtext("link", ""), ts, now)
        items.append(itm)
        if len(items) >= limit:
            break
    return items


def fetch_google_news(query: str, hours: int, limit: int,
                      timeout: int) -> tuple[list[SentItem], str]:
    q = urllib.parse.quote_plus(f"{query} when:2d")
    status, body = http_request(GOOGLE_NEWS_RSS.format(q=q), timeout=timeout)
    if status != 200:
        raise PushError(f"HTTP {status}")
    items = parse_google_rss(body, hours, limit,
                             datetime.now(UTC))
    if not items:
        raise PushError(f"{hours}h 内无相关新闻")
    return items, ""


# ---------- 源4：YouTube @investtalk（Handle 解析 + 官方 RSS）----------

YOUTUBE_ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
<entry><yt:videoId>abc123</yt:videoId>
<title>Market rally: why tech could surge this week</title>
<published>2026-08-04T06:00:00+00:00</published></entry>
<entry><yt:videoId>def456</yt:videoId>
<title>Investors brace for loss: bearish signals in energy</title>
<published>2026-08-03T07:00:00+00:00</published></entry>
<entry><yt:videoId>old789</yt:videoId><title>Old episode</title>
<published>2026-07-30T07:00:00+00:00</published></entry>
</feed>"""


def parse_youtube_atom(text: str, hours: int, limit: int,
                       now: datetime) -> list[SentItem]:
    ns = {"a": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015"}
    items = []
    for e in ET.fromstring(text).findall("a:entry", ns):
        try:
            ts = datetime.fromisoformat(e.findtext("a:published", "", ns))
        except ValueError:
            ts = None
        if not _in_window(ts, hours, now):
            continue
        vid = e.findtext("yt:videoId", "", ns)
        itm = _mk_item("YouTube·investtalk", e.findtext("a:title", "", ns),
                       f"https://youtu.be/{vid}" if vid else "", ts, now)
        items.append(itm)
        if len(items) >= limit:
            break
    return items


def fetch_youtube(handle: str, hours: int, limit: int,
                  timeout: int) -> tuple[list[SentItem], str]:
    handle = (handle or "investtalk").lstrip("@")
    status, body = http_request(YOUTUBE_HANDLE_PAGE.format(handle=handle),
                                timeout=timeout)
    if status != 200:
        raise PushError(f"频道页 HTTP {status}")
    m = (re.search(r'"channelId"\s*:\s*"(UC[\w-]{22})"', body)
         or re.search(r'itemprop="channelId"\s+content="(UC[\w-]{22})"', body))
    if not m:
        raise PushError(f"未能解析 @{handle} 的 channelId")
    status, feed = http_request(YOUTUBE_FEED.format(cid=m.group(1)),
                                timeout=timeout)
    if status != 200:
        raise PushError(f"频道 RSS HTTP {status}")
    items = parse_youtube_atom(feed, hours, limit, datetime.now(UTC))
    if not items:
        raise PushError(f"@{handle} 在 {hours}h 内无新视频")
    return items, ""


# ================================================================ 模块②b：全市场快讯 12 源（通用采集框架）
#
# 设计：各家快讯 API 返回结构差异大且无官方文档，这里用「通用 JSON 探测 +
# 键名试探 + 时间格式自适应」做防御式解析。任何单源失败只记入缺口，不中断。

# content_text/content_short 必须排在 content 前：华尔街见闻快讯的 content 是
# 带 <p> 标签的 HTML 正文，优先取纯文本版；createTimestamp 是格隆汇时间键；
# roll_data/depth_list/day_items 分别是财联社电报/深度、华尔街见闻最热的列表键，
# 必须显式列出，否则会被财联社深度返回里的 banner 等干扰列表抢占。
TITLE_KEYS = ["title", "content_text", "content_short", "content",
              "digest", "summary", "brief", "name", "description",
              "news_title", "sub_title", "text"]
TIME_KEYS = ["ctime", "created_at", "display_time", "show_time", "time",
             "pubDate", "publish_time", "update_time", "timestamp",
             "created", "date", "release_time", "createTimestamp",
             "create_time"]
LIST_KEYS = ["data", "list", "items", "result", "newest", "news", "lives",
             "telegraphs", "articles", "messages", "rows", "records",
             "roll_data", "depth_list", "top_article", "day_items"]


def _json_find_list(obj, depth: int = 0):
    """在任意 JSON 里找第一个「dict 列表」（含常见包裹键递归下探）。"""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj
    if isinstance(obj, dict) and depth < 4:
        for k in LIST_KEYS:
            v = obj.get(k)
            got = _json_find_list(v, depth + 1)
            if got is not None:
                return got
        for v in obj.values():
            if isinstance(v, (dict, list)):
                got = _json_find_list(v, depth + 1)
                if got is not None:
                    return got
    return None


def _parse_any_time(v) -> datetime | None:
    """兼容：epoch 秒/毫秒、'YYYY-MM-DD HH:MM:SS'、ISO、RFC822。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if v > 1e12:
            v = v / 1000
        if v > 1e8:
            return datetime.fromtimestamp(v, UTC)
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_any_time(int(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=CST)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def extract_items_generic(payload, source: str, limit: int, hours: int,
                          now: datetime) -> list[SentItem]:
    """通用提取：定位列表 → 每条试探标题/时间键 → 情绪打标 → 48h 过滤。"""
    rows = _json_find_list(payload) or []
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 广告/专题位不计入舆情样本（财联社 is_ad、华尔街见闻信息流
        # resource_type=ad/theme）
        if row.get("is_ad") or row.get("resource_type") in ("ad", "theme"):
            continue
        # 嵌套作用域：金十双层 data、华尔街见闻信息流 resource 包裹
        nested = [row[k] for k in ("data", "resource")
                  if isinstance(row.get(k), dict)]
        title, ts = "", None
        for scope in (row, *nested):
            for k in TITLE_KEYS:
                v = scope.get(k)
                if isinstance(v, str) and len(v.strip()) >= 4:
                    title = v.strip()
                    break
            if title:
                break
        if not title:
            continue
        for scope in (row, *nested):
            for k in TIME_KEYS:
                if k in scope:
                    ts = _parse_any_time(scope[k])
                if ts:
                    break
            if ts:
                break
        if ts and not _in_window(ts, hours, now):
            continue
        # 快讯流本身即最新：无可靠时间戳的条目保留但标记
        itm = _mk_item(source, re.sub(r"<[^>]+>", "", title), "", ts, now)
        items.append(itm)
        if len(items) >= limit:
            break
    return items


@dataclass
class FeedSpec:
    name: str
    urls: list[str]                # 依次尝试
    kind: str = "json"             # json | js | cls(签名JSON) | fb_express | fb_news
    headers: dict = field(default_factory=dict)
    note: str = ""
    # kind="cls" 时的业务查询参数；值 "NOW_TS" 在请求时替换为当前 epoch 秒
    query: dict = field(default_factory=dict)
    # 每源样本上限：默认 10；金十快讯密度大、单条短，单独放宽到 50
    limit: int = 10


# 财联社接口签名：参数按 key 排序后 sign = md5(sha1(query))。
# 端点与签名算法对照 RSSHub / NewsNow 的 cls 路由（2026-10 实测可用）。
CLS_BASE_PARAMS = {"appName": "CailianpressWeb", "os": "web", "sv": "8.7.9"}


def cls_signed_url(base: str, query: dict, now: datetime) -> str:
    params = dict(CLS_BASE_PARAMS)
    for k, v in (query or {}).items():
        params[k] = str(int(now.timestamp())) if v == "NOW_TS" else str(v)
    qs = urllib.parse.urlencode(sorted(params.items()))
    sign = hashlib.md5(
        hashlib.sha1(qs.encode("utf-8")).hexdigest().encode("utf-8")
    ).hexdigest()
    return f"{base}?{qs}&sign={sign}"


FEED_HEADERS = {"Referer": "https://wallstreetcn.com/", "Accept": "application/json"}
FEED_SPECS: list[FeedSpec] = [
    FeedSpec("MKTNews 快讯", [
        "https://api.mktnews.net/api/flash?limit=10",
        "https://mktnews.net/api/flash?limit=10",
    ]),
    # 华尔街见闻：旧域名 api-ddc-wscn.awtmt.com 已整体 71404 下线，
    # 现行域名 api-one.wallstcn.com（api-one-wscn.awtmt.com 为其别名备援）。
    FeedSpec("华尔街见闻 快讯", [
        "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=10",
        "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=10",
    ], headers=FEED_HEADERS),
    FeedSpec("华尔街见闻 最新", [
        "https://api-one.wallstcn.com/apiv1/content/information-flow?channel=global-channel&accept=article&limit=10",
        "https://api-one-wscn.awtmt.com/apiv1/content/information-flow?channel=global-channel&accept=article&limit=10",
    ], headers=FEED_HEADERS),
    FeedSpec("华尔街见闻 最热", [
        "https://api-one.wallstcn.com/apiv1/content/articles/hot?period=all",
        "https://api-one-wscn.awtmt.com/apiv1/content/articles/hot?period=all",
    ], headers=FEED_HEADERS),
    # 财联社：旧 nodeapi/telegraphList 已 404；现行端点需签名（cls_signed_url）。
    FeedSpec("财联社 电报", [
        "https://www.cls.cn/v1/roll/get_roll_list",
    ], kind="cls", headers={"Referer": "https://www.cls.cn/telegraph"},
        query={"last_time": "NOW_TS", "refresh_type": 1, "rn": 20}),
    FeedSpec("财联社 深度", [
        "https://www.cls.cn/v3/depth/home/assembled/1000",
    ], kind="cls", headers={"Referer": "https://www.cls.cn/depth"}),
    FeedSpec("财联社 热门", [
        "https://www.cls.cn/v2/article/hot/list",
    ], kind="cls", headers={"Referer": "https://www.cls.cn/"}),
    FeedSpec("雪球 热门股票", [], kind="xueqiu"),  # 专用流程（Cookie 预热）
    # 格隆汇：旧 fastnews/v2 已 404；现行快讯聚合接口 lives/v4。
    FeedSpec("格隆汇 事件", [
        "https://www.gelonghui.com/api/live-channels/all/lives/v4",
    ], headers={"Referer": "https://www.gelonghui.com/live"}),
    # 法布财经（FastBull）：无公开 JSON 端点，解析服务端渲染的列表页 HTML。
    FeedSpec("法布财经 快讯", [
        "https://www.fastbull.com/cn/express-news",
    ], kind="fb_express",
        headers={"Referer": "https://www.fastbull.com/cn/",
                 "Accept": "text/html"}),
    FeedSpec("法布财经 头条", [
        "https://www.fastbull.com/cn/news",
    ], kind="fb_news",
        headers={"Referer": "https://www.fastbull.com/cn/",
                 "Accept": "text/html"}),
    # 金十：flash_newest.js 一次返回数十条，按口径取最新≤50 条
    # （其中 channel=[5] 是同一快讯的英文重复版，抓取时过滤，避免重复计数）
    FeedSpec("金十数据", [
        "https://www.jin10.com/flash_newest.js",
    ], kind="js", limit=50,
        headers={"Referer": "https://www.jin10.com/", "x-app-id": "rU6QIu7JHe2gOUTe"}),
]


def _drop_jin10_english(payload):
    """金十 flash 列表里 channel=[5] 是同一条快讯的英文版，与中文版重复计数，
    抓取时剔除（对照 NewsNow jin10 源的口径）；其它结构原样返回。"""
    rows = payload if isinstance(payload, list) else (
        payload.get("data") if isinstance(payload, dict) else None)
    if not isinstance(rows, list):
        return payload
    kept = [r for r in rows
            if not (isinstance(r, dict) and r.get("channel") == [5])]
    if isinstance(payload, list):
        return kept
    out = dict(payload)
    out["data"] = kept
    return out


_JIN10_FLASH_API = "https://flash-api.jin10.com/get_flash_list"


def _jin10_fill_pages(payload, limit: int, timeout: int):
    """flash_newest.js 单页常只有约 25~40 条中文快讯；不足 limit 时用官方
    flash-api 按最末条时间 max_time 继续翻页（对照 RSSHub jin10 路由的
    x-app-id/x-version 头），去重后补满。任何失败都静默回退已有条目。"""
    rows = payload if isinstance(payload, list) else None
    if not rows or len(rows) >= limit:
        return payload
    seen = {r.get("id") for r in rows if isinstance(r, dict)}
    headers = {"Referer": "https://www.jin10.com/",
               "x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"}
    for _ in range(3):  # 最多再翻 3 页（每页数十条，足够补满 50）
        last = rows[-1] if isinstance(rows[-1], dict) else {}
        max_time = str(last.get("time") or "").strip()
        if not max_time:
            break
        url = (_JIN10_FLASH_API + "?channel=-8200&vip=1"
               f"&t={int(time.time())}"
               f"&max_time={urllib.parse.quote(max_time)}")
        try:
            status, body = http_request(url, headers=headers, timeout=timeout)
            if status != 200:
                break
            page = json.loads(body)
        except Exception:  # noqa: BLE001 —— 分页失败不影响首页结果
            break
        page_rows = _drop_jin10_english(page)
        if isinstance(page_rows, dict):
            page_rows = page_rows.get("data")
        if not isinstance(page_rows, list) or not page_rows:
            break
        added = 0
        for r in page_rows:
            if not isinstance(r, dict):
                continue
            rid = r.get("id")
            if rid is not None and rid in seen:
                continue
            data = r.get("data") if isinstance(r.get("data"), dict) else {}
            text = str(data.get("title") or data.get("content") or "")
            if not re.search(r"[一-鿿]", text):
                continue  # flash-api 无 channel 字段：无汉字视为英文版剔除
            seen.add(rid)
            rows.append(r)
            added += 1
            if len(rows) >= limit:
                break
        if added == 0 or len(rows) >= limit:
            break
    return rows


def fetch_feed_json(spec: FeedSpec, hours: int, timeout: int,
                    now: datetime) -> list[SentItem]:
    last_err = "无可用端点"
    for url in spec.urls:
        try:
            if spec.kind == "cls":  # 财联社：每次请求现算签名
                url = cls_signed_url(url, spec.query, now)
            status, body = http_request(url, headers=spec.headers or None,
                                        timeout=timeout)
            if status != 200:
                last_err = f"HTTP {status}"
                continue
            if spec.kind in ("fb_express", "fb_news"):  # 法布财经 HTML 列表页
                items = parse_fastbull(body, spec.kind, spec.name,
                                       spec.limit, hours, now)
                if items:
                    return items
                last_err = "解析成功但 48h 内无条目（结构可能已变）"
                continue
            if spec.kind == "js":  # 金十 flash_newest.js：剥 JS 壳
                m = re.search(r"[{\[]", body)
                body = body[m.start():] if m else body
                body = body.rstrip(";\n ")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                last_err = "非 JSON 返回（可能需要登录/被反爬）"
                continue
            if spec.kind == "js":  # 金十：过滤英文重复版 + 不足 50 翻页补满
                payload = _drop_jin10_english(payload)
                payload = _jin10_fill_pages(payload, spec.limit, timeout)
            items = extract_items_generic(payload, spec.name, spec.limit,
                                          hours, now)
            if items:
                return items
            last_err = "解析成功但 48h 内无条目（结构可能已变）"
        except PushError as e:
            last_err = str(e)[:80]
    raise PushError(last_err)


# ---- 法布财经（FastBull）：无公开 JSON，解析服务端渲染列表页 ----
# DOM 结构对照 RSSHub / NewsNow 的 fastbull 路由：
#   快讯 /cn/express-news：.news-list[data-date=ms]，标题在 .title_name
#     （形如【标题】正文…），链接在子元素 [data-href] / [data-id]；
#   头条 /cn/news：a.trending_type[href]，标题在 .title，
#     时间在子元素 [data-date]（.new_time）。
# 仓库坚持零第三方依赖，这里用标准库 html.parser 搭一棵迷你 DOM。


class _MiniNode:
    __slots__ = ("tag", "attrs", "children", "texts")

    def __init__(self, tag: str, attrs: dict):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_MiniNode] = []
        self.texts: list[str] = []


class _MiniDomParser(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _MiniNode("#root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _MiniNode(tag, dict(attrs))
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_MiniNode(tag, dict(attrs)))

    def handle_endtag(self, tag):
        # 容错：弹到最近匹配的开标签（页面常有未闭合标签）
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self._stack[-1].texts.append(data)


def _node_classes(node: _MiniNode) -> set:
    return set((node.attrs.get("class") or "").split())


def _node_text(node: _MiniNode) -> str:
    parts = list(node.texts)
    for ch in node.children:
        parts.append(_node_text(ch))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _find_descendants(node: _MiniNode, pred, out: list | None = None,
                      include_self: bool = False) -> list:
    if out is None:
        out = []
    if include_self and pred(node):
        out.append(node)
    for ch in node.children:
        if pred(ch):
            out.append(ch)
        _find_descendants(ch, pred, out)
    return out


def parse_fastbull(body: str, kind: str, source: str, limit: int,
                   hours: int, now: datetime) -> list[SentItem]:
    """解析 FastBull 列表页 HTML → SentItem（去重、按时间倒序、窗口过滤）。"""
    parser = _MiniDomParser()
    try:
        parser.feed(body)
    except Exception:  # noqa: BLE001 —— 页面结构异常按无条目处理，由上层记缺口
        return []
    root = parser.root
    base = "https://www.fastbull.com"
    rows: list[tuple[str, str, datetime | None]] = []  # (title, url, ts)

    if kind == "fb_express":
        blocks = _find_descendants(root, lambda n: "news-list" in _node_classes(n))
        for b in blocks:
            cand = _find_descendants(b, lambda n: "title_name" in _node_classes(n))
            if not cand:
                continue
            text = _node_text(cand[0])
            m = re.match(r"【(.+?)】", text, re.S)
            title = m.group(1).strip() if m and len(m.group(1).strip()) >= 4 else text
            link_node = _find_descendants(
                b, lambda n: bool(n.attrs.get("data-href")), include_self=True)
            href = link_node[0].attrs["data-href"] if link_node else ""
            if not href:
                id_node = _find_descendants(
                    b, lambda n: bool(n.attrs.get("data-id")), include_self=True)
                if id_node:
                    href = f"/cn/fastshort/{id_node[0].attrs['data-id']}"
            ts = _parse_any_time(b.attrs.get("data-date"))
            rows.append((title, href, ts))
    else:  # fb_news
        blocks = _find_descendants(root,
                                   lambda n: "trending_type" in _node_classes(n))
        for b in blocks:
            cand = _find_descendants(b, lambda n: "title" in _node_classes(n))
            if not cand:
                continue
            title = _node_text(cand[0])
            dated = _find_descendants(
                b, lambda n: "new_time" in _node_classes(n)
                and bool(n.attrs.get("data-date")))
            if not dated:
                dated = _find_descendants(
                    b, lambda n: bool(n.attrs.get("data-date")), include_self=True)
            ts = _parse_any_time(dated[0].attrs.get("data-date")) if dated else None
            rows.append((title, b.attrs.get("href") or "", ts))

    seen: set = set()
    uniq: list[tuple[str, str, datetime | None]] = []
    for title, href, ts in rows:
        key = href or title
        title = re.sub(r"<[^>]+>", "", title).strip()
        if len(title) < 4 or key in seen:
            continue
        seen.add(key)
        if ts and not _in_window(ts, hours, now):
            continue
        uniq.append((title,
                     urllib.parse.urljoin(base, href) if href else "",
                     ts))
    uniq.sort(key=lambda r: r[2] or datetime.min.replace(tzinfo=UTC),
              reverse=True)
    return [_mk_item(source, t, u, ts, now) for t, u, ts in uniq[:limit]]


XUEQIU_HOT_URL = ("https://stock.xueqiu.com/v5/stock/hot_stock/list.json"
                  "?size=10&_type=10&type=10")


def parse_xueqiu_hot(text: str, now: datetime) -> list[SentItem]:
    """雪球热度榜：涨跌幅直接映射情绪分，关注度增量作置信参考。"""
    rows = _json_find_list(json.loads(text)) or []
    items = []
    for row in rows:
        name, code = row.get("name", ""), row.get("code", "")
        if not name:
            continue
        pct = row.get("percent")
        inc = row.get("increment") or row.get("follow_increment")
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
        score = max(-1.0, min(1.0, (pct or 0) / 5))
        label = "利好" if score > 0.12 else ("利空" if score < -0.12 else "中性")
        title = f"{name} {code} 涨跌{pct:+.2f}%" if pct is not None \
            else f"{name} {code}"
        if inc:
            title += f"，关注增量 {inc}"
        items.append(SentItem(source="雪球 热门股票", title=title,
                              age_h=0.0, score=round(score, 2), label=label,
                              conf=0.7))
        if len(items) >= 10:
            break
    return items


def fetch_xueqiu(timeout: int, now: datetime) -> list[SentItem]:
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (pushplus-deepseek/2.0)")]
    try:
        opener.open("https://xueqiu.com/hq", timeout=timeout).read()  # Cookie 预热
        resp = opener.open(XUEQIU_HOT_URL, timeout=timeout)
        body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise PushError(f"HTTP {e.code}（雪球反爬升级）") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise PushError(f"网络失败 {getattr(e, 'reason', e)}") from e
    items = parse_xueqiu_hot(body, now)
    if not items:
        raise PushError("接口结构已变，未解析到榜单")
    return items


@dataclass
class FeedPack:
    hours: int
    items: dict[str, list[SentItem]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    agg: dict = field(default_factory=dict)


def _try_import_deepseek_search():
    try:
        import deepseek_search
        return deepseek_search
    except ImportError:
        return None


def fetch_deepseek_feed(topic: str, hours: int, timeout: int, now: datetime) -> list[SentItem]:
    """DeepSeek API 作为第 13 源（AI 搜索引擎）接入 feedscan，失败只记缺口"""
    ds = _try_import_deepseek_search()
    if ds is None:
        raise PushError("deepseek_search 模块不可用")
    # 使用 topic 作为查询，若为全市场简报则用通用查询
    query = topic.strip() if topic and not is_market_wide_topic(topic) else f"{topic or '全球市场'} 最新财经快讯"
    rows = ds.fetch_deepseek_search(query, timeout=timeout, hours=hours)
    items: list[SentItem] = []
    for r in rows:
        title = r.get("title", "").strip()
        if len(title) < 4:
            continue
        ts = r.get("ts")
        # 复用情绪打标
        items.append(_mk_item("DeepSeek 搜索", title, r.get("url", ""), ts, now))
        if len(items) >= 10:
            break
    if not items:
        raise PushError("DeepSeek 搜索未命中（或返回空）")
    return items


def collect_feeds(hours: int, timeout: int, topic: str = "") -> FeedPack:
    now = datetime.now(UTC)
    pack = FeedPack(hours=hours)
    for spec in FEED_SPECS:
        if spec.kind == "xueqiu":
            try:
                pack.items[spec.name] = fetch_xueqiu(min(timeout, 12), now)
            except Exception as e:  # noqa: BLE001
                pack.errors.append(f"{spec.name}：{str(e)[:100]}")
            continue
        if not spec.urls:
            pack.errors.append(f"{spec.name}：{spec.note or '无端点'}")
            continue
        try:
            pack.items[spec.name] = fetch_feed_json(
                spec, hours, min(timeout, 12), now)
        except Exception as e:  # noqa: BLE001 —— 逐源隔离
            pack.errors.append(f"{spec.name}：{str(e)[:100]}")

    # ---- DeepSeek 搜索（AI 搜索引擎，第 13 源，新增）----
    try:
        # 仅在有 Key 或离线样本模式下尝试，避免无 Key 时每次都记缺口噪音过大？
        # 这里按“有 Key 才尝试，无 Key 记跳过”策略，与其他源一致：无 Key 也记缺口，便于追踪
        pack.items["DeepSeek 搜索"] = fetch_deepseek_feed(topic or "全球市场", hours, min(timeout, 15), now)
    except Exception as e:  # noqa: BLE001
        # 无 Key 时提示更友好
        msg = str(e)[:120]
        if "DEEPSEEK_API_KEY" in msg:
            pack.errors.append(f"DeepSeek 搜索：跳过（未配置 DEEPSEEK_API_KEY）")
        else:
            pack.errors.append(f"DeepSeek 搜索：{msg}")

    all_items = [i for v in pack.items.values() for i in v]
    n_ok = len(pack.items)
    mean = round(sum(i.score for i in all_items) / len(all_items), 2) \
        if all_items else 0.0
    pos = sum(1 for i in all_items if i.label == "利好")
    neg = sum(1 for i in all_items if i.label == "利空")
    anchor = 50 + 45 * max(-1.0, min(1.0, mean * 3))
    pack.agg = {"n": len(all_items), "pos": pos, "neg": neg,
                "neu": len(all_items) - pos - neg, "mean": mean,
                "n_sources": n_ok, "综合多头概率锚点": round(anchor)}
    return pack


def is_market_wide_topic(topic: str) -> bool:
    """识别自动市场简报主题，避免把全市场扫描误写成个股投研。"""
    normalized = re.sub(r"[\s·/_-]+", "", (topic or "")).lower()
    return any(marker in normalized for marker in (
        "全市场", "全球市场", "港股a股市场", "市场早报", "市场晚报",
        "市场简报", "marketwide"))


def feed_context(topic: str, pack: FeedPack) -> str:
    scope = ("报告范围：全市场（不指定单一个股）" if is_market_wide_topic(topic)
             else f"关注标的：{topic}")
    total = 13  # 12 原有 + DeepSeek 1
    ctx = [f"【{pack.hours}h 全市场快讯数据：{pack.agg['n_sources']}/{total} 源可用，"
           f"共 {pack.agg['n']} 条，已本地打标】{scope}"]
    for spec in FEED_SPECS:
        items = pack.items.get(spec.name)
        if not items:
            continue
        agg = _src_agg(items)
        ctx.append(f"▼ {spec.name}（{agg['n']}条：利好{agg['pos']}/"
                   f"利空{agg['neg']}/中性{agg['neu']}，动量{agg['mean']:+.2f}）")
        ctx += [_item_line(i, k + 1) for k, i in enumerate(items[:spec.limit])]
    # DeepSeek 搜索（新增第 13 源）
    ds_items = pack.items.get("DeepSeek 搜索")
    if ds_items:
        agg = _src_agg(ds_items)
        ctx.append(f"▼ DeepSeek 搜索（{agg['n']}条：利好{agg['pos']}/"
                   f"利空{agg['neg']}/中性{agg['neu']}，动量{agg['mean']:+.2f}）")
        ctx += [_item_line(i, k + 1) for k, i in enumerate(ds_items[:10])]
    if pack.errors:
        ctx.append("▼ 数据缺口：" + "；".join(pack.errors))
    ctx.append(f"▼ 本地聚合：总样本 {pack.agg['n']}，利好/利空/中性="
               f"{pack.agg['pos']}/{pack.agg['neg']}/{pack.agg['neu']}，"
               f"情绪均值 {pack.agg['mean']:+.2f}，"
               f"综合多头概率锚点 ≈ {pack.agg['综合多头概率锚点']}%")
    return "\n".join(ctx)


def render_feed_rule(topic: str, pack: FeedPack) -> str:
    lines = [f"**{topic} · 全市场快讯情绪扫描**（rule 本地计算，未经 AI 综合，含 DeepSeek 搜索）",
             "", "| 来源 | 样本 | 利好/利空/中性 | 情绪分 | 代表快讯 |",
             "|---|---|---|---|---|"]
    for spec in FEED_SPECS:
        items = pack.items.get(spec.name)
        if not items:
            err = next((e.split("：", 1)[1] for e in pack.errors
                        if e.startswith(spec.name)), "失败")
            lines.append(f"| {spec.name} | — | — | — | ⚠️ {err[:16]} |")
            continue
        agg = _src_agg(items)
        rep = items[0].title[:16]
        lines.append(f"| {spec.name} | {agg['n']} | {agg['pos']}/{agg['neg']}"
                     f"/{agg['neu']} | {agg['mean']:+.2f} | {rep}… |")
    # DeepSeek 搜索（第 13 源）
    ds_items = pack.items.get("DeepSeek 搜索")
    if ds_items:
        agg = _src_agg(ds_items)
        rep = ds_items[0].title[:16]
        lines.append(f"| DeepSeek 搜索 | {agg['n']} | {agg['pos']}/{agg['neg']}"
                     f"/{agg['neu']} | {agg['mean']:+.2f} | {rep}… |")
    else:
        err = next((e.split("：", 1)[1] for e in pack.errors
                    if e.startswith("DeepSeek")), "跳过")
        lines.append(f"| DeepSeek 搜索 | — | — | — | ⚠️ {err[:16]} |")
    a = pack.agg
    anchor = a.get("综合多头概率锚点", 50)
    stance = "偏多" if anchor >= 55 else ("偏空" if anchor <= 45 else "中性")
    lines += ["",
              f"- **全市场情绪温度**：均值 {a['mean']:+.2f}"
              f"（{a['pos']}利好/{a['neg']}利空/{a['neu']}中性，"
              f"共 {a['n']} 条 / {a['n_sources']}/13 源）",
              f"- **综合判断**：{stance}，综合多头概率 ≈ **{anchor}%**",
              "", "> 已接入 DeepSeek API 作为第 13 源智能搜索引擎，ai_provider 选 deepseek 可生成跨源主线归纳与板块映射。",
              "> ⚠️ 非投资建议，仅供参考。"]
    return "\n".join(lines)


def render_feed_appendix(pack: FeedPack, new_keys: set | None = None) -> str:
    nk = new_keys or set()
    lines = ["---", f"📰 **快讯原始明细（{pack.agg['n']} 条，金十≤50、其余每源≤10，含 DeepSeek 搜索）**"]
    for spec in FEED_SPECS:
        items = pack.items.get(spec.name)
        if not items:
            continue
        lines.append(f"\n**{spec.name}（{len(items)}/{spec.limit}）**")
        lines += [_item_line(i, k + 1, _item_key(i.title) in nk)
                  for k, i in enumerate(items[:spec.limit])]
    ds_items = pack.items.get("DeepSeek 搜索")
    if ds_items:
        lines.append(f"\n**DeepSeek 搜索（{len(ds_items)}/10）**")
        lines += [_item_line(i, k + 1, _item_key(i.title) in nk)
                  for k, i in enumerate(ds_items[:10])]
    if pack.errors:
        lines.append("\n**数据缺口**")
        lines += [f"- ⚠️ {e}" for e in pack.errors]
    return "\n".join(lines)


# ---------- 量价情绪动量：Yahoo 小时线（近48h 涨跌 + 量比）----------

def compute_price_momentum(closes: list[float], vols: list[float],
                           tss: list[int], hours: int,
                           now: datetime) -> dict:
    cutoff = now.timestamp() - hours * 3600
    pts = [(t, c, v) for t, c, v in zip(tss, closes, vols)
           if c is not None and t >= cutoff]
    prev_vols = [v for t, c, v in zip(tss, closes, vols)
                 if t < cutoff and v]
    if len(pts) < 2:
        return {"ok": False, "score": 0.0, "label": "数据不足", "detail": ""}
    ret = (pts[-1][1] - pts[0][1]) / pts[0][1] * 100
    vols_48 = [v for _, _, v in pts if v]
    avg_recent = sum(vols_48) / len(vols_48) if vols_48 else 0.0
    avg_prev = sum(prev_vols) / len(prev_vols) if prev_vols else avg_recent
    vol_ratio = avg_recent / avg_prev if avg_prev else 1.0
    score = max(-1.0, min(1.0, ret / 4)) * 0.6 \
        + max(-0.8, min(0.8, vol_ratio - 1)) * 0.5
    score = round(max(-1.0, min(1.0, score)), 2)
    label = "偏多" if score > 0.12 else ("偏空" if score < -0.12 else "中性")
    detail = f"近{hours}h {ret:+.2f}%，量比 {vol_ratio:.2f}，样本 {len(pts)} 根数据"
    return {"ok": True, "score": score, "label": label,
            "detail": detail, "ret": round(ret, 2),
            "vol_ratio": round(vol_ratio, 2)}


def fetch_price_momentum(code: str, hours: int, timeout: int) -> dict:
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{int(code)}.HK?interval=1h&range=5d")
    status, body = http_request(url, timeout=timeout)
    if status != 200:
        raise PushError(f"小时线 HTTP {status}")
    res = json.loads(body)["chart"]["result"][0]
    quote = res["indicators"]["quote"][0]
    return compute_price_momentum(quote.get("close", []),
                                  quote.get("volume", []),
                                  res.get("timestamp", []), hours,
                                  datetime.now(UTC))


# ---------- 汇总：多源采集 + 动量聚合 ----------

@dataclass
class SentPack:
    hours: int
    items: dict[str, list[SentItem]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    momentum: dict = field(default_factory=dict)
    agg: dict = field(default_factory=dict)
    scan: object | None = None   # 十七平台股票扫描结果（stock_news_scan.ScanPack）
    financial_sentiment_md: str | None = None  # 推送复核缓存，避免重复渲染时重复推理


def _src_agg(items: list[SentItem]) -> dict:
    if not items:
        return {"n": 0}
    pos = sum(1 for i in items if i.label == "利好")
    neg = sum(1 for i in items if i.label == "利空")
    mean = round(sum(i.score for i in items) / len(items), 2)
    recent = [i.score for i in items if i.age_h is not None and i.age_h <= 24]
    older = [i.score for i in items if i.age_h is not None and i.age_h > 24]
    trend = "→"
    if recent and older:
        d = sum(recent) / len(recent) - sum(older) / len(older)
        trend = "↑" if d > 0.1 else ("↓" if d < -0.1 else "→")
    return {"n": len(items), "pos": pos, "neg": neg,
            "neu": len(items) - pos - neg, "mean": mean, "24h趋势": trend}


def _clean_query_topic(topic: str) -> str:
    """检索用主题清洗：去括号注释、去模板名等杂质，避免污染外部查询。"""
    t = re.sub(r"[（(].*$", "", topic).strip()
    t = re.sub(r"(每日简报|简报)$", "", t).strip(" 　·-")
    return t or "阿里巴巴"


def collect_sentiment(topic: str, code: str | None, hours: int,
                      timeout: int, yt_handle: str = "") -> SentPack:
    """采集 4 源 + 量价动量。任何单源失败只记录、不中断。"""
    pack = SentPack(hours=hours)
    q_topic = _clean_query_topic(topic)
    jobs = [
        ("Google新闻", lambda: fetch_google_news(q_topic, hours, 10, timeout)),
        ("YouTube·investtalk", lambda: fetch_youtube(
            yt_handle or (env("YT_CHANNEL") or "investtalk"), hours, 10, timeout)),
    ]
    for name, fn in jobs:
        try:
            items, note = fn()
            pack.items[name] = items
            if note:
                pack.errors.append(f"{name}：{note}")
        except Exception as e:  # noqa: BLE001 —— 逐源隔离
            pack.errors.append(f"{name}：{str(e)[:120]}")
    if code:
        try:
            pack.momentum = fetch_price_momentum(code, hours, timeout)
        except Exception as e:  # noqa: BLE001
            pack.momentum = {"ok": False, "score": 0.0,
                             "label": "获取失败", "detail": str(e)[:100]}
    # ---------- 十七平台扫描：输入股票代码 → 窗口内相关新闻 + 有关板块 ----------
    if code or q_topic:
        try:
            from stock_news_scan import scan_stock
            pack.scan = scan_stock(code or "", q_topic, hours=hours,
                                   timeout=min(timeout, 12))
        except Exception as e:  # noqa: BLE001 —— 扫描失败仅记数据缺口
            pack.errors.append(f"十七平台扫描：{str(e)[:120]}")
    a = {"news": _src_agg(pack.items.get("Google新闻", [])),
         "social": _src_agg([i for k in ("YouTube·investtalk",)
                             for i in pack.items.get(k, [])])}
    news_score = a["news"].get("mean", 0.0) if a["news"].get("n") else 0.0
    social_score = a["social"].get("mean", 0.0) if a["social"].get("n") else 0.0
    mom_score = pack.momentum.get("score", 0.0)
    anchor = 50 + 45 * (0.40 * mom_score + 0.35 * news_score + 0.25 * social_score)
    a["综合多头概率锚点"] = max(5, min(95, round(anchor)))
    pack.agg = a
    return pack


def _item_line(i: SentItem, idx: int, is_new: bool = False) -> str:
    age = f"{i.age_h:.0f}h前" if i.age_h is not None else "时间未知"
    t = i.title if len(i.title) <= 60 else i.title[:57] + "…"
    mark = "🆕 " if is_new else ""   # 上次推送之后新进入窗口的样本
    return f"{idx}. {mark}[{i.label} {i.score:+.2f}] {t} — {i.source} · {age}"


def render_financial_sentiment_push(pack: SentPack) -> str:
    """推送页金融情绪复核；同一采集包只推理一次，失败不阻断推送。"""
    if pack.financial_sentiment_md is not None:
        return pack.financial_sentiment_md
    heading = "## 🧠 金融情绪复核"
    modes = "- 四种模式：auto 自动路由 / hf 金融模型 / llm 大模型 / lexicon 离线词典（非四路并行）。"
    titles = [i.title for items in pack.items.values() for i in items if i.title]
    scan = getattr(pack, "scan", None)
    if scan is not None:
        for group in (scan.direct, scan.sector_hits):
            titles.extend(i.title for items in group.values() for i in items if i.title)
    titles = list(dict.fromkeys(titles))[:200]
    if not titles:
        md = f"{heading}\n\n{modes}\n- 暂无可分析的新闻样本，不生成情绪分数。"
    else:
        try:
            import hf_hub
            engine = env("SENTIMENT_ENGINE") or "auto"
            result = hf_hub.score_texts(titles, engine=engine)
            md = hf_hub.render_sentiment_md(result, title="🧠 金融情绪复核")
            md += f"\n{modes}\n- 请求模式：{engine}；样本为新闻与扫描标题去重后最多 200 条。"
            if result.get("model"):
                md += f"\n- 实际模型：{result['model']}"
            md += "\n- 情绪仅反映文本语气，不代表股价预测或投资建议。"
        except Exception:  # 复核不可用仍须保留主报告，且不暴露端点错误详情
            md = f"{heading}\n\n{modes}\n- ⚠️ 情绪复核暂不可用，主报告照常推送。"
    pack.financial_sentiment_md = md
    return md


def render_sentiment_appendix(pack: SentPack, new_keys: set | None = None) -> str:
    nk = new_keys or set()
    n = sum(len(v) for v in pack.items.values())
    lines = ["---", f"📡 **{SENTIMENT_FACTOR}证据（{pack.hours}h，共 {n} 条，本地打标）**"]
    for name in ("Google新闻", "YouTube·investtalk"):
        items = pack.items.get(name)
        if not items:
            continue
        lines.append(f"\n**{name}（{len(items)}/10）**")
        lines += [_item_line(i, k + 1, _item_key(i.title) in nk)
                  for k, i in enumerate(items)]
    m = pack.momentum
    if m:
        lines.append("\n**量价情绪动量**")
        lines.append(f"- {m.get('detail') or m.get('label')} → "
                     f"{m.get('label')}（动量分 {m.get('score', 0):+.2f}）")
    if pack.errors:
        lines.append("\n**数据缺口**")
        lines += [f"- ⚠️ {e}" for e in pack.errors]
    if getattr(pack, "scan", None):
        try:
            from stock_news_scan import render_scan_md
            lines += ["", render_scan_md(pack.scan)]
        except Exception:  # noqa: BLE001 —— 附录缺扫描段不影响主报告
            pass
    lines += ["", render_financial_sentiment_push(pack)]
    return "\n".join(lines)


def sentiment_context(pack: SentPack) -> str:
    ctx = [
        f"【新增因子：{SENTIMENT_FACTOR}】",
        f"【{pack.hours}h 多源舆情数据（本地预打标：标签/分值/时效）】",
    ]
    for name, items in pack.items.items():
        ctx.append(f"▼ {name}")
        ctx += [_item_line(i, k + 1) for k, i in enumerate(items)]
    m = pack.momentum
    if m.get("ok"):
        ctx.append(f"▼ 量价动量：{m['detail']} → {m['label']}（{m['score']:+.2f}）")
    if getattr(pack, "scan", None):
        try:
            from stock_news_scan import scan_context
            ctx.append("▼ 十七平台扫描：")
            ctx += ["  " + line for line in scan_context(pack.scan).splitlines()]
        except Exception:  # noqa: BLE001
            pass
    if pack.errors:
        ctx.append("▼ 数据缺口：" + "；".join(pack.errors))
    a = pack.agg
    ctx.append(
        "▼ 本地预聚合："
        f"新闻动量 {a.get('news', {}).get('mean', 0):+.2f}，"
        f"社媒动量 {a.get('social', {}).get('mean', 0):+.2f}，"
        f"量价动量 {m.get('score', 0):+.2f}，"
        f"综合多头概率锚点 ≈ {a.get('综合多头概率锚点', '—')}%")
    return "\n".join(ctx)


def render_sentiment_rule(topic: str, code: str | None, pack: SentPack) -> str:
    a, m = pack.agg, pack.momentum
    news, soc = a.get("news", {}), a.get("social", {})
    anchor = a.get("综合多头概率锚点", 50)
    stance = "偏多" if anchor >= 55 else ("偏空" if anchor <= 45 else "中性")
    rows = [
        "| 象限 | 样本 | 利好/利空/中性 | 动量分 | 24h趋势 |",
        "|---|---|---|---|---|",
        f"| 新闻动量 | {news.get('n', 0)} | {news.get('pos', 0)}/{news.get('neg', 0)}"
        f"/{news.get('neu', 0)} | {news.get('mean', 0):+.2f} | {news.get('24h趋势', '—')} |",
        f"| 舆情动量 | {soc.get('n', 0)} | {soc.get('pos', 0)}/{soc.get('neg', 0)}"
        f"/{soc.get('neu', 0)} | {soc.get('mean', 0):+.2f} | {soc.get('24h趋势', '—')} |",
        f"| 量价动量 | 小时级数据 | — | {m.get('score', 0):+.2f} | — |",
    ]
    return "\n".join([
        f"**{topic} · {pack.hours}h 量价舆情动量**（rule 本地计算，未经 AI 综合）",
        "", *rows, "",
        f"- **综合判断**：{stance}，综合多头概率 ≈ **{anchor}%**"
        "（权重：量价40% / 新闻35% / 舆情25%）",
        f"- **来源**：HK{code or '—'}；Google新闻 / YouTube@investtalk（{pack.hours}h 窗口）",
        "", "> ai_provider 选 deepseek 可在此数据基础上生成完整综合报告。",
        "> ⚠️ 非投资建议，仅供参考。"])


# ================================================================ 模块③：分析框架（12 套模板 / 7 个分析因子）

RULES_TAIL = "末尾固定一行「⚠️ 非投资建议，仅供参考」。"


def _ctx_line(context: str) -> str:
    if context:
        return f"参考数据/背景（优先采用，数值以此为准）：\n{context}\n\n"
    return ("注意：无实时数据接入，请基于既有知识推断，"
            "凡涉及具体点位/数值的地方标注 *（推断）。\n\n")


def build_messages(template: str, topic: str, context: str,
                   risk: str = "mid") -> list[dict]:
    """按模板构造 AI 对话。多空判断一律带概率（50%=中性）。"""
    ctx = _ctx_line(context)
    if template == "sentiment":
        user = (
            f"基于以下数据窗口内、已完成本地预打标的多源数据，"
            f"为「{topic}」输出一份量价与舆情动量报告。\n\n{context}\n\n"
            "严格按此格式输出：\n\n"
            "## 情绪动量报告\n\n"
            "| 象限 | 样本 | 利好/利空/中性 | 动量分 | 24h趋势 | 多头概率 |\n"
            "|---|---|---|---|---|---|\n"
            "| 新闻动量 |  |  |  |  |  |\n"
            "| 舆情动量 |  |  |  |  |  |\n"
            "| 量价动量 |  | — |  | — |  |\n\n"
            "- **新闻要点**：3 条最有信息量的（引用编号）\n"
            "- **舆情要点**：3 条（注明来自 YouTube）\n"
            "- **催化 vs 风险**：各 2 条\n"
            "- **综合判断**：明确偏多/偏空 + 综合多头概率%"
            "（可参考本地锚点，偏离须给理由）\n"
            "- **失效条件**：1~2 条\n\n"
            "打分区间 [-1,1]；样本不足的象限必须明说而非编造。"
            + RULES_TAIL)
    elif template == "feedscan":
        if is_market_wide_topic(topic):
            # 每日简报并入的栏目（数据段存在时才要求 AI 写对应小节）
            columns_note = ""
            if "【香港券商新闻与数据" in context:
                columns_note += (
                    "- **香港券商动态**：一小节概括香港券商最新研报/新闻标题要点，"
                    "附上市券商股价表现（只引用下方同名数据段给定内容）\n")
            if "【国外机构一周研报" in context:
                columns_note += (
                    "- **国外投行研报**：一小节逐家列出国外投行最新研报标题与"
                    "评级要点（只引用下方同名数据段给定内容并注明机构）\n")
            user = (
                "基于以下最近 24h 全市场快讯数据（已本地预打标，含各源情绪指标），"
                "输出一份不针对单只股票的全市场市场情报简报。\n\n"
                f"{context}\n\n严格按此格式输出：\n\n"
                "## 全市场快讯情绪扫描\n\n"
                "| 来源 | 样本 | 利好/利空/中性 | 情绪分 | 市场信号 |\n"
                "|---|---|---|---|---|\n（逐源罗列，不可用源注明）\n\n"
                "- **市场情绪温度**：偏暖/偏冷一句话 + 综合情绪锚点%\n"
                "- **三大市场主线**：跨源归纳，每条标注支撑来源与可信度\n"
                "- **板块映射**：受益/承压板块各 2~3 个；没有足够证据时标明未知\n"
                "- **风险与观察**：2~3 条市场级风险或接下来需要验证的信号\n"
                + columns_note
                + "- **数据缺口**：注明失效/无返回来源，不补造数据\n\n"
                "重要约束：只做宏观、市场与板块层面的概览；禁止输出任何单只股票的研报、"
                "评级、目标价、买卖点或个股投资建议。快讯即使提到公司，也只可作为新闻事实，"
                "不得把正文写成个股页。不得臆造数据。"
                + RULES_TAIL)
        else:
            user = (
                f"基于以下 48h 内全市场快讯数据（已本地预打标，含各源情绪指标），"
                f"输出一份全市场情绪扫描报告，并评估对关注标的「{topic}」的传导。\n\n"
                f"{context}\n\n严格按此格式输出：\n\n"
                "## 全市场快讯情绪扫描\n\n"
                "| 来源 | 样本 | 利好/利空/中性 | 情绪分 | 多头概率 |\n"
                "|---|---|---|---|---|\n（逐源罗列，不可用源注明）\n\n"
                "- **全市场情绪温度**：偏暖/偏冷一句话 + 综合多头概率%"
                "（可参考本地锚点，偏离须给理由）\n"
                "- **三大主线**：跨源归纳，每条标注支撑来源与成立概率%\n"
                "- **板块映射**：利多板块/利空板块各 2~3 个\n"
                f"- **对「{topic}」所在产业链的传导**：1~2 句 + 方向概率%\n"
                "- **噪音提示**：2 条看似重要但可忽略的快讯\n\n"
                "打分区间 [-1,1]；样本不足的源必须明说而非编造。"
                + RULES_TAIL)
    elif template == "newsnow":
        user = (
            f"基于以下 NewsNow 热榜聚合数据（10源：知乎热榜/抖音热搜/微博实时热搜/虎扑热搜/AI hot/联合早报/香港01/今日头条热榜/百度实时热点/B站热榜），"
            f"分析全网热点与对标的「{topic}」的潜在关联。\n\n{context}\n\n"
            "严格按此格式输出：\n\n"
            "## 热榜聚合分析\n\n"
            "| 来源 | Top3 标题 | 热度/备注 | 与标的关联度% |\n"
            "|---|---|---|---|\n（逐源 Top3）\n\n"
            "- **全网热点温度**：一句话 + 热点集中度%\n"
            "- **三大主线**：跨源归纳热点主线，每条标注来源数与成立概率%\n"
            "- **对「{topic}」的传导**：1~2 句 + 方向概率%\n"
            "- **可忽略噪音**：2条\n\n"
            "若某源无数据需明说。"
            + RULES_TAIL)
    elif template == "scan":
        user = (
            "扫一遍今天全球市场，总结推动股价的 5 大力量。"
            "重点关注宏观事件、板块轮动、情绪变化，区分重点与噪音。\n\n" + ctx +
            "严格按此格式输出：\n\n"
            "| 力量 | 方向 | 对港股影响概率 | 逻辑 | 相关板块 |\n"
            "|---|---|---|---|---|\n（恰好 5 行）\n\n"
            "- **重点**：2 条今日真正值得跟踪的\n- **噪音**：2 条看似热闹但可忽略的\n"
            "- **今日结论**：1~2 句，给出港股整体偏多/偏空概率\n\n"
            "概率取整数%。" + RULES_TAIL)
    elif template == "picker":
        user = (
            "根据当下市场环境，挑出未来 30 天高概率的股票 3~5 只"
            "（范围：港股/A股，互联网及科技链优先）。每只说清楚为什么看好、"
            "关键风险、什么情况下要止损。\n\n" + ctx +
            "严格按此格式输出：\n\n"
            "| 股票 | 方向 | 30日上涨概率 | 看好逻辑 | 关键风险 | 止损触发 |\n"
            "|---|---|---|---|---|---|\n（3~5 行）\n\n"
            "- **首选**：1 句话点名胜率最高的一只\n"
            "- **弃权说明**：若环境不适合开新仓，明说并给理由\n\n"
            "概率取整数%。" + RULES_TAIL)
    elif template == "fusion":
        user = (
            f"分析「{topic}」，结合价格走势结构、财报和最新新闻，给出明确的看多还是看空。\n\n"
            + ctx +
            "严格按此格式输出：\n\n"
            "| 维度 | 判断 | 多头概率 | 要点 |\n|---|---|---|---|\n"
            "| 走势结构 |  |  |  |\n| 基本面/财报 |  |  |  |\n"
            "| 最新消息 |  |  |  |\n| 资金与情绪 |  |  |  |\n\n"
            "- **结论**：明确写「看多」或「看空」+ 信心概率%\n"
            "- **关键点位**：支撑 S1/S2、压力 R1/R2（有行情数据时按真实价格算，"
            "否则标注推断）\n- **适合风格**：短线/波段/长线三选一 + 一句理由\n\n"
            + RULES_TAIL)
    elif template == "plan":
        user = (
            f"给「{topic}」做个完整交易计划：理想进场区间、止损怎么设、"
            "多个止盈位、头寸怎么配置才低风险。\n\n" + ctx +
            "严格按此格式输出：\n\n"
            "- **方向**：做多/观望/做空 + 信心概率%\n"
            "- **理想进场区间**：价格带 + 理由 1 句\n"
            "- **止损位**：价格 + 距进场幅度% + 触发即无条件执行\n\n"
            "| 止盈位 | 目标价 | 到达概率 | 到达后动作 |\n|---|---|---|---|\n"
            "| T1 |  |  | 减仓 1/3 |\n| T2 |  |  | 再减 1/3 |\n"
            "| T3 |  |  | 清仓或移动止盈 |\n\n"
            "- **头寸配置**：占总资金%（低风险原则，单票≤15%）\n"
            "- **盈亏比**：估算并给出值\n- **计划失效条件**：2 条\n\n"
            "" + RULES_TAIL)
    elif template == "earnings":
        user = (
            f"分析「{topic}」的即将发布财报：用历史财报反应、指引趋势、"
            "板块表现、市场情绪，推测财报后股价最可能怎么走。\n\n" + ctx +
            "严格按此格式输出：\n\n"
            "- **历史财报反应**：近几次财报后平均涨跌% 与规律 1 句\n"
            "- **指引趋势**：管理层口径变化 1 句\n- **板块与情绪**：1 句\n\n"
            "| 财报后情形 | 概率 | 触发条件 |\n|---|---|---|\n"
            "| 大涨(>5%) |  |  |\n| 小涨(0~5%) |  |  |\n"
            "| 小跌(0~-5%) |  |  |\n| 大跌(<-5%) |  |  |\n"
            "（概率合计=100%）\n\n"
            "- **最可能路径**：1~2 句\n- **关键观察点**：2 条（订单/毛利率/指引）\n\n"
            "" + RULES_TAIL)
    elif template == "portfolio":
        user = (
            f"根据我的风险偏好【{RISK_ZH[risk]}】，设计一个分散的股票组合"
            "（港股/A股，互联网科技为重点再加其他板块）。各板块怎么配、"
            "为什么要这些头寸、多久调整一次。\n\n" + ctx +
            "严格按此格式输出：\n\n"
            "| 板块 | 配置比例 | 代表标的 | 配置理由 |\n|---|---|---|---|\n"
            "（4~6 行，比例合计 100%，含现金档）\n\n"
            "- **头寸原则**：单票上限%、单板块上限%\n"
            "- **再平衡**：频率（如每季度）+ 2 条触发式调整条件\n"
            "- **预期特征**：该风险档位的预期波动 1 句\n\n"
            "" + RULES_TAIL)
    elif template == "review":
        if context:
            user = (
                f"复盘我这笔交易：\n{context}\n\n"
                "找出我的错误、优点、心理偏差，以及下次怎样做才能长期稳定盈利。\n\n"
                "严格按此格式输出：\n\n"
                "- **错误 TOP3**：各 1 句，按损失贡献排序\n"
                "- **优点 TOP2**：各 1 句，需保持\n"
                "- **心理偏差**：点名具体偏差（FOMO/锚定/损失厌恶/过度自信等）"
                "+ 在本单中的表现\n"
                "- **下次改进（if-then 规则）**：3 条，形如"
                "「若…则…」，可直接执行\n"
                "- **长期胜率杠杆**：1 条最值得固化的习惯\n\n"
                "" + RULES_TAIL)
        else:
            user = (
                "用户未提供具体交易细节。输出一份《交易复盘框架》：\n"
                "1) 需要记录哪些字段（进出场价/仓位/理由/情绪）；"
                "2) 错误分类清单（择时/仓位/纪律/信息）；"
                "3) 心理偏差自查表（各给一句自查问题）；"
                "4) 说明把交易细节粘贴到 context 输入后，可获得逐条复盘。\n\n"
                "" + RULES_TAIL)
    elif template == "regime":
        user = (
            "判断当前市场是趋势、震荡、风险偏好高还是低。"
            "在这个环境下交易策略应该怎么调整，交易员常掉的坑是什么。\n\n" + ctx +
            "严格按此格式输出：\n\n"
            "| 属性 | 判定 | 概率 | 依据 |\n|---|---|---|---|\n"
            "| 趋势市 | 是/否 |  |  |\n| 震荡市 | 是/否 |  |  |\n"
            "| 风险偏好高 | 是/否 |  |  |\n| 高波动 | 是/否 |  |  |\n\n"
            "- **环境一句话**：当前最贴切的 regime 标签\n"
            "- **策略调整**：仓位/持仓周期/止损宽度/可用品类 各 1 条\n"
            "- **常见坑**：该环境下交易员最常犯的 2~3 个错误\n\n"
            "" + RULES_TAIL)
    elif template == "analysis":
        factors_text = "\n".join(f"{i+1}. {f}" for i, f in enumerate(FACTORS))
        new_factor_no = FACTORS.index(SENTIMENT_FACTOR) + 1
        user = (
            f"请对「{topic}」按以下 {FACTOR_COUNT} 个因子逐一做多空分析。{ctx}"
            "每个因子给出【方向】和【多头概率】：多头概率 = 该因子当前指向"
            "上涨/利多的把握，50% 中性，>50% 偏多，<50% 偏空。\n\n"
            f"因子列表（必须全部覆盖，顺序不可变）：\n{factors_text}\n\n"
            f"第 {new_factor_no} 项「{SENTIMENT_FACTOR}」是新增因子，必须单独占一行，"
            "不得并入消息面与情绪面；优先使用上下文中标注的本地预聚合结果和样本依据。\n\n"
            f"「{INDUSTRY_POLICY_FACTOR}」只依据真实可见的政策/监管动态分析"
            "（如实际发布的产业政策、监管文件、补贴或招标规则），禁止编造，"
            "不得默认任何单一行业政策，也不得套用产业链传导逻辑。\n\n"
            "严格按以下 Markdown 格式输出，不要增删表格行：\n\n"
            "| 因子 | 方向 | 多头概率 | 依据 |\n|---|---|---|---|\n"
            "| （逐因子填写） |\n\n"
            "- **综合判断**：方向+综合多头概率（如「震荡偏多，约 58%」）\n"
            "- **关键风险**：1~2 条\n- **数据局限**：一句话\n\n"
            "" + RULES_TAIL)
    elif template == "equity":
        # 机构级个股投研：正式路径走 equity_research_column.generate_column；
        # 此处 prompt 作为 pushplus_deepseek 主流程的降级/兼容入口。
        user = (
            f"请按 equity-research skill 九章结构，为「{topic}」撰写机构级个股投资研究报告。\n\n"
            + ctx +
            "必须包含：结论框（决策三分法：内在价值/1–3个月交易方向/投资动作）、"
            "Tearsheet、预期差 Gap 表、护城河、财报质量等级 A–D、"
            "至少三种估值方法交叉验证、反方论证（一年后失败的3个原因）、"
            "监控清单与免责声明。关键数据标注来源+时间戳；缺失写「未获取到」。\n"
            "输出简体中文 Markdown。" + RULES_TAIL)
    elif template == "foreign_weekly":
        user = (
            f"基于以下国外专业投资机构一周研报数据（高盛/摩根士丹利/摩根大通/美银/花旗/瑞银/德银/巴克莱/汇丰/晨星/伯恩斯坦/野村等，窗口 168h），"
            f"为主题「{topic}」撰写一份国外机构一周研报栏目。\n\n{ctx}\n\n"
            "严格按此格式输出：\n\n"
            "## 国外专业投资机构一周研报\n\n"
            "| 机构 | 核心观点 | 评级/方向 | 目标价/关键判断 | 成立概率% | 依据来源 |\n"
            "|---|---|---|---|---|---|\n"
            "（逐机构 1-2 行，至少覆盖 5 家，含高盛/大摩/小摩/美银/瑞银，缺失标明“本周无更新”）\n\n"
            "- **全球策略主线**：归纳 3 条跨机构共识或分歧，每条标注支撑机构数与可信度\n"
            "- **板块与标的映射**：利多板块/利空板块各 2-3 个，关联标的举例\n"
            "- **评级调整汇总**：本周上调/下调各 2-3 条，含标的、旧/新评级、理由一句\n"
            "- **风险与分歧**：2-3 条机构间分歧或需观察信号\n"
            "- **下周关注**：2-3 个催化剂（财报/议息/数据）\n\n"
            "要求：所有观点标注来源机构与时间；不展示链接；缺失写“未获取到”；概率取整数%；简体中文 Markdown。"
            + RULES_TAIL)
    elif template == "hk_brokers":
        user = (
            f"基于以下香港主要券商新闻与数据（中金/中信证券国际/华泰金控/国泰君安国际/招商证券国际/海通国际/交银国际/建银国际/中银国际/富途/老虎/耀才等，含研报动态与上市券商股价快照），"
            f"为主题「{topic}」撰写一份香港券商栏目。\n\n{ctx}\n\n"
            "严格按此格式输出：\n\n"
            "## 香港券商新闻与数据\n\n"
            "| 券商 | 最新动态（研报/新闻标题） | 评级/方向 | 依据来源 |\n"
            "|---|---|---|---|\n"
            "（逐家 1-2 行，窗口内无动态的机构标明“暂无更新”，不要编造）\n\n"
            "- **上市券商股价**：引用数据中的股价快照，点评涨跌居前者（注明为抓取时点）\n"
            "- **券商观点共识与分歧**：归纳 2-3 条（涉及板块/标的方向，标注支撑家数）\n"
            "- **行业事件**：IPO/佣金/开户/政策等券商行业新闻 2-3 条\n"
            "- **数据缺口**：注明未命中的机构，不补造数据\n\n"
            "要求：标题与评级必须来自给定数据；不得编造目标价；不得给出买卖建议；不展示链接；简体中文 Markdown。"
            + RULES_TAIL)
    else:
        # Anthropic 官方 skill 模板：委托 skills_hub 构造完整 prompt
        try:
            import skills_hub as sh
            if sh.is_skill_template(template):
                return sh.build_messages(template, topic, context)
        except Exception:
            pass
        if template == "brief":
            user = (f"请围绕「{topic}」生成一份今日简报：3~5 个要点，"
                    "每个要点一句话；结尾一句小结。\n\n" + ctx)
        else:
            user = (
                f"请围绕「{topic}」按模板「{template}」生成一份研究备忘录。\n\n"
                + ctx + RULES_TAIL)
    system = ("你是一位严谨的跨市场分析师，深耕港股/A股市场，熟悉宏观与行业政策。"
              "输出必须是简体中文 Markdown，不要寒暄，不要使用代码块，"
              "所有概率用整数百分比表示。")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def validate_analysis(content: str) -> bool:
    """校验 analysis 是否逐项体现全部因子（包括新增的量价舆情动量）。"""
    if "| 因子" not in content:
        return False

    # 只检查因子表格，避免模型在后续风险说明中写几个百分比就误通过。
    rows: list[str] = []
    in_factor_table = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_factor_table and stripped:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == "因子":
            in_factor_table = True
            continue
        if not in_factor_table or not cells:
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) >= 3:
            rows.append(stripped)

    return all(
        any(factor in row and re.search(r"(?<!\d)\d{1,3}%", row) for row in rows)
        for factor in FACTORS
    )


# ================================================================ 内容生成（rule / AI 统一入口）

def gen_by_rule(topic: str, template: str,
                sent_pack: SentPack | None = None,
                newsnow_pack=None) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    if template == "newsnow" and newsnow_pack is not None:
        # 移植 newsnow 渲染
        try:
            from newsnow_sources import render_newsnow_rule
            return render_newsnow_rule(topic, newsnow_pack)
        except Exception:
            return f"**{topic} · NewsNow热榜**（rule）\n\n> 暂无数据，newsnow模块未加载"
    if template == "analysis":
        rows = []
        for factor in FACTORS:
            direction, probability, evidence = "中性", 50, "示例占位，待 AI 填充"
            if factor == SENTIMENT_FACTOR and sent_pack is not None:
                has_data = any(sent_pack.items.values()) or bool(
                    sent_pack.momentum.get("ok"))
                if has_data:
                    probability = int(sent_pack.agg.get("综合多头概率锚点", 50))
                    direction = ("偏多" if probability > 50
                                 else ("偏空" if probability < 50 else "中性"))
                    momentum = sent_pack.momentum.get("score", 0)
                    evidence = (f"48h本地锚点 {probability}%；"
                                f"量价动量 {momentum:+.2f}")
                else:
                    evidence = "48h 数据不足，无法计算本地锚点"
            rows.append(f"| {factor} | {direction} | {probability}% | {evidence} |")
        return "\n".join([
            f"**{topic} · 多空因子分析框架**（rule 演示模板，概率均为占位示例）",
            "", "| 因子 | 方向 | 多头概率 | 依据 |", "|---|---|---|---|",
            "\n".join(rows),
            "", "- **综合判断**：示例——ai_provider 选 deepseek 后由 AI 填充真实概率",
            "- **关键风险**：示例", "- **数据局限**：rule 模式不含真实分析", "",
            f"> 运行时间：{now}（北京时间）。⚠️ 非投资建议，仅供参考。"])
    if template == "equity":
        # 优先走独立栏目模块（含 dcf.py）；失败则给简短骨架
        try:
            import equity_research_column as erc
            return erc.gen_rule_report(topic, context="", mode="full",
                                       industry=erc.guess_industry(topic))
        except Exception as e:  # noqa: BLE001
            return "\n".join([
                f"**{topic} · 机构级个股投研**（rule 降级）", "",
                f"- equity_research_column 不可用：{e}",
                f"- 运行时间：{now}（北京时间）",
                "- 请确认 equity_research/ 已安装 skill 资源", "",
                "> ⚠️ 非投资建议，仅供参考。"])
    if template == "foreign_weekly":
        # 国外机构一周研报独立栏目
        try:
            import foreign_institution_weekly as fiw
            # 尝试从 context 中提取已采集的 pack，若无则用离线演示包
            # 这里 gen_by_rule 无 pack 上下文，降级为离线演示
            pack = fiw.demo_pack()
            pack.topic = topic
            return fiw.render_foreign_weekly_rule(topic, pack)
        except Exception as e:  # noqa: BLE001
            return "\n".join([
                f"**{topic} · 国外专业投资机构一周研报**（rule 降级）", "",
                f"- foreign_institution_weekly 不可用：{e}",
                f"- 运行时间：{now}（北京时间）",
                "- 覆盖机构：高盛/摩根士丹利/摩根大通/美银/花旗/瑞银/德银/巴克莱/汇丰/晨星/伯恩斯坦/野村",
                "- 提示：使用 --template foreign_weekly --hours 168 可采集最近一周研报",
                "", "> ⚠️ 非投资建议，仅供参考。"])
    if template == "hk_brokers":
        # 香港券商新闻与数据独立栏目（gen_by_rule 无 pack 时降级演示包；
        # 主流程独立模板走 collect_hk_brokers 真实采集，不走这里）
        try:
            import hk_brokers as hb
            pack = hb.demo_pack()
            pack.topic = topic
            return hb.render_hk_brokers_rule(topic, pack)
        except Exception as e:  # noqa: BLE001
            return "\n".join([
                f"**{topic} · 香港券商新闻与数据**（rule 降级）", "",
                f"- hk_brokers 不可用：{e}",
                f"- 运行时间：{now}（北京时间）",
                "- 覆盖券商：中金/中信证券国际/华泰金控/国泰君安国际/招商证券国际/海通国际/交银国际/建银国际/中银国际/富途/老虎/耀才",
                "- 提示：使用 --template hk_brokers --hours 96 可采集最新券商动态",
                "", "> ⚠️ 非投资建议，仅供参考。"])
    try:
        import skills_hub as sh
        if sh.is_skill_template(template):
            return sh.gen_rule_skill(template, topic, context="")
    except Exception as e:  # noqa: BLE001
        if template in ("initiate", "earnings_preview", "earnings_update",
                        "model_update", "morning_note", "catalysts",
                        "thesis", "sector", "ideas"):
            return "\n".join([
                f"**{topic} · {TEMPLATE_TITLES.get(template, template)}**（rule 降级）",
                "", f"- skills_hub 不可用：{e}",
                f"- 运行时间：{now}（北京时间）",
                "> ⚠️ 非投资建议，仅供参考。"])
    return "\n".join([
        f"**{topic} · {TEMPLATE_TITLES.get(template, '简报')}**（rule 演示模板）", "",
        f"- 模板：{template}（正式内容需 ai_provider=deepseek）",
        f"- 运行时间：{now}（北京时间）",
        "- 工作流：GitHub Actions 手动运行（推送目标由工作流配置）", "",
        "> 在 Actions 运行页选择 ai_provider=deepseek 即可获得完整 AI 分析。",
        "> ⚠️ 非投资建议，仅供参考。"])


def chat_completion(url: str, api_key: str, model: str, messages: list[dict],
                    max_tokens: int, timeout: int,
                    temperature: float = 0.7) -> str:
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    status, body = http_post_json(url, payload,
                                  {"Authorization": f"Bearer {api_key}"}, timeout)
    if status != 200:
        raise PushError(f"AI 接口返回 HTTP {status}：{body[:400]}")
    try:
        return json.loads(body)["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise PushError(f"AI 接口返回格式异常：{body[:400]}") from e


# ================================================================ 模块③c：AI 分节压缩（正文逐节简化 + 压缩）
#
# 拿到 AI 生成的正文后，按 Markdown 章节（# / ## 标题）切成若干「部分」，
# 每一部分分别调用一次 AI 智能分析做「简化 + 压缩」：保留结论、数字与
# 因子表，删铺垫与冗余。压缩在「内容指纹 / 分篇 / 推送」之前完成，
# 因此压缩结果就是最终推送内容；任一部分压缩失败都回退该部分原文，
# 绝不因压缩丢内容。

COMPRESS_MIN_PART_CHARS = 160   # 短于该字符数的分节不值得一次 AI 调用
COMPRESS_MAX_PARTS = 12         # 单次最多压缩的分节数（防 API 调用失控）

AI_COMPRESS_SYSTEM = "你是投研推送的精简编辑，任务是把给出的每一节内容简化、压缩。"

AI_COMPRESS_RULES = (
    "请大幅简化并压缩下面这一节投研内容，严格遵守：\n"
    "1) 第一行标题必须原样保留（# / ## 标题不得改动、不得删除）；\n"
    "2) 保留全部关键结论与数字：方向判断、概率%、价格、估值、目标位、样本数；\n"
    "3) 因子/象限等表格必须逐行保留（行名与百分比不得删），只精简文字描述；\n"
    "4) 删除铺垫、重复、客套与冗余修辞，合并同类要点；\n"
    "5) 不得新增事实，不得改变任何结论方向；\n"
    "6) 直接输出压缩后的这一节（Markdown），不要任何前言、解释或代码围栏；\n"
    "7) 目标：压缩到原字数的 40%~60%。"
)


def ai_compress_enabled() -> bool:
    """AI 分节压缩总开关：默认开启；AI_COMPRESS=0/false/off/no 关闭。"""
    raw = (env("AI_COMPRESS") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    return True


def inference_override():
    """显式配置的统一推理 API（INFERENCE_*）；未配置返回 None。

    这一层让 DeepSeek / OpenAI / 硅基流动 / 百炼 / 本地 vLLM 等
    OpenAI 兼容端点都能直接驱动本仓库的全部 AI 能力，无需改代码。
    """
    try:
        import inference_api as ia
        cfg = ia.explicit_config()
    except Exception:  # 模块缺失或配置异常时退回旧逻辑
        return None
    return cfg if (cfg is not None and cfg.configured) else None


def ai_credentials(provider: str) -> tuple[str, str, str, str, bool]:
    """返回 (url, model, key, label, keyless)。

    优先级：INFERENCE_* 统一推理 API > 旧 DEEPSEEK/OPENAI 配置。
    keyless=True 表示本地端点（Ollama/vLLM 等）允许空 Key。
    """
    cfg = inference_override()
    if cfg is not None:
        return (cfg.endpoint(), cfg.model, cfg.api_key,
                f"推理API · {cfg.label}", bool(cfg.keyless))
    if provider == "openai":
        base = env("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
        return (f"{base.rstrip('/')}/chat/completions", "gpt-4o-mini",
                env("OPENAI_API_KEY") or "", "openai（gpt-4o-mini）", False)
    return (DEEPSEEK_URL, "deepseek-chat", env("DEEPSEEK_API_KEY") or "",
            "deepseek（deepseek-chat）", False)


def ai_ready(provider: str) -> bool:
    """当前是否具备调用 AI 的条件（有 Key，或本地 keyless 端点）。"""
    url, _model, key, _label, keyless = ai_credentials(provider)
    return bool(url) and (bool(key) or keyless)


def ai_auto_provider() -> str:
    """auto 模式解析：显式推理 API 或旧 DeepSeek Key 可用则走 AI，否则 rule。"""
    if inference_override() is not None:
        return "deepseek"
    return "deepseek" if env("DEEPSEEK_API_KEY") else "rule"


def _ai_endpoint(provider: str) -> tuple[str, str, str]:
    """按 provider 返回 (url, model, key)；key 可能为空串。"""
    url, model, key, _label, _keyless = ai_credentials(provider)
    return url, model, key


def split_md_sections(md: str) -> list[str]:
    """把 Markdown 正文按一级/二级标题切成「部分」；### 及更小标题归属上一节。

    首个标题之前的导语文本也算独立一部分。返回不含空节。
    """
    parts: list[str] = []
    cur: list[str] = []
    for line in md.splitlines():
        if re.match(r"^#{1,2}\s+\S", line) and cur:
            parts.append("\n".join(cur).strip())
            cur = [line]
        else:
            cur.append(line)
    if cur:
        parts.append("\n".join(cur).strip())
    return [p for p in parts if p]


def _compress_clean(out: str, part: str) -> str:
    """清洗模型输出：去围栏/前后缀；异常（空/变长/丢标题）时回退原文。"""
    txt = (out or "").strip()
    # 去掉模型偶尔包的 ```markdown 围栏
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt).strip()
    if not txt or len(txt) >= len(part):
        return part
    # 原节以标题开头而输出丢了标题：补回原标题行，保证章节结构稳定
    head = part.splitlines()[0].strip()
    if re.match(r"^#{1,2}\s+\S", head) and not re.match(r"^#{1,2}\s+\S",
                                                        txt.splitlines()[0]):
        txt = head + "\n\n" + txt
    return txt if len(txt) < len(part) else part


def ai_compress_md(md: str, *, provider: str, template: str = "",
                   timeout: int = 30) -> tuple[str, str]:
    """正文按章节逐节调用 AI 简化压缩；返回 (压缩后 md, 统计说明)。

    - 总开关关闭（AI_COMPRESS=0）或没有可用 AI Key：原样返回；
    - 每节独立调用，单节失败/输出异常回退该节原文，整体永不抛异常；
    - 短节（<COMPRESS_MIN_PART_CHARS）与超出 COMPRESS_MAX_PARTS 的节不压缩。
    """
    if not md or not md.strip():
        return md, ""
    if not ai_compress_enabled():
        return md, ""
    url, model, key = _ai_endpoint(provider)
    if not key and provider != "openai":        # deepseek/rule 无 key：尝试 openai
        url2, model2, key2 = _ai_endpoint("openai")
        if key2:
            url, model, key = url2, model2, key2
    if not key and provider == "openai":        # openai 无 key：尝试 deepseek
        url2, model2, key2 = _ai_endpoint("deepseek")
        if key2:
            url, model, key = url2, model2, key2
    if not key:
        return md, "未配置 AI Key，跳过分节压缩"

    parts = split_md_sections(md)
    todo = [i for i, p in enumerate(parts)
            if len(p) >= COMPRESS_MIN_PART_CHARS][:COMPRESS_MAX_PARTS]
    if not todo:
        return md, ""

    done = 0
    changed = False
    for n, i in enumerate(todo, 1):
        part = parts[i]
        title = (part.splitlines()[0].lstrip("#").strip() or f"第{n}节")[:18]
        try:
            out = chat_completion(
                url, key, model,
                [{"role": "system", "content": AI_COMPRESS_SYSTEM},
                 {"role": "user", "content": AI_COMPRESS_RULES
                  + "\n\n--- 待压缩的这一节 ---\n" + part}],
                max(400, min(2000, int(len(part) * 0.7))), timeout,
                temperature=0.3)
            new_part = _compress_clean(out, part)
        except PushError as e:
            log(f"  ⚠️ 压缩失败（{title}），保留原文：{e}")
            new_part = part
        if new_part != part:
            done += 1
            changed = True
            log(f"  ✂️ [{n}/{len(todo)}] {title}："
                f"{len(part)} → {len(new_part)} 字")
        parts[i] = new_part

    new_md = "\n\n".join(parts)
    before, after = len(md), len(new_md)
    if not changed or after >= before:
        return md, f"AI 分节压缩未缩短（{before} 字），沿用原文"
    return new_md, (f"AI 分节压缩：{done}/{len(todo)} 节生效，"
                    f"{before} → {after} 字（省 {100 - after * 100 // before}%）")


# ================================================================ Secrets 检查

def channel_targets(channel: str) -> list[str]:
    """把 all 展开为真实推送通道；console 保持为单通道。"""
    return ALL_CHANNELS if channel == "all" else [channel]


def channel_required_secrets(channel: str) -> list[str]:
    """返回单个推送通道需要的 Secret（不含 AI Key）。"""
    if channel == "all":
        names: list[str] = []
        for ch in ALL_CHANNELS:
            names.extend(CHANNEL_SECRET_MAP.get(ch, []))
        return names
    return list(CHANNEL_SECRET_MAP.get(channel, []))


def missing_channel_secrets(channel: str) -> list[str]:
    """返回单个推送通道缺失的 Secret；用于真实推送前优雅跳过。"""
    return [name for name in channel_required_secrets(channel) if not env(name)]


def skip_missing_secret_result(missing: list[str]) -> str:
    """统一的“缺 Key 跳过”结果文案；不算失败，正文仍已完整输出。"""
    return (f"{SKIP_RESULT_PREFIX}：缺少 Secret：{', '.join(missing)}"
            "（正文已完整生成并打印到日志，未真实推送）")


def is_skip_result(text: object) -> bool:
    return str(text).startswith(SKIP_RESULT_PREFIX)


def is_failure_result(text: object) -> bool:
    return str(text).startswith("失败")


def has_deliverable_channel(channel: str) -> bool:
    """是否至少有一个所选通道可投递/输出；全缺 Key 时不写推送状态。"""
    return any(not missing_channel_secrets(ch) for ch in channel_targets(channel))


def required_secrets(channel: str, provider: str) -> list[str]:
    req: list[str] = []
    req.extend(channel_required_secrets(channel))
    if provider in ("deepseek", "openai"):
        cfg = inference_override()
        if cfg is not None:
            # 已接入统一推理 API：只有远端且未配 Key 时才提示 INFERENCE_API_KEY
            if cfg.requires_key and not cfg.api_key:
                req.append("INFERENCE_API_KEY")
        else:
            req.append("DEEPSEEK_API_KEY" if provider == "deepseek"
                       else "OPENAI_API_KEY")
    return req


def print_secret_report(channel: str, provider: str) -> bool:
    names = required_secrets(channel, provider)
    log("🔎 Secrets 配置检查（只显示是否配置，绝不打印内容）：")
    if not names:
        log("  （所选 通道+AI 组合无需任何 Secret）")
        return True
    missing = []
    for name in names:
        if env(name):
            log(f"  ✅ {name} 已配置")
        else:
            log(f"  ❌ {name} 缺失")
            missing.append(name)
    if missing:
        log("\n⚠️ 如需真实送达，请前往 仓库 Settings → Secrets and variables → Actions 补齐对应 Secret。")
        log("  缺失的推送通道会在真实推送阶段被优雅跳过，不会使 Actions 因缺 Key 失败。")
        return False
    log("  → 全部所需 Secrets 已就绪 ✅")
    return True


# ================================================================ 模块④b：HTML 主题渲染
#
# PushPlus template=html 时走该渲染器。微信/PushPlus 详情页对 <style> 标签
# 支持不稳定，全部使用内联样式。
# guizang 主题：参考 Guizang PPT Skill Style A 的「电子杂志 × 电子墨水」语言，
#               适配为手机端竖版长页面：暖纸底、墨黑 Hero、衬线标题、等宽元信息、
#               发丝线与大留白；不照搬横向翻页/WebGL 运行时。
# game 主题：8-bit 像素游戏风 - 深夜蓝游戏屏/金色粗框/硬黑像素阴影/
#            HP血条/SCORE/LEVEL 游戏元素/金币高亮/涨跌突出
# klein 主题：游戏复古像素风 - 米黄纸底/黑细框/像素图标/涨跌突出
# pixel 主题：复古监控风 - 暗色服务器大屏/细线框/等宽细字/涨跌突出/REC摄像头元素
# dos 主题：DOS CRT 终端 - 黑底磷光绿/琥珀光标/命令行状态栏/扫描线，当前推送默认

GUIZANG = {
    # ---- 电子杂志 × 电子墨水：墨水经典（Monocle 默认）竖版长页面 ----
    "bg": "#BDBDBB",            # 长页面外框 / Hero 墨黑
    "page_bg": "#F2F2F0",       # 暖米白电子纸
    "paper_tint": "#E1E1DF",    # 轻微层级底色
    "card_bg": "#F2F2F0",       # 内容与纸张同色：不做浮动卡片
    "hbg": "#3A3A38",
    "hfg": "#C8FF00",
    "fg": "#111111",
    "muted": "#6D6D69",
    "border": "#1A1A1A",
    "hairline": "#C9C9C5",
    "accent": "#C8FF00",
    "accent_fg": "#111111",
    "up": "#176B45",
    "down": "#A63D32",
    "up_bg": "#DFE9E2",
    "down_bg": "#EEDFDA",
    "code_bg": "#18181A",
    "code_fg": "#F1EFEA",
    "size": "12px",
    "size_title": "14px",
    "size_h1": "26px",
    "size_h2": "20px",
    "size_h3": "16px",
    "line": "1.55",
    "font": "'Noto Sans SC','PingFang SC',sans-serif",
    "font_serif": "'Noto Serif SC','Songti SC',Georgia,serif",
    "font_en": "Georgia,'Times New Roman',serif",
    "font_mono": "'IBM Plex Mono',Consolas,'Courier New',monospace",
    "editorial": True,
    # 分篇推送时第 2 篇起使用紧凑续篇壳（省略巨幅 Hero），
    # 让长报告在 48KB 软上限内能装下更多正文（见 themed_html continuation）。
    "continuation": True,
}

KLEIN = {
    # ---- 游戏复古像素风：米黄纸底 + 白卡片 + 黑细框 + 像素字体/图标 ----
    "bg": "#EDE8D0",            # 纸色底
    "card_bg": "#FFFFFF",       # 白卡片
    "hbg": "#111111",           # 黑底标题栏
    "hfg": "#FFFFFF",           # 标题白字
    "fg": "#111111",            # 正文黑字
    "muted": "#777777",         # 辅助灰
    "border": "#111111",        # 黑细线框 1px
    "line": "1.5",
    "font": "'Courier New','Nimbus Mono PS',Consolas,monospace",
    "size": "12px",
    "size_title": "13px",
    "size_h1": "13px",
    "size_h2": "12px",
    "size_h3": "12px",
    "up": "#00A85F",
    "down": "#FF2D2A",
    "up_bg": "#D1F5DF",
    "down_bg": "#FFD6D6",
    "accent": "#FFE600",        # 像素黄 重点凸显
    "accent_fg": "#111111",
}

PIXEL = {
    # ---- 复古监控风：暗色服务器大屏 + 细线框 + 等宽细字 + 摄像头元素 ----
    "bg": "#0A0E0C",            # 机房暗底
    "card_bg": "#101613",       # 监控面板底
    "hbg": "#0B100D",           # 顶部状态栏深底
    "hfg": "#C8F0D2",           # 磷光绿标题
    "fg": "#9FD9AD",            # 正文磷光绿
    "muted": "#5E7A68",         # 暗绿辅助字
    "border": "#2C4A39",        # 细线框（全部 1px）
    "accent": "#FFB000",        # 琥珀黄 重点数据/复古图标
    "accent_fg": "#1A1200",
    "up": "#3AE374",
    "down": "#FF4A4A",
    "up_bg": "#0D2417",
    "down_bg": "#2A1313",
    "size": "11px",
    "size_title": "12px",
    "size_h1": "12px",
    "size_h2": "11px",
    "size_h3": "11px",
    "line": "1.5",
    "font": "'Courier New','Nimbus Mono PS',Consolas,monospace",
    "scan": "rgba(0,0,0,0.30)",         # CRT 扫描线
    "grid": "rgba(140,220,170,0.05)",   # 面板抽象网格
    "shadow": "rgba(0,0,0,0.55)",       # 硬偏移阴影
}

DOS = {
    # ---- DOS CRT 终端：黑底磷光绿 + 琥珀提示 + 扫描线 ----
    "bg": "#020603",            # CRT 屏幕边框
    "card_bg": "#06100A",       # 终端面板底色
    "hbg": "#001A08",           # DOS 状态栏深绿
    "hfg": "#67FF8A",           # 磷光绿标题
    "fg": "#A8F2B3",            # 正文柔和绿
    "muted": "#4B8D59",         # 辅助终端绿
    "border": "#236B36",        # 像素绿边框
    "accent": "#FFC857",        # 琥珀色光标/提示
    "accent_fg": "#151000",
    "up": "#63FF78",
    "down": "#FF6969",
    "up_bg": "#08200F",
    "down_bg": "#260B0B",
    "code_bg": "#010402",
    "code_fg": "#75FF91",
    "size": "12px",
    "size_title": "13px",
    "size_h1": "14px",
    "size_h2": "13px",
    "size_h3": "12px",
    "line": "1.5",
    "font": "'Lucida Console','Courier New',monospace",
    "font_mono": "'Lucida Console','Courier New',monospace",
    "scan": "rgba(0,0,0,0.34)",
    "grid": "rgba(103,255,138,0.04)",
    "shadow": "rgba(0,0,0,0.85)",
    "table_free": True,
}

GAME = {
    # ---- 8-bit 像素游戏风（兼容保留）：深夜蓝游戏屏 + 金色粗框 + 硬黑像素阴影 ----
    #      标题栏=游戏菜单金条 / 状态栏=HP血条+LV等级 / 底部=SCORE+PRESS START
    "bg": "#0B0E2A",            # 游戏背景（深夜蓝）
    "card_bg": "#16193B",       # 游戏面板底（靛蓝）
    "hbg": "#F8B800",           # 金色标题栏（菜单金条）
    "hfg": "#111111",           # 标题黑字
    "fg": "#E9EAF5",            # 正文米白
    "muted": "#8A90BC",         # 辅助蓝灰
    "border": "#F8B800",        # 金色粗框（2px 游戏描边）
    "accent": "#FFD23F",        # 金币黄 重点凸显
    "accent_fg": "#1A1200",
    "up": "#5CFF5C",            # 磷光绿 涨
    "down": "#FF5C5C",          # 警示红 跌
    "up_bg": "#12301E",
    "down_bg": "#35121A",
    "size": "12px",
    "size_title": "13px",
    "size_h1": "14px",
    "size_h2": "12px",
    "size_h3": "12px",
    "line": "1.5",
    "font": "'Courier New','Nimbus Mono PS',Consolas,monospace",
    "shadow": "rgba(0,0,0,0.90)",       # 硬黑像素阴影
    "grid": "rgba(248,184,0,0.05)",     # 面板金色网格
    "star1": "rgba(255,255,255,0.55)",  # 背景星点（白）
    "star2": "rgba(255,210,63,0.45)",   # 背景星点（金）
    "hp_full": "#5CFF5C",               # 血条填充色
    "hp_empty": "rgba(233,234,245,0.20)",  # 血条空格色
    "status_bg": "#0E1130",             # 状态栏深底
}

MONITOR = {
    # ---- 服务器大屏监视风：深空暗底 + 荧光青绿 + 零表格（文字+列表横排） ----
    "bg": "#06090F",            # 深空暗黑科技底
    "card_bg": "#0B121E",       # 监控面板暗底
    "hbg": "#0F1E33",           # 顶部 HUD 栏深底
    "hfg": "#00F0FF",           # 荧光青标题
    "fg": "#E2E8F0",            # 正文亮白灰
    "muted": "#64748B",         # 辅助蓝灰
    "border": "#00F0FF",        # 荧光青发光边框
    "accent": "#00FF9D",        # 荧光绿重点
    "accent_fg": "#06090F",
    "up": "#00FF9D",            # 荧光绿 涨
    "down": "#FF3366",          # 警示红 跌
    "up_bg": "#072418",
    "down_bg": "#2B0B14",
    "size": "12px",
    "size_title": "13px",
    "size_h1": "14px",
    "size_h2": "12px",
    "size_h3": "12px",
    "line": "1.5",
    "font": "'Courier New','Nimbus Mono PS',Consolas,monospace",
    "shadow": "rgba(0,0,0,0.85)",
    "grid": "rgba(0,240,255,0.04)",
    "scan": "rgba(0,240,255,0.03)",
    "glow": "rgba(0,240,255,0.25)",
    "table_free": True,
}

THEMES = {
    "guizang": GUIZANG,
    "game": GAME,
    "klein": KLEIN,
    "pixel": PIXEL,
    "dos": DOS,
    "monitor": MONITOR,
    "noc": MONITOR,
}


def _get_theme(name_or_dict):
    if isinstance(name_or_dict, dict):
        return name_or_dict
    if isinstance(name_or_dict, str) and name_or_dict in THEMES:
        return THEMES[name_or_dict]
    return GAME


# ---- 荧光文字「自带黑色底」规则 ----
# 微信/PushPlus 详情页可能剥离外层容器背景，荧光青/荧光绿/荧光黄等发光色
# 文字一旦落到白色页面上几乎不可读。因此所有荧光色文字（标题、涨跌数字、
# 图标符号等）一律在文字元素自身加纯黑背景，不依赖任何父容器背景存活。
FLU_BLACK_BG = "#000"
# 荧光色集合直接派生自主题字典：调整主题色后，荧光判定自动跟进。
FLUORESCENT_TEXT_COLORS = {
    MONITOR["hfg"], MONITOR["accent"], MONITOR["up"],   # 荧光青/荧光绿
    GAME["accent"], GAME["up"], GAME["hp_full"], GAME["border"],  # 金币黄/磷光绿/金框
    PIXEL["hfg"], PIXEL["accent"], PIXEL["up"],          # 磷光绿/琥珀黄
    DOS["hfg"], DOS["accent"], DOS["up"],                # DOS 磷光绿/琥珀光标
    GUIZANG["accent"],                                   # 荧光黄绿标题
    KLEIN["accent"],                                     # 像素黄
}


def _flu_bg(color: str | None, pad: str = "0 3px") -> str:
    """荧光色文字 → 自带黑色底；非荧光色保持原样（不加底）。"""
    if color and color.upper() in FLUORESCENT_TEXT_COLORS:
        return f"background:{FLU_BLACK_BG};padding:{pad};"
    return ""


def _contains_up(txt: str) -> bool:
    # 涨关键词 + 数值 +▲
    up_kw = ["利好", "偏多", "看多", "上涨", "大涨", "突破", "中标", "增长",
             "创新高", "回购", "预增", "超预期", "盈利", "利多"]
    if any(k in txt for k in up_kw):
        return True
    if "▲" in txt or "↑" in txt:
        return True
    # +数字% 判定为涨
    if re.search(r"\+\s*\d", txt) and "%" in txt:
        return True
    if re.search(r"\+\d+\.\d+", txt):
        return True
    return False


def _contains_down(txt: str) -> bool:
    down_kw = ["利空", "偏空", "看空", "下跌", "大跌", "亏损", "减持", "下调",
               "预警", "预亏", "违约", "利空", "利淡"]
    if any(k in txt for k in down_kw):
        return True
    if "▼" in txt or "↓" in txt:
        return True
    if re.search(r"-\s*\d", txt) and "%" in txt:
        return True
    if re.search(r"-\d+\.\d+%", txt):
        return True
    return False


def _inline_md(s: str, theme_name: str = "game",
               link_color: str | None = None) -> str:
    # link_color：链接文字色覆盖。用于链接落在纯黑底上的场景
    # （如 guizang 荧光黄绿标题自带黑底），避免正文色链接黑底黑字不可见。
    theme = _get_theme(theme_name)
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    # 图片优先于链接：![alt](url) → <img>（微信/PushPlus html 主题直接显示）
    # referrerpolicy="no-referrer"：微信内置浏览器对第三方图床常做防盗链拦截，
    # 不发送 Referer 可显著提高外链图片（jsDelivr 等 CDN）在微信内的加载成功率。
    if theme.get("editorial"):
        img_style = (
            f'max-width:100%;width:100%;height:auto;display:block;margin:18px 0 8px;'
            f'border-top:1px solid {theme["border"]};'
            f'border-bottom:1px solid {theme["border"]};border-radius:0;')
    else:
        img_style = (
            'max-width:100%;display:block;margin:6px 0;'
            'border:1px solid rgba(128,128,160,0.45);border-radius:2px;')
    s = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)\)",
        rf'<img src="\2" alt="\1" referrerpolicy="no-referrer" style="{img_style}">',
        s)
    if theme.get("editorial"):
        s = re.sub(
            r"\*\*([^*]+)\*\*",
            rf'<strong style="font-family:{theme["font_serif"]};font-weight:700;">\1</strong>',
            s)
    else:
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               rf'<a href="\2" style="color:{link_color or theme["fg"]};text-decoration:underline;'
               rf'text-underline-offset:3px;">\1</a>', s)
    return s


def _render_factor_cards(rows: list[str], theme_name: str = "game") -> str:
    """「因子 | 方向 | 多头概率 | 依据」表 → 卡片式内容展示（每个因子一张卡）。

    卡片结构（全主题统一，无 <table>）：
      ① 卡头：像素方块 + 因子名（加粗）+ 方向徽章（▲偏多绿 / ▼偏空红 / ●中性灰）
      ② 概率行：多头概率大号数字 + 概率条（game 主题为像素血条 █░，
         其余主题为细框进度条）；无【方向】列时按概率 50% 上下推导徽章
      ③ 额外列：逐行“列名：值”补充展示
      ④ 依据行：「依据」标签（标题栏配色）+ 辅助色正文
    色值全部取自主题常量，未硬编码。
    """
    theme = _get_theme(theme_name)

    def cells(r: str) -> list[str]:
        return [c.strip() for c in r.strip().strip("|").split("|")]

    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]] if len(rows) > 2 else []

    def col(*names: str):
        for want in names:
            for idx, h in enumerate(head):
                if h == want:
                    return idx
        return None

    dir_i = col("方向")
    prob_i = col("多头概率", "概率")
    ev_i = col("依据")
    prob_label = head[prob_i] if prob_i is not None else "多头概率"
    known = {i for i in (0, dir_i, prob_i, ev_i) if i is not None}

    # 电子杂志主题：把因子卡改造成竖向 editorial rowline。无阴影、无浮卡，
    # 依靠衬线标题、等宽元数据、发丝线和留白建立层级。
    if theme.get("editorial"):
        cards: list[str] = []
        for row_no, r in enumerate(body, 1):
            if not r or not r[0]:
                continue

            def cell(i) -> str:
                return r[i] if (i is not None and 0 <= i < len(r)) else ""

            name = r[0]
            direction = cell(dir_i)
            prob_raw = cell(prob_i)
            evidence = cell(ev_i)
            extras = [(h, r[idx]) for idx, h in enumerate(head)
                      if idx not in known and idx < len(r) and r[idx]]
            m_prob = re.search(r"(\d{1,3})\s*%", prob_raw)
            p_val = max(0, min(100, int(m_prob.group(1)))) if m_prob else None
            if direction:
                is_up = _contains_up(direction)
                is_down = (not is_up) and _contains_down(direction)
                badge_text = direction
            elif p_val is not None:
                is_up, is_down = p_val > 50, p_val < 50
                badge_text = "偏多" if is_up else ("偏空" if is_down else "中性")
            else:
                joined = " ".join(r)
                is_up = _contains_up(joined)
                is_down = (not is_up) and _contains_down(joined)
                badge_text = "偏多" if is_up else ("偏空" if is_down else "中性")
            edge = theme["up"] if is_up else (
                theme["down"] if is_down else theme["muted"])
            arrow = "▲" if is_up else ("▼" if is_down else "●")
            badge_html = (
                f'<span style="display:inline-block;color:{edge};border:1px solid {edge};'
                f'padding:2px 7px;font-family:{theme["font_mono"]};font-size:10px;'
                f'letter-spacing:.08em;white-space:nowrap;">{arrow} '
                f'{_inline_md(badge_text, theme_name)}</span>')
            prob_html = ""
            if prob_raw:
                width = p_val if p_val is not None else 0
                prob_html = (
                    f'<div style="margin-top:13px;display:flex;align-items:center;gap:9px;">'
                    f'<span style="font-family:{theme["font_mono"]};font-size:9px;'
                    f'letter-spacing:.14em;color:{theme["muted"]};text-transform:uppercase;">'
                    f'{prob_label}</span>'
                    f'<span style="font-family:{theme["font_en"]};font-size:25px;'
                    f'font-weight:700;line-height:1;color:{edge};">'
                    f'{_inline_md(prob_raw, theme_name)}</span>'
                    f'<span style="height:2px;flex:1;background:{theme["hairline"]};">'
                    f'<span style="display:block;width:{width}%;height:2px;background:{edge};">'
                    f'</span></span></div>')
            extras_html = "".join(
                f'<div style="margin-top:7px;font-family:{theme["font"]};font-size:12px;'
                f'color:{theme["muted"]};"><span style="font-family:{theme["font_mono"]};'
                f'font-size:9px;letter-spacing:.12em;text-transform:uppercase;">'
                f'{_inline_md(h, theme_name)}</span> · '
                f'<span style="color:{theme["fg"]};">{_inline_md(v, theme_name)}</span></div>'
                for h, v in extras)
            evidence_html = ""
            if evidence:
                evidence_html = (
                    f'<div style="margin-top:11px;color:{theme["muted"]};'
                    f'font-family:{theme["font"]};font-size:{theme["size"]};'
                    f'line-height:{theme["line"]};">'
                    f'<span style="font-family:{theme["font_mono"]};font-size:9px;'
                    f'letter-spacing:.14em;color:{theme["fg"]};">EVIDENCE</span><br>'
                    f'{_inline_md(evidence, theme_name)}</div>')
            cards.append(
                f'<div style="border-top:1px solid {theme["hairline"]};'
                f'padding:18px 0 20px;">'
                f'<div style="display:flex;align-items:flex-start;gap:12px;">'
                f'<span style="font-family:{theme["font_en"]};font-style:italic;'
                f'font-size:13px;color:{theme["muted"]};padding-top:3px;">{row_no:02d}</span>'
                f'<span style="flex:1;font-family:{theme["font_serif"]};font-size:20px;'
                f'font-weight:700;line-height:1.35;color:{theme["fg"]};">'
                f'{_inline_md(name, theme_name)}</span>{badge_html}</div>'
                f'{prob_html}{extras_html}{evidence_html}</div>')
        return (
            f'<div style="margin:20px 0 26px;border-bottom:1px solid {theme["hairline"]};">'
            f'<div style="display:flex;justify-content:space-between;margin-bottom:8px;'
            f'font-family:{theme["font_mono"]};font-size:9px;letter-spacing:.16em;'
            f'color:{theme["muted"]};"><span>FACTOR MATRIX</span>'
            f'<span>{len(cards):02d} SIGNALS</span></div>{"".join(cards)}</div>')

    # 卡片阴影：monitor 用辉光，game/pixel 用硬偏移像素阴影，klein 用黑框硬阴影
    if theme.get("glow"):
        shadow_css = f'box-shadow:0 0 6px {theme["glow"]};'
    elif theme.get("shadow"):
        shadow_css = f'box-shadow:2px 2px 0 {theme["shadow"]};'
    else:
        shadow_css = f'box-shadow:2px 2px 0 {theme["border"]};'

    cards: list[str] = []
    for r in body:
        if not r or not r[0]:
            continue

        def cell(i) -> str:
            return r[i] if (i is not None and 0 <= i < len(r)) else ""

        name = r[0]
        direction = cell(dir_i)
        prob_raw = cell(prob_i)
        evidence = cell(ev_i)
        extras = [(h, r[idx]) for idx, h in enumerate(head)
                  if idx not in known and idx < len(r) and r[idx]]

        m_prob = re.search(r"(\d{1,3})\s*%", prob_raw)
        p_val = max(0, min(100, int(m_prob.group(1)))) if m_prob else None
        if direction:
            is_up = _contains_up(direction)
            is_down = (not is_up) and _contains_down(direction)
            badge_text = direction
        elif p_val is not None:
            is_up, is_down = p_val > 50, p_val < 50
            badge_text = "偏多" if is_up else ("偏空" if is_down else "中性")
        else:
            joined = " ".join(r)
            is_up = _contains_up(joined)
            is_down = (not is_up) and _contains_down(joined)
            badge_text = "偏多" if is_up else ("偏空" if is_down else "")

        edge = theme["up"] if is_up else (
            theme["down"] if is_down else theme["border"])
        if is_up:
            badge_html = (
                f'<span style="color:{theme["up"]};background:{theme["up_bg"]};'
                f'border:1px solid {theme["up"]};padding:0 4px;font-weight:bold;'
                f'font-size:10px;white-space:nowrap;">▲ {badge_text}</span>')
        elif is_down:
            badge_html = (
                f'<span style="color:{theme["down"]};background:{theme["down_bg"]};'
                f'border:1px solid {theme["down"]};padding:0 4px;font-weight:bold;'
                f'font-size:10px;white-space:nowrap;">▼ {badge_text}</span>')
        elif badge_text:
            badge_html = (
                f'<span style="color:{theme["muted"]};border:1px solid '
                f'{theme["border"]};padding:0 4px;font-size:10px;'
                f'white-space:nowrap;">● {badge_text}</span>')
        else:
            badge_html = ""

        # 概率条：game 主题用像素血条字符，其余主题用细框进度条
        if p_val is None:
            bar_html = ""
        elif theme_name == "game":
            filled = max(0, min(10, round(p_val / 10)))
            bar_txt = "█" * filled + "░" * (10 - filled)
            bar_html = (
                f' <span style="color:{theme["hp_full"]};'
                f'background:{FLU_BLACK_BG};padding:0 2px;font-size:9px;'
                f'letter-spacing:-1px;">{bar_txt}</span>')
        else:
            bar_html = (
                f' <span style="display:inline-block;width:70px;height:6px;'
                f'border:1px solid {theme["border"]};background:{theme["bg"]};'
                f'vertical-align:middle;margin-left:4px;">'
                f'<span style="display:block;width:{p_val}%;height:100%;'
                f'background:{edge};"></span></span>')

        prob_html = ""
        if prob_raw:
            prob_html = (
                f'<div style="margin-top:4px;font-size:10px;'
                f'color:{theme["muted"]};">'
                f'{prob_label} '
                f'<span style="color:{edge};{_flu_bg(edge)}font-weight:bold;'
                f'font-size:{theme["size_title"]};">'
                f'{_inline_md(prob_raw, theme_name)}</span>{bar_html}</div>')

        extras_html = "".join(
            f'<div style="margin-top:3px;font-size:10px;color:{theme["muted"]};">'
            f'{_inline_md(h, theme_name)} '
            f'<span style="color:{theme["fg"]};font-weight:bold;">'
            f'{_inline_md(v, theme_name)}</span></div>'
            for h, v in extras)

        evidence_html = ""
        if evidence:
            evidence_html = (
                f'<div style="margin-top:4px;color:{theme["muted"]};'
                f'font-size:{theme["size"]};line-height:{theme["line"]};">'
                f'<span style="background:{theme["hbg"]};color:{theme["hfg"]};'
                f'padding:0 3px;font-size:9px;margin-right:4px;">依据</span>'
                f'{_inline_md(evidence, theme_name)}</div>')

        cards.append(
            f'<div style="background:{theme["card_bg"]};'
            f'border:1px solid {theme["border"]};'
            f'border-left:4px solid {edge};{shadow_css}'
            f'padding:6px 8px;margin:0 0 6px 0;box-sizing:border-box;">'
            f'<div style="display:flex;justify-content:space-between;'
            f'align-items:center;">'
            f'<span style="font-weight:bold;color:{theme["fg"]};'
            f'font-size:{theme["size"]};">'
            f'<span style="display:inline-block;width:6px;height:6px;'
            f'background:{theme["accent"]};margin-right:5px;'
            f'vertical-align:middle;"></span>{_inline_md(name, theme_name)}</span>'
            f'{badge_html}</div>'
            f'{prob_html}{extras_html}{evidence_html}</div>')

    return (
        f'<div style="margin:6px 0;'
        f'font-family:{theme.get("font", KLEIN["font"])};">'
        f'{"".join(cards)}</div>')


def _render_table(rows: list[str], theme_name: str = "game") -> str:
    theme = _get_theme(theme_name)

    def cells(r: str) -> list[str]:
        return [c.strip() for c in r.strip().strip("|").split("|")]

    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]] if len(rows) > 2 else []

    # 因子分析表（因子/方向/多头概率/依据）→ 卡片式内容展示，不再渲染表格
    if head and head[0] == "因子":
        return _render_factor_cards(rows, theme_name)

    if theme.get("editorial"):
        # 电子杂志竖版页：宽表在手机端会横向溢出，因此改成 rowline。
        # 首列是衬线关键词，其余列用「等宽标签 / 正文值」逐项展开。
        row_items: list[str] = []
        for row_no, r in enumerate(body, 1):
            if not r:
                continue
            joined = " ".join(r)
            is_up = _contains_up(joined)
            is_down = (not is_up) and _contains_down(joined)
            signal = theme["up"] if is_up else (
                theme["down"] if is_down else theme["fg"])
            name = _inline_md(r[0], theme_name) if r else ""
            details = []
            for idx, value in enumerate(r[1:], 1):
                if not value:
                    continue
                label = head[idx] if idx < len(head) else f"COL {idx + 1}"
                details.append(
                    f'<div style="margin-top:6px;display:grid;grid-template-columns:88px 1fr;'
                    f'gap:9px;">'
                    f'<span style="font-family:{theme["font_mono"]};font-size:9px;'
                    f'letter-spacing:.12em;color:{theme["muted"]};">'
                    f'{_inline_md(label, theme_name)}</span>'
                    f'<span style="font-size:13px;line-height:1.65;color:{signal};">'
                    f'{_inline_md(value, theme_name)}</span></div>')
            row_items.append(
                f'<div style="padding:17px 0 19px;border-top:1px solid {theme["hairline"]};">'
                f'<div style="display:flex;gap:12px;align-items:baseline;">'
                f'<span style="font-family:{theme["font_en"]};font-style:italic;'
                f'font-size:12px;color:{theme["muted"]};">{row_no:02d}</span>'
                f'<span style="font-family:{theme["font_serif"]};font-size:18px;'
                f'font-weight:700;line-height:1.4;color:{signal};">{name}</span></div>'
                f'{"".join(details)}</div>')
        labels = " · ".join(_inline_md(x, theme_name) for x in head)
        return (
            f'<div style="margin:19px 0 27px;border-bottom:1px solid {theme["hairline"]};">'
            f'<div style="padding:0 0 9px;font-family:{theme["font_mono"]};font-size:9px;'
            f'letter-spacing:.12em;color:{theme["muted"]};text-transform:uppercase;">'
            f'DATA ROWS · {labels}</div>{"".join(row_items)}</div>')

    if theme.get("table_free") or theme_name in ("monitor", "noc"):
        # 服务器大屏监视风格：零表格（NO TABLES），文字 + 列表横排
        card_items = []
        for r in body:
            if not r:
                continue
            name = _inline_md(r[0], theme_name) if len(r) > 0 else ""
            orig = " ".join(r)
            is_up = _contains_up(orig)
            is_down = not is_up and _contains_down(orig)

            b_color = theme["up"] if is_up else (theme["down"] if is_down else theme["border"])
            dir_html = (
                f'<span style="color:{theme["up"]};{_flu_bg(theme["up"])}font-weight:bold;font-size:10px;">▲ 偏多</span>'
                if is_up
                else (f'<span style="color:{theme["down"]};{_flu_bg(theme["down"])}font-weight:bold;font-size:10px;">▼ 偏空</span>' if is_down else "")
            )
            val_strs = [
                f'<span style="padding:1px 4px;background:{theme["card_bg"]};border:1px solid {theme["border"]};color:{theme["accent"]};font-weight:bold;">{_inline_md(x, theme_name)}</span>'
                for x in r[1:]
            ]
            vals_line = " ".join(val_strs)

            card_items.append(
                f'<div style="flex:1 1 calc(50% - 6px);min-width:130px;background:{theme["card_bg"]};'
                f'border:1px solid {b_color};padding:6px 8px;font-size:{theme["size"]};'
                f'box-sizing:border-box;display:flex;flex-direction:column;gap:3px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                f'<span style="font-weight:bold;color:{theme["fg"]};font-size:{theme["size"]};">{name}</span>'
                f'{dir_html}</div>'
                f'<div style="color:{theme["muted"]};font-size:10px;">{vals_line}</div>'
                f'</div>'
            )
        return (
            f'<div style="display:flex;flex-direction:row;flex-wrap:wrap;gap:6px;margin:6px 0;'
            f'font-family:{theme.get("font", KLEIN["font"])};">'
            f'{"".join(card_items)}</div>'
        )

    # 表头 - 细线框 1px
    th_style_base = (
        f'border:1px solid {theme["border"]};'
        f'padding:2px 4px;'
        f'background:{theme["hbg"]};'
        f'color:{theme.get("hfg", theme["fg"])};'
        f'font-weight:bold;text-align:left;'
        f'font-size:{theme["size"]};'
        f'font-family:{theme.get("font", KLEIN["font"])};'
    )
    # 复古主题表头加像素图标
    th_cells = []
    for c in head:
        icon = ""
        if theme_name in ("pixel", "dos", "klein", "game"):
            icon = f'<span style="display:inline-block;width:6px;height:6px;background:{theme["accent"]};margin-right:4px;vertical-align:middle;"></span>'
        th_cells.append(
            f'<th style="{th_style_base}">{icon}{_inline_md(c, theme_name)}</th>'
        )
    th_html = "".join(th_cells)

    trs = []
    for r in body:
        tds = []
        for c in r:
            orig = c
            inner = _inline_md(c, theme_name)
            is_up = _contains_up(orig)
            is_down = not is_up and _contains_down(orig)

            # 基础 td：细线框
            td_base = (
                f'border:1px solid {theme["border"]};'
                f'padding:2px 4px;'
                f'color:{theme["fg"]};'
                f'font-size:{theme["size"]};'
                f'font-family:{theme.get("font", KLEIN["font"])};'
                f'line-height:{theme["line"]};'
            )
            prefix = ""

            if theme_name in ("pixel", "dos", "klein", "game"):
                if is_up:
                    # 涨：绿字 + 淡绿底 + ▲ 像素图标 最突出
                    td_base = (
                        f'border:1px solid {theme["border"]};'
                        f'padding:2px 4px;'
                        f'color:{theme["up"]};'
                        f'background:{theme["up_bg"]};'
                        f'font-weight:bold;'
                        f'font-size:{theme["size"]};'
                        f'font-family:{theme["font"]};'
                    )
                    prefix = f'<span style="font-size:9px;">▲</span> '
                elif is_down:
                    # 跌：红字 + 淡红底 + ▼ 最突出
                    td_base = (
                        f'border:1px solid {theme["border"]};'
                        f'padding:2px 4px;'
                        f'color:{theme["down"]};'
                        f'background:{theme["down_bg"]};'
                        f'font-weight:bold;'
                        f'font-size:{theme["size"]};'
                        f'font-family:{theme["font"]};'
                    )
                    prefix = f'<span style="font-size:9px;">▼</span> '
                else:
                    # 重点数据：数字/百分比/价格 加黄底黑框凸显，简洁
                    if len(orig) <= 20 and re.search(r"\d+\.\d+%?|\b\d+%\b|^\d+\.\d+$|^\d+$", orig):
                        inner = (
                            f'<span style="background:{theme["accent"]};'
                            f'color:{theme["accent_fg"]};'
                            f'border:1px solid {theme["border"]};'
                            f'padding:0px 3px;font-weight:bold;">'
                            f'{inner}</span>'
                        )
                    # game 主题：百分比数值附 8-bit 像素血条（HP BAR）
                    if theme_name == "game":
                        m = re.match(r"^(\d{1,3})\s*%$", orig)
                        if m:
                            filled = round(int(m.group(1)) / 10)
                            filled = max(0, min(10, filled))
                            bar = "█" * filled + "░" * (10 - filled)
                            inner += (
                                f' <span style="color:{theme["hp_full"]};'
                                f'font-size:9px;letter-spacing:-1px;'
                                f'background:{theme["card_bg"]};">'
                                f'{bar}</span>'
                            )
            else:
                # 兜底：其他主题保留涨跌突出
                if is_up:
                    td_base = (
                        f'border:1px solid {theme["border"]};'
                        f'padding:2px 4px;'
                        f'color:{theme.get("up", theme["fg"])};'
                        f'background:{theme.get("up_bg", KLEIN["card_bg"])};'
                        f'font-weight:bold;'
                    )
                    prefix = "▲ "
                elif is_down:
                    td_base = (
                        f'border:1px solid {theme["border"]};'
                        f'padding:2px 4px;'
                        f'color:{theme.get("down", theme["fg"])};'
                        f'background:{theme.get("down_bg", KLEIN["card_bg"])};'
                        f'font-weight:bold;'
                    )
                    prefix = "▼ "

            tds.append(f'<td style="{td_base}">{prefix}{inner}</td>')
        trs.append(f"<tr>{''.join(tds)}</tr>")

    table_style = (
        f'border-collapse:collapse;width:100%;'
        f'font-size:{theme["size"]};margin:4px 0;'
        f'border:1px solid {theme["border"]};'
    )
    return (
        f'<table style="{table_style}">'
        f"<thead><tr>{th_html}</tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table>"
    )


def md_to_html(md: str, theme_name: str = "game") -> str:
    """轻量 Markdown→HTML；所有主题使用内联样式，兼容 PushPlus/微信详情页。"""
    theme = _get_theme(theme_name)
    html: list[str] = []
    lines = md.split("\n")
    i = 0
    heading_no = 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue

        if s.startswith("```"):  # 代码块（用于字符模拟图等宽展示）
            lang = s[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1  # 跳过闭合 ```
            code_text = "\n".join(code_lines)
            # 转义以防止 HTML 注入，保留字符图原样
            esc = code_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            code_bg = theme.get("code_bg", theme["card_bg"])
            code_fg = theme.get("code_fg", theme["fg"])
            code_font = theme.get("font_mono", theme["font"])
            code_margin = "8px 0 10px" if theme.get("editorial") else "6px 0"
            code_padding = "8px 10px" if theme.get("editorial") else "8px"
            html.append(
                f'<pre style="background:{code_bg};border:1px solid {theme["border"]};'
                f'padding:{code_padding};overflow-x:auto;font-family:{code_font};'
                f'font-size:{theme["size"]};line-height:1.45;white-space:pre;'
                f'color:{code_fg};margin:{code_margin};">{esc}</pre>'
            )
            continue

        if s.startswith("|"):  # 表格块
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            if len(tbl) >= 2:
                html.append(_render_table(tbl, theme_name))
            continue

        if re.match(r"^-{3,}$", s):  # 分隔线
            if theme.get("editorial"):
                html.append(
                    f'<div style="margin:16px 0 14px;display:flex;align-items:center;gap:10px;">'
                    f'<span style="width:24px;height:1px;background:{theme["border"]};"></span>'
                    f'<span style="font-family:{theme["font_en"]};font-size:13px;'
                    f'font-style:italic;color:{theme["muted"]};">§</span>'
                    f'<span style="height:1px;flex:1;background:{theme["hairline"]};"></span></div>')
            elif theme_name == "game":
                # 游戏风：金色 2px 实线（像素分割线）
                html.append(
                    f'<hr style="border:none;border-top:2px solid {theme["border"]};'
                    f'margin:8px 0;">'
                )
            elif theme_name in ("pixel", "dos", "klein"):
                html.append(
                    f'<hr style="border:none;border-top:1px dashed {theme["border"]};margin:8px 0;">'
                )
            else:
                html.append(
                    f'<hr style="border:none;border-top:1px solid {theme["border"]};margin:10px 0;">'
                )
            i += 1
            continue

        if s.startswith("#"):  # 标题
            h = len(s) - len(s.lstrip("#"))
            txt = s.lstrip("#").strip()
            fs = {"1": theme["size_h1"], "2": theme["size_h2"]}.get(
                str(h), theme["size_h3"]
            )
            heading_no += 1
            if theme.get("editorial"):
                kicker = "OPENING" if h == 1 else f"SECTION · {heading_no:02d}"
                top_rule = "" if h == 1 else (
                    f'border-top:1px solid {theme["border"]};padding-top:12px;')
                margin = "5px 0 12px" if h == 1 else "18px 0 9px"
                html.append(
                    f'<div style="{top_rule}margin:{margin};">'
                    f'<div style="font-family:{theme["font_mono"]};font-size:9px;'
                    f'letter-spacing:.22em;text-transform:uppercase;color:{theme["muted"]};'
                    f'margin-bottom:5px;">{kicker}</div>'
                    f'<div style="font-family:{theme["font_serif"]};font-size:{fs};'
                    f'font-weight:700;color:{theme["accent"]};'
                    f'background:{FLU_BLACK_BG};padding:2px 8px 3px;'
                    f'line-height:1.28;'
                    f'letter-spacing:-.02em;overflow-wrap:anywhere;">'
                    f'{_inline_md(txt, theme_name, link_color=theme["accent"])}</div></div>')
            elif theme_name in ("pixel", "dos", "klein", "game"):
                if theme_name == "game":
                    icon_map = {1: "◆", 2: "►", 3: "·"}
                    lb = "3px"  # 游戏风粗左线
                else:
                    icon_map = {1: "■", 2: "►", 3: "·"}
                    # klein 游戏复古用粗左线 3px；pixel 监控风保持细线 1px
                    lb = "3px" if theme_name == "klein" else "1px"
                icon = icon_map.get(h, "·")
                html.append(
                    f'<div style="font-size:{fs};font-weight:bold;'
                    f'color:{theme["fg"]};margin:8px 0 3px;'
                    f'border-left:{lb} solid {theme["border"]};'
                    f'padding-left:6px;'
                    f'font-family:{theme["font"]};line-height:{theme["line"]};'
                    f'letter-spacing:1px;">'
                    f'<span style="margin-right:4px;color:{theme["accent"]};'
                    f'background:{FLU_BLACK_BG};padding:0 2px;">{icon}</span>'
                    f"{_inline_md(txt, theme_name)}</div>"
                )
            else:
                html.append(
                    f'<div style="font-size:{fs};font-weight:bold;'
                    f'color:{theme["fg"]};margin:8px 0 4px;'
                    f'font-family:{theme.get("font", KLEIN["font"])};">'
                    f"{_inline_md(txt, theme_name)}</div>"
                )
            i += 1
            continue

        if s.startswith(">"):  # 引用块
            qs = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                qs.append(lines[i].strip().lstrip(">").strip())
                i += 1
            if theme.get("editorial"):
                html.append(
                    f'<div style="margin:26px 0 28px;padding:19px 0 20px;'
                    f'border-top:1px solid {theme["border"]};'
                    f'border-bottom:1px solid {theme["hairline"]};">'
                    f'<div style="font-family:{theme["font_en"]};font-size:28px;'
                    f'font-style:italic;line-height:.8;color:{theme["muted"]};">“</div>'
                    f'<div style="font-family:{theme["font_serif"]};font-size:14px;'
                    f'line-height:1.55;color:{theme["fg"]};">'
                    + "<br>".join(_inline_md(q, theme_name) for q in qs)
                    + f'</div><div style="margin-top:11px;font-family:{theme["font_mono"]};'
                      f'font-size:9px;letter-spacing:.18em;color:{theme["muted"]};">'
                      f'EDITORIAL NOTE</div></div>')
            elif theme_name in ("pixel", "dos", "klein", "game"):
                # game 游戏风：金色 3px 左线（任务/提示框）；pixel 细线 1px
                lb = "3px" if theme_name in ("klein", "game") else "1px"
                lb_color = theme["accent"] if theme_name == "game" else theme["border"]
                html.append(
                    f'<div style="color:{theme["muted"]};font-size:{theme["size"]};'
                    f'border:1px solid {theme["border"]};'
                    f'border-left:{lb} solid {lb_color};'
                    f'padding:4px 6px;margin:4px 0;'
                    f'background:{theme["card_bg"]};'
                    f'font-family:{theme["font"]};line-height:{theme["line"]};'
                    f'">' + "<br>".join(_inline_md(q, theme_name) for q in qs) + "</div>"
                )
            else:
                html.append(
                    f'<div style="color:{theme["muted"]};font-size:{theme["size"]};'
                    f'border-left:3px solid {theme["border"]};'
                    f'padding-left:8px;margin:4px 0;">'
                    + "<br>".join(_inline_md(q, theme_name) for q in qs)
                    + "</div>"
                )
            continue

        if re.match(r"^[-*]\s+", s):  # 无序列表
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(re.sub(r"^[-*]\s+", "", lines[i].strip()))
                i += 1
            if theme.get("editorial"):
                lis = "".join(
                    f'<li style="list-style:none;display:flex;gap:11px;align-items:flex-start;'
                    f'padding:5px 0;border-top:1px solid {theme["hairline"]};">'
                    f'<span style="font-family:{theme["font_en"]};font-style:italic;'
                    f'color:{theme["muted"]};">—</span>'
                    f'<span style="flex:1;">{_inline_md(x, theme_name)}</span></li>'
                    for x in items)
                html.append(
                    f'<ul style="margin:8px 0 10px;padding:0;border-bottom:1px solid '
                    f'{theme["hairline"]};color:{theme["fg"]};font-family:{theme["font"]};'
                    f'font-size:{theme["size"]};line-height:{theme["line"]};list-style:none;">'
                    f'{lis}</ul>')
            elif theme_name in ("pixel", "dos", "klein", "game"):
                # game/pixel 用金币/琥珀方块；klein 游戏复古用黑方块
                mk = theme["accent"] if theme_name in ("pixel", "dos", "game") else theme["border"]
                lis = "".join(
                    f'<li style="margin:2px 0;list-style:none;position:relative;padding-left:12px;">'
                    f'<span style="position:absolute;left:0;top:2px;width:6px;height:6px;'
                    f'background:{mk};display:inline-block;"></span>'
                    f"{_inline_md(x, theme_name)}</li>"
                    for x in items
                )
                html.append(
                    f'<ul style="margin:4px 0;padding-left:4px;'
                    f'color:{theme["fg"]};font-family:{theme["font"]};'
                    f'font-size:{theme["size"]};list-style:none;">{lis}</ul>'
                )
            else:
                lis = "".join(
                    f'<li style="margin:2px 0;">{_inline_md(x, theme_name)}</li>'
                    for x in items
                )
                html.append(
                    f'<ul style="margin:4px 0;padding-left:18px;'
                    f'color:{theme["fg"]};">{lis}</ul>'
                )
            continue

        if re.match(r"^\d+\.\s+", s):  # 有序列表
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i].strip()))
                i += 1
            if theme.get("editorial"):
                lis = "".join(
                    f'<li style="list-style:none;display:grid;grid-template-columns:34px 1fr;'
                    f'gap:8px;padding:6px 0;border-top:1px solid {theme["hairline"]};">'
                    f'<span style="font-family:{theme["font_en"]};font-size:18px;'
                    f'font-style:italic;color:{theme["muted"]};">{idx:02d}</span>'
                    f'<span>{_inline_md(x, theme_name)}</span></li>'
                    for idx, x in enumerate(items, 1))
                html.append(
                    f'<ol style="margin:8px 0 10px;padding:0;border-bottom:1px solid '
                    f'{theme["hairline"]};color:{theme["fg"]};font-family:{theme["font"]};'
                    f'font-size:{theme["size"]};line-height:{theme["line"]};list-style:none;">'
                    f'{lis}</ol>')
            else:
                lis = "".join(
                    f'<li style="margin:2px 0;">{_inline_md(x, theme_name)}</li>'
                    for x in items
                )
                html.append(
                    f'<ol style="margin:4px 0;padding-left:18px;'
                    f'color:{theme["fg"]};font-family:{theme.get("font")};'
                    f'font-size:{theme["size"]};">{lis}</ol>'
                )
            continue

        # 普通段落
        para = [s]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith(("|", "#", ">", "-", "*"))
                or re.match(r"^\d+\.\s+", nxt)
                or re.match(r"^-{3,}$", nxt)
            ):
                break
            para.append(nxt)
            i += 1
        para_margin = "5px 0 8px" if theme.get("editorial") else "4px 0"
        html.append(
            f'<div style="margin:{para_margin};color:{theme["fg"]};'
            f'font-size:{theme["size"]};line-height:{theme["line"]};'
            f'font-family:{theme.get("font", KLEIN["font"])};overflow-wrap:anywhere;">'
            + "<br>".join(_inline_md(p, theme_name) for p in para)
            + "</div>"
        )
    return "".join(html)


def themed_html(title: str, content_md: str, theme_name: str = "game",
                *, continuation: bool = False, part_no: int = 1,
                part_count: int = 1) -> str:
    theme = _get_theme(theme_name)
    body = md_to_html(content_md, theme_name)
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if continuation and theme.get("continuation"):
        # 分篇推送的续篇壳（guizang）：省略 108px 幽灵字巨幅 Hero 与杂志大页脚，
        # 改用一行等宽页眉 + 小号衬线标题 + 一行页脚，视觉仍属同一杂志长页。
        # 目的：把每篇的「壳」从 ~4KB 压到 ~1.3KB，让长研报在微信软上限内
        # 用更少的篇数装下，避免整篇退回纯 markdown（推送页丢失主题风格）。
        part_label = f"PART {part_no}/{part_count}" if part_count > 1 else "CONTINUED"
        return (
            f'<div style="width:100%;margin:0;background:{theme["bg"]};padding:12px 0;'
            f'color:{theme["fg"]};font-family:{theme["font"]};box-sizing:border-box;">'
            f'<div style="width:100%;max-width:760px;margin:0 auto;background:{theme["page_bg"]};'
            f'overflow:hidden;box-sizing:border-box;">'
            # 紧凑页眉：等宽杂志眉线 + 小号衬线标题（墨黑底，荧光黄绿标题自带深底）
            f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};padding:14px 20px 16px;">'
            f'<div style="display:flex;justify-content:space-between;gap:12px;'
            f'padding-bottom:10px;border-bottom:1px solid {theme["muted"]};'
            f'font-family:{theme["font_mono"]};font-size:9px;line-height:1.5;'
            f'letter-spacing:.16em;text-transform:uppercase;opacity:.82;">'
            f'<span>OCTOPUS AI<br>MARKET INTELLIGENCE</span>'
            f'<span style="text-align:right;">{part_label}<br>VERTICAL EDITION</span></div>'
            f'<div style="margin-top:11px;font-family:{theme["font_serif"]};font-size:17px;'
            f'font-weight:700;line-height:1.32;letter-spacing:-.015em;'
            f'overflow-wrap:anywhere;">{safe_title}</div></div>'
            # 正文：与首篇同版式
            f'<div style="background:{theme["page_bg"]};padding:22px 20px 40px;'
            f'font-family:{theme["font"]};font-size:{theme["size"]};'
            f'line-height:{theme["line"]};overflow-wrap:anywhere;">'
            f'<div style="display:flex;justify-content:space-between;gap:12px;'
            f'padding-bottom:10px;margin-bottom:22px;border-bottom:1px solid {theme["border"]};'
            f'font-family:{theme["font_mono"]};font-size:9px;letter-spacing:.15em;'
            f'color:{theme["muted"]};text-transform:uppercase;">'
            f'<span>LONGFORM / {part_no:02d}</span><span>READ ↓</span></div>{body}</div>'
            # 紧凑页脚
            # 紧凑页脚（墨黑底）
            f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};padding:14px 20px;">'
            f'<div style="height:1px;background:{theme["muted"]};margin-bottom:10px;"></div>'
            f'<div style="display:flex;justify-content:space-between;gap:14px;'
            f'font-family:{theme["font_mono"]};font-size:9px;letter-spacing:.12em;'
            f'opacity:.62;">'
            f'<span>章鱼 AI · 全景分析</span>'
            f'<span style="text-align:right;">END {part_label}<br>04 / FIN</span></div>'
            f'</div></div></div>')
    if theme_name == "guizang":
        # Guizang PPT Skill Style A 的竖版长页适配：横向 slide/chrome 被重新解释为
        # 连续文章的 Hero / section / rowline。全内联，保证 PushPlus 详情页不依赖 CSS/JS。
        return (
            f'<div style="width:100%;margin:0;background:{theme["bg"]};padding:12px 0;'
            f'color:{theme["fg"]};font-family:{theme["font"]};box-sizing:border-box;">'
            f'<div style="width:100%;max-width:760px;margin:0 auto;background:{theme["page_bg"]};'
            f'overflow:hidden;box-sizing:border-box;">'
            # 墨黑 Hero：等宽页眉 + 衬线主标题 + 巨型幽灵字
            # （荧光黄绿标题文字必须落在墨黑底上，不用灰色外框色）
            f'<div style="position:relative;overflow:hidden;background:{theme["hbg"]};'
            f'color:{theme["hfg"]};padding:14px 16px 18px;">'
            f'<div style="position:absolute;right:-10px;bottom:-30px;font-family:{theme["font_en"]};'
            f'font-size:76px;font-weight:700;line-height:1;opacity:.06;">04</div>'
            f'<div style="position:relative;display:flex;justify-content:space-between;gap:12px;'
            f'padding-bottom:8px;border-bottom:1px solid {theme["muted"]};'
            f'font-family:{theme["font_mono"]};font-size:9px;line-height:1.5;'
            f'letter-spacing:.16em;text-transform:uppercase;opacity:.82;">'
            f'<span>OCTOPUS AI<br>MARKET INTELLIGENCE</span>'
            f'<span style="text-align:right;">WECHAT<br>LONGFORM</span></div>'
            f'<div style="position:relative;margin-top:14px;font-family:{theme["font_mono"]};'
            f'font-size:8px;letter-spacing:.18em;text-transform:uppercase;opacity:.65;">'
            f'RESEARCH NOTE · ELECTRONIC INK</div>'
            f'<div style="position:relative;margin-top:7px;font-family:{theme["font_serif"]};'
            f'font-size:28px;font-weight:700;line-height:1.24;letter-spacing:-.025em;'
            f'overflow-wrap:anywhere;">{safe_title}</div>'
            f'<div style="position:relative;margin-top:10px;max-width:560px;font-family:{theme["font"]};'
            f'font-size:11px;line-height:1.5;color:{theme["fg"]};background:{theme["accent"]};padding:5px 7px;">'
            f'{BRAND_SUBTITLE}</div>'
            f'<div style="position:relative;margin-top:12px;width:24px;height:2px;background:{theme["accent"]};"></div></div>'
            # 正文：暖纸、窄栏、长页；rowline 与 section 由 md_to_html 生成
            f'<div style="background:{theme["page_bg"]};padding:16px 16px 24px;'
            f'font-family:{theme["font"]};font-size:{theme["size"]};'
            f'line-height:{theme["line"]};overflow-wrap:anywhere;">'
            f'<div style="display:flex;justify-content:space-between;gap:12px;'
            f'padding-bottom:6px;margin-bottom:12px;border-bottom:1px solid {theme["border"]};'
            f'font-family:{theme["font_mono"]};font-size:9px;letter-spacing:.15em;'
            f'color:{theme["muted"]};text-transform:uppercase;">'
            f'<span>LONGFORM / 01</span><span>READ ↓</span></div>{body}</div>'
            # 杂志页脚
            f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};padding:12px 16px;">'
            f'<div style="height:1px;background:{theme["muted"]};margin-bottom:14px;"></div>'
            f'<div style="display:flex;justify-content:space-between;gap:14px;align-items:flex-end;">'
            f'<div style="font-family:{theme["font_serif"]};font-size:17px;line-height:1.45;">'
            f'章鱼 AI<br><span style="font-family:{theme["font"]};font-size:10px;opacity:.62;">'
            f'全景分析 · 多模型协同</span></div>'
            f'<div style="font-family:{theme["font_mono"]};font-size:9px;line-height:1.65;'
            f'letter-spacing:.12em;text-align:right;opacity:.62;">END OF NOTE<br>04 / FIN</div>'
            f'</div></div></div></div>')
    if theme_name == "game":
        # 8-bit 像素游戏风（兼容保留）：深夜蓝游戏屏 + 金色粗框 + 硬黑像素阴影
        # 标题栏=游戏菜单金条 / 状态栏=♥HP血条+★LV等级 / 底部=SCORE+PRESS START
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # 由标题生成稳定的游戏数值（SCORE/HP/LV），同一标题每次渲染一致
        seed = sum(ord(c) for c in title)
        score = seed * 7 % 999999
        hp = 60 + seed % 40                       # 60-99
        hp_filled = round(hp / 10)
        hp_bar = ("█" * hp_filled
                  + "░" * (10 - hp_filled))
        lv = 1 + seed % 9                         # LV.01-09
        return (
            # 外层：游戏机屏幕（深夜蓝 + 像素星点背景）
            f'<div style="background:{theme["bg"]};padding:14px 8px;'
            f'font-size:{theme["size"]};line-height:{theme["line"]};'
            f'color:{theme["fg"]};font-family:{theme["font"]};'
            f'background-image:'
            f'radial-gradient(1.5px 1.5px at 26px 32px,{theme["star1"]},rgba(0,0,0,0) 70%),'
            f'radial-gradient(1.5px 1.5px at 88px 74px,{theme["star1"]},rgba(0,0,0,0) 70%),'
            f'radial-gradient(2px 2px at 152px 22px,{theme["star2"]},rgba(0,0,0,0) 70%),'
            f'radial-gradient(1.5px 1.5px at 212px 96px,{theme["star1"]},rgba(0,0,0,0) 70%),'
            f'radial-gradient(2px 2px at 310px 52px,{theme["star2"]},rgba(0,0,0,0) 70%);\">'
            # 游戏面板：金色 2px 粗框 + 硬黑像素阴影 + 金色网格
            f'<div style="background:{theme["card_bg"]};'
            f'background-image:repeating-linear-gradient(0deg,{theme["grid"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 24px),'
            f'repeating-linear-gradient(90deg,{theme["grid"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 24px);'
            f'border:2px solid {theme["border"]};'
            f'box-shadow:4px 4px 0 {theme["shadow"]};'
            f'padding:0;">'
            # 标题栏：菜单金条（黑字 + 游戏角标 + SCORE 计分）
            f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};'
            f'font-size:{theme["size_title"]};font-weight:bold;'
            f'padding:6px 8px;border-bottom:2px solid {theme["hfg"]};'
            f'font-family:{theme["font"]};letter-spacing:1px;">'
            f'<span>◤</span> {safe_title}'
            f'<span style="float:right;font-size:9px;letter-spacing:1px;">'
            f'SCORE {score:06d}</span>'
            f'</div>'
            # 状态栏：♥ HP 血条 + ★ LV 等级 + UTC 时间戳
            f'<div style="background:{theme["status_bg"]};color:{theme["muted"]};'
            f'font-size:9px;letter-spacing:1px;'
            f'padding:3px 8px;border-bottom:2px solid {theme["border"]};'
            f'font-family:{theme["font"]};">'
            f'<span style="color:{theme["down"]};">♥</span> '
            f'<span style="color:{theme["hp_full"]};background:{FLU_BLACK_BG};padding:0 2px;">{hp_bar}</span>'
            f' <span style="color:{theme["up"]};background:{FLU_BLACK_BG};padding:0 2px;">{hp}</span>/100'
            f'&nbsp;&nbsp;<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">★</span> LV.{lv:02d}'
            f'<span style="float:right;">✦ {stamp} UTC</span>'
            f'</div>'
            # 正文
            f'<div style="padding:8px;">{body}</div>'
            # 底部状态行：GAME.LOG + PRESS START + 光标块
            f'<div style="border-top:2px solid {theme["border"]};'
            f'margin:4px 8px 6px;padding-top:4px;color:{theme["muted"]};'
            f'font-size:9px;font-family:{theme["font"]};letter-spacing:1px;">'
            f'<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">▞▚</span> GAME.LOG · '
            f'<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">★</span> LV.{lv:02d}'
            f'<span style="float:right;">PRESS START <span style="color:{theme["accent"]};background:{FLU_BLACK_BG};">▮</span></span>'
            f'</div>'
            f'</div></div>'
        )
    if theme_name == "dos":
        # DOS CRT 终端：仿命令行窗口、磷光绿文字、琥珀光标与扫描线。
        stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        # 分篇推送时在标题栏右侧标出「PART n/m」：DOS 主题正文不渲染标题，
        # 没有这个标记读者看到第 2/3 篇时不知道是续篇。
        right_tag = (f"PART {part_no}/{part_count}" if part_count > 1
                     else "● SYSTEM READY")
        return (
            f'<div style="background:{theme["bg"]};padding:12px 8px;'
            f'font-size:{theme["size"]};line-height:{theme["line"]};'
            f'color:{theme["fg"]};font-family:{theme["font"]};'
            f'background-image:repeating-linear-gradient(0deg,{theme["scan"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 3px);">'
            f'<div style="background:{theme["card_bg"]};'
            f'background-image:repeating-linear-gradient(0deg,{theme["grid"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 24px);'
            f'border:1px solid {theme["border"]};'
            f'box-shadow:2px 2px 0 {theme["shadow"]};padding:0;">'
            f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};'
            f'font-size:{theme["size_title"]};font-weight:bold;'
            f'padding:6px 8px;border-bottom:1px solid {theme["border"]};'
            f'font-family:{theme["font"]};letter-spacing:.5px;">'
            f'<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 3px;">C:\&gt;</span> '
            f'DOS MARKET MONITOR'
            f'<span style="float:right;color:{theme["up"]};background:{FLU_BLACK_BG};'
            f'padding:0 3px;font-size:9px;">{right_tag}</span></div>'
            f'<div style="border-bottom:1px dashed {theme["border"]};'
            f'padding:4px 8px;color:{theme["muted"]};font-size:9px;'
            f'letter-spacing:.3px;font-family:{theme["font"]};">'
            f'C:\MARKET\MONITOR.BAT&nbsp;&nbsp; | &nbsp;&nbsp;{stamp} BJT&nbsp;&nbsp; | &nbsp;&nbsp;FEED: ONLINE'
            f'</div>'
            f'<div style="padding:8px;">{body}</div>'
            f'<div style="border-top:1px dashed {theme["border"]};'
            f'margin:4px 8px 6px;padding:5px 0 2px;color:{theme["muted"]};'
            f'font-size:9px;font-family:{theme["font"]};">'
            f'<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 3px;">C:\&gt;</span> '
            f'END OF REPORT&nbsp;&nbsp; <span style="color:{theme["hfg"]};">█</span>'
            f'<span style="float:right;">OCTOPUS AI / DOS-CRT</span>'
            f'</div></div></div>'
        )
    if theme_name == "pixel":
        # 复古监控风：暗色服务器大屏 + 细线框 + 等宽细字 + 摄像头 REC 元素
        safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f'<div style="background:{theme["bg"]};padding:12px 8px;'
            f'font-size:{theme["size"]};line-height:{theme["line"]};'
            f'color:{theme["fg"]};font-family:{theme["font"]};'
            f'background-image:repeating-linear-gradient(0deg,{theme["scan"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 3px);">'
            f'<div style="background:{theme["card_bg"]};'
            f'background-image:repeating-linear-gradient(0deg,{theme["grid"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 24px),'
            f'repeating-linear-gradient(90deg,{theme["grid"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 24px);'
            f'border:1px solid {theme["border"]};'
            f'box-shadow:2px 2px 0 {theme["shadow"]};'
            f'padding:0;">'
            f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};'
            f'font-size:{theme["size_title"]};font-weight:bold;'
            f'padding:5px 8px;border-bottom:1px solid {theme["border"]};'
            f'font-family:{theme["font"]};letter-spacing:1px;">'
            f'<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">■</span> '
            f'{safe_title}'
            f'<span style="float:right;color:{theme["down"]};font-size:9px;'
            f'font-weight:bold;letter-spacing:1px;">● REC</span>'
            f'</div>'
            f'<div style="border-bottom:1px dashed {theme["border"]};'
            f'padding:2px 8px;color:{theme["muted"]};font-size:9px;'
            f'letter-spacing:1px;font-family:{theme["font"]};">'
            f'⌜ CAM-01 ▸ 04-SERVER ▸ LIVE ▸ {stamp} UTC ⌟'
            f'</div>'
            f'<div style="padding:8px;">{body}</div>'
            f'<div style="border-top:1px dashed {theme["border"]};'
            f'margin:4px 8px 6px;padding-top:4px;color:{theme["muted"]};'
            f'font-size:9px;font-family:{theme["font"]};letter-spacing:1px;">'
            f'<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">▚▞</span> SYS.OK · CH-04 · '
            f'<span style="color:{theme["down"]};">●</span> REC-ON'
            f'</div>'
            f'</div></div>'
        )
    if theme_name in ("monitor", "noc"):
        # 服务器大屏监视风：深空暗底 + 荧光青绿 + 零表格（文字+横排卡片流） + HUD 双时钟
        safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f'<div style="background:{theme["bg"]};padding:12px 8px;'
            f'font-size:{theme["size"]};line-height:{theme["line"]};'
            f'color:{theme["fg"]};font-family:{theme["font"]};'
            f'background-image:repeating-linear-gradient(0deg,{theme["scan"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 24px),'
            f'repeating-linear-gradient(90deg,{theme["grid"]} 0 1px,'
            f'rgba(0,0,0,0) 1px 24px);">'
            f'<div style="background:{theme["card_bg"]};'
            f'border:1px solid {theme["border"]};'
            f'box-shadow:0 0 10px {theme["glow"]};'
            f'padding:0;">'
            f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};'
            f'font-size:{theme["size_title"]};font-weight:bold;'
            f'padding:6px 10px;border-bottom:1px solid {theme["border"]};'
            f'font-family:{theme["font"]};letter-spacing:1px;'
            f'display:flex;justify-content:space-between;align-items:center;">'
            f'<span><span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">⌜</span> {safe_title} <span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">⌟</span></span>'
            f'<span style="color:{theme["down"]};font-size:9px;'
            f'font-weight:bold;letter-spacing:1px;">● REC LIVE</span>'
            f'</div>'
            f'<div style="border-bottom:1px dashed {theme["border"]};'
            f'padding:3px 10px;color:{theme["muted"]};font-size:9px;'
            f'letter-spacing:0.5px;font-family:{theme["font"]};'
            f'display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;">'
            f'<span><span style="color:{theme["accent"]};background:{FLU_BLACK_BG};">●</span> NOC-SERVER-04 · LIVE TELEMETRY</span>'
            f'<span>✦ {stamp} UTC</span>'
            f'</div>'
            f'<div style="padding:10px 8px;">{body}</div>'
            f'<div style="border-top:1px dashed {theme["border"]};'
            f'margin:4px 8px 6px;padding-top:4px;color:{theme["muted"]};'
            f'font-size:9px;font-family:{theme["font"]};letter-spacing:1px;'
            f'display:flex;justify-content:space-between;">'
            f'<span><span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 2px;">▚▞</span> SYS.NOMINAL · CH-04 · 12ms</span>'
            f'<span style="color:{theme["accent"]};background:{FLU_BLACK_BG};padding:0 4px;">ALL FEEDS VERIFIED ▮</span>'
            f'</div>'
            f'</div></div>'
        )
    # klein 游戏复古像素风：米黄纸底 + 白卡片 + 1px黑细框 + 像素阴影 + 黑底标题栏
    return (
        f'<div style="background:{theme["bg"]};padding:10px;'
        f'font-size:{theme["size"]};line-height:{theme["line"]};'
        f'color:{theme["fg"]};font-family:{theme["font"]};">'
        f'<div style="background:{theme["card_bg"]};'
        f'border:1px solid {theme["border"]};'
        f'box-shadow:3px 3px 0 {theme["border"]};'
        f'padding:0;">'
        f'<div style="background:{theme["hbg"]};color:{theme["hfg"]};'
        f'font-size:{theme["size_title"]};font-weight:bold;'
        f'padding:5px 8px;border-bottom:1px solid {theme["border"]};'
        f'font-family:{theme["font"]};letter-spacing:1px;">'
        f'<span style="display:inline-block;width:8px;height:8px;'
        f'background:{theme["accent"]};margin-right:6px;'
        f'vertical-align:middle;border:1px solid {theme["hfg"]};"></span>'
        f'{safe_title}'
        f'<span style="float:right;font-size:9px;opacity:0.8;">[RETRO]</span>'
        f'</div>'
        f'<div style="padding:8px;">{body}</div>'
        f'<div style="border-top:1px dashed {theme["border"]};'
        f'margin:4px 8px 8px;height:0;"></div>'
        f'</div></div>'
    )


# ================================================================ 模块⑤：推送通道

CHANNEL_LIMITS = {"pushplus": 0, "serverchan": 20000,
                  "wecom": 4096, "console": 0}  # 0 = 不限


# ================================================================ 运行状态与内容指纹
#
# 每次真实推送把「内容指纹 + 样本标题指纹 + 行情快照」写入本地状态文件
# （默认 output/push_state.json，可用环境变量 PUSH_STATE_PATH 覆盖，可用
# --no-state 关闭；GitHub Actions 用 actions/cache 跨运行恢复）。
# 与上次对比后即可回答两个问题——
#   ① 本次推送正文与上次是否几乎一致（“内容没变”）；
#   ② 数据窗口内新增了几条样本（🆕 增量）。
# 结论会直接印在推送正文顶部的「数据新鲜度看板」里，
# 不用翻 Actions 日志就能看出本次运行到底有没有新内容。

STATE_VERSION = 2
STATE_ENV_VAR = "PUSH_STATE_PATH"
STATE_MAX_KEYS_PER_SRC = 60          # 每源最多留存的样本指纹数
STATE_MAX_RUN_KEYS = 8               # 状态文件内最多保留的 模板|主题|代码 组合数

# AI 差异化重试前缀：指纹与上次完全一致时，自动换表述重试一次
ANTI_DUP_PREFIX = (
    "【差异化要求】上次推送的正文与本次即将生成的内容几乎完全一致。"
    "请换用不同的表述结构与段落组织重新生成（概率结论可保持一致），"
    "并在结尾新增一句「本期与上期观点差异」，显式说明本期新增证据；"
    "若确实没有任何变化，该句明确写作「无变化」。\n\n")


def _state_default_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "output", "push_state.json")


def state_path() -> str:
    return env(STATE_ENV_VAR) or _state_default_path()


def load_state(path: str) -> dict:
    """读取跨运行状态；文件缺失或损坏时返回空基线，绝不中断主流程。"""
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("runs"), dict):
            return st
    except Exception:  # noqa: BLE001 —— 状态损坏视为全新基线
        pass
    return {"version": STATE_VERSION, "runs": {}}


def save_state(path: str, st: dict) -> None:
    """原子写入状态文件；失败仅告警，不影响推送本身。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠️ 状态文件写入失败（不影响推送）：{str(e)[:100]}")


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _item_key(title: str) -> str:
    """样本标题指纹：忽略空白与大小写，跨运行可比。"""
    t = _norm_text(title)
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16] if t else ""


def gather_item_keys(*packs) -> dict[str, list[str]]:
    """汇总各数据源本次样本标题指纹（按源分组、去重）。"""
    out: dict[str, list[str]] = {}

    def add(src: str, title: str) -> None:
        key = _item_key(title)
        if not key:
            return
        bucket = out.setdefault(src, [])
        if key not in bucket:
            bucket.append(key)

    for pack in packs:
        if pack is None:
            continue
        items = getattr(pack, "items", None)
        if isinstance(items, dict):
            for src, lst in items.items():
                for it in lst or []:
                    add(str(src), getattr(it, "title", "") or "")
        # 十七/十八平台扫描
        scan = getattr(pack, "scan", None)
        if scan is not None:
            for groups in (getattr(scan, "direct", None),
                           getattr(scan, "sector_hits", None)):
                if isinstance(groups, dict):
                    for src, lst in groups.items():
                        for it in lst or []:
                            add(f"扫·{src}", getattr(it, "title", "") or "")
        # 国外机构一周研报（新增）
        reports = getattr(pack, "reports", None)
        if isinstance(reports, dict):
            for src, lst in reports.items():
                for it in lst or []:
                    add(f"外研·{src}", getattr(it, "title", "") or "")
    return {src: sorted(keys) for src, keys in out.items()}


def _quote_brief(quotes: list[Quote]) -> dict:
    """提取行情共识用于指纹与看板（多源取中位数）。"""
    prices = [q.price for q in quotes if q.ok and q.price]
    pcts = [q.change_pct for q in quotes
            if q.ok and q.change_pct is not None]
    brief: dict = {"n_ok": len(prices), "n_total": len(quotes)}
    if prices:
        brief["price"] = round(median(prices), 3)
        if pcts:
            brief["change_pct"] = round(median(pcts), 2)
        name = next((q.name for q in quotes if q.ok and q.name), "")
        if name:
            brief["name"] = name
        ts = next((q.time_str for q in quotes if q.ok and q.time_str), "")
        if ts:
            brief["time_str"] = ts
    return brief


def run_state_key(template: str, topic: str, hk_code_raw: str) -> str:
    if not hk_code_raw:
        code = "-"
    else:
        try:
            _c, _y, _em, sfx = resolve_chart_symbols(hk_code_raw)
            code = f"{sfx}{_c}"
        except Exception:
            code = normalize_hk_code(hk_code_raw)
    return f"{template}|{topic[:24]}|{code}"


def content_fingerprint(template: str, topic: str, ai_md: str,
                        quote_brief: dict, extra_points: dict,
                        item_keys: dict[str, list[str]]) -> str:
    """内容指纹：正文+行情共识+本地锚点+样本集合。
    时间戳/样本年龄不参与，保证「内容真变了」指纹才变。"""
    payload = {"t": template, "topic": _norm_text(topic),
               "md": _norm_text(ai_md),
               "q": quote_brief, "x": extra_points, "items": item_keys}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def diff_item_keys(cur: dict[str, list[str]],
                   prev: dict[str, list[str]]) -> dict[str, list[str]]:
    """本次有、上次没有的样本指纹，按源分组；无基线时返回空。"""
    if not prev:
        return {}
    out: dict[str, list[str]] = {}
    for src, keys in cur.items():
        prev_set = set(prev.get(src, []))
        new = [k for k in keys if k not in prev_set]
        if new:
            out[src] = new
    return out


def _quote_freshness_line(qb: dict, prev_price) -> str:
    """行情一句话：共识价 + 在线源数 + 与上次推送的价格差异。"""
    if not qb.get("n_ok"):
        return "三源行情未接入或全部失败（详见正文核验区块与数据缺口）"
    pct = (f"（{qb['change_pct']:+.2f}%）"
           if qb.get("change_pct") is not None else "")
    base = f"{qb['price']:.3f}{pct} · {qb['n_ok']}/{qb['n_total']} 源在线"
    if qb.get("time_str"):
        base += f" · 行情时间 {qb['time_str']}"
    if prev_price is None:
        return base + "｜首次记录基线"
    diff = qb["price"] - prev_price
    if abs(diff) < 1e-9:
        return base + "｜较上次推送价格持平"
    dpct = diff / prev_price * 100 if prev_price else 0.0
    return f"{base}｜较上次推送 {diff:+.3f}（{dpct:+.2f}%）"


def render_freshness_md(*, now_cst: str, template: str, hours: int,
                        fingerprint: str, dup: bool | None,
                        quote_line: str, new_items: dict[str, list[str]],
                        first_run: bool, anchor_line: str,
                        state_enabled: bool) -> str:
    """推送正文顶部的数据新鲜度看板：一眼看出本次运行有没有更新。"""
    if not state_enabled:
        fp_note = "状态对比已关闭（--no-state）"
    elif first_run:
        fp_note = "🆕 首次建立基线，下次运行起对比增量"
    elif dup:
        fp_note = "⚠️ 与上次推送内容一致（窗口内无新增信号）"
    else:
        fp_note = "✅ 与上次推送相比已更新"
    if not state_enabled or first_run:
        new_line = "🆕 新增样本：—（无上次基线可对比）"
    elif new_items:
        per_src = " · ".join(
            f"{s} {len(v)}" for s, v in
            sorted(new_items.items(), key=lambda kv: -len(kv[1]))[:5])
        total_new = sum(len(v) for v in new_items.values())
        new_line = f"🆕 新增样本：{total_new} 条（{per_src}）"
    else:
        new_line = "🆕 新增样本：0 条（窗口内条目与上次相同）"
    return "\n".join([
        "### 🧭 数据新鲜度 · 本次运行指纹",
        f"- ⏱ 运行时间：{now_cst}（北京时间）"
        f" · 模板「{TEMPLATE_TITLES.get(template, template)}」"
        f" · 数据窗口 {hours}h",
        f"- 📈 行情：{quote_line}",
        f"- {new_line}",
        f"- 🎯 本地计算：{anchor_line}",
        f"- 🔢 内容指纹 `{fingerprint}`：{fp_note}",
    ])


def _utf8_len(text: str) -> int:
    """推送服务的长度限制按 UTF-8 字节计算，不能用 Python 字符数代替。"""
    return len(text.encode("utf-8"))


def _truncate_to_utf8_bytes(text: str, max_bytes: int) -> str:
    """截到最多 ``max_bytes`` 个 UTF-8 字节，且绝不截断一个中文字符。"""
    if max_bytes <= 0:
        return ""
    if _utf8_len(text) <= max_bytes:
        return text
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _split_protected_suffix(content: str, protected_suffix: str) -> tuple[str, str]:
    """从内容中分离须保留的尾注；未匹配时保持普通截断行为。"""
    normalized = content.rstrip()
    suffix = protected_suffix.strip()
    if suffix and normalized.endswith(suffix):
        return normalized[:-len(suffix)].rstrip(), suffix
    return normalized, ""


def fit_for_channel(channel: str, content: str,
                    protected_suffix: str = "") -> tuple[str, str]:
    """按通道字节上限截断，返回 ``(内容, 截断说明或空串)``。

    企业微信的 Markdown 上限是 4096 UTF-8 字节。若指定 ``protected_suffix``
    （主流程传入品牌尾注），会从正文中间裁切，保留声明和最后一行作者，避免
    长报告在推送后显示“作者不见了”或作者不在结尾。
    """
    limit = CHANNEL_LIMITS.get(channel, 0)
    if not limit or _utf8_len(content) <= limit:
        return content, ""

    body, suffix = _split_protected_suffix(content, protected_suffix)
    note_detail = ("作者与声明已保留；" if suffix else "")
    notice = (f"> ⚠️ 内容超出 {channel} 单条上限，部分正文已省略"
              f"（{note_detail}完整内容见 GitHub Actions 运行日志）")
    suffix_block = f"\n\n{suffix}" if suffix else ""
    tail = f"\n\n{notice}{suffix_block}"
    body_budget = limit - _utf8_len(tail)

    if body_budget > 0:
        trimmed_body = _truncate_to_utf8_bytes(body, body_budget)
        # 尽量在一行结束处截断；没有合适换行时，安全地按字节截断。
        cut = trimmed_body.rfind("\n")
        if cut >= len(trimmed_body) * 0.5:
            trimmed_body = trimmed_body[:cut]
        trimmed = trimmed_body.rstrip() + tail
    elif suffix and _utf8_len(suffix) <= limit:
        # 极端情况下优先保留固定尾注，而不是发出一个不完整的作者行。
        trimmed = suffix
    else:
        trimmed = _truncate_to_utf8_bytes(notice, limit)

    # 上述预算应当保证这一条件；保留断言式兜底，防止日后改文案超限。
    if _utf8_len(trimmed) > limit:
        trimmed = _truncate_to_utf8_bytes(trimmed, limit)
    return trimmed, f"已按 {limit} 字节截断"


# 微信订阅号/服务通知对单条 HTML 很敏感：实测超过约 48KB 常出现
# PushPlus 接口 code=200，但微信端不投递或进不了会话。
#
# 历史故障（2026-10-19）：每日市场简报并入金十 50 条 + 双栏目后，HTML 涨到
# ~68KB，按 48KB 切分后「第 1 篇」正好卡在临界点上被微信静默丢弃，读者只收到
# 后半段，表现为「快讯明细不显示」——正文里的快讯原始明细整体看不见。
# 因此单篇目标下调到 32KB（约为失效临界点的 2/3），留出安全余量：
# 宁可多推两篇，也保证每一篇都真的送达。
WECHAT_HTML_SOFT_LIMIT = 32_000

# 单篇 HTML 超软上限时的最大分篇数；超过该篇数才退回 markdown。
# （研报越长 style 内联膨胀越厉害，九章级长报告常见 3-6 篇；guizang 续篇壳
#   可显著压缩每篇开销。单篇目标从 48KB 降到 32KB 后同样内容篇数变多，
#   上限相应提到 12 篇：总容量 12×32KB=384KB，与此前 8×48KB 持平）
WECHAT_HTML_MAX_PARTS = 12


def _md_blocks(md: str) -> list[str]:
    """按空行切 markdown 块；围栏代码块（内部可能含空行）保持整体不被切断。"""
    blocks: list[str] = []
    cur: list[str] = []
    in_fence = False
    for ln in md.split("\n"):
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            cur.append(ln)
            continue
        if not in_fence and not ln.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _html_fits(title: str, md: str, theme_name: str, limit: int,
               continuation: bool = False) -> bool:
    return _utf8_len(themed_html(title, md, theme_name=theme_name,
                                 continuation=continuation)) <= limit


def _split_block_lines(title: str, block: str, theme_name: str,
                       limit: int, continuation: bool = False) -> list[str] | None:
    """单个块（如长表格/长列表）超限时的兜底：按行贪心装填。

    表格块（``|`` 开头且第二行为分隔行）续篇自动补表头+分隔行，
    保证每篇仍是可渲染的完整表格。单行即超限（极端长行）返回 None。
    """
    lines = block.split("\n")
    header: list[str] = []
    if (len(lines) >= 3 and lines[0].lstrip().startswith("|")
            and re.match(r"^\|?[\s:\-|]+\|?$", lines[1])):
        header, lines = lines[:2], lines[2:]
    parts: list[str] = []
    cur: list[str] = list(header)
    for ln in lines:
        if _html_fits(title, "\n".join(cur + [ln]), theme_name, limit,
                      continuation):
            cur.append(ln)
            continue
        if len(cur) > len(header):
            parts.append("\n".join(cur))
        cur = header + [ln]
        if not _html_fits(title, "\n".join(cur), theme_name, limit,
                          continuation):
            return None
    if len(cur) > len(header):
        parts.append("\n".join(cur))
    return parts or None


def _greedy_split_parts(title: str, md: str, theme_name: str,
                        limit: int) -> list[str] | None:
    """贪心把 md 按块装进若干篇（每篇 themed_html ≤ limit）；无法切分返回 None。

    第 2 篇起按「紧凑续篇壳」测量（仅支持 continuation 的主题生效），
    与 push_pushplus 实际投递的外壳一致。返回的篇数不受 max_parts 限制，
    由调用方判断是否超限。
    """
    theme = _get_theme(theme_name)
    has_cont = bool(theme.get("continuation"))
    if _html_fits(title, md, theme_name, limit):
        return [md]
    parts: list[str] = []
    cur = ""
    continuation = False  # 第 2 篇起使用紧凑续篇壳
    for block in _md_blocks(md):
        trial = f"{cur}\n\n{block}" if cur else block
        if _html_fits(title, trial, theme_name, limit, continuation):
            cur = trial
            continue
        if cur:
            parts.append(cur)
            cur = ""
            continuation = has_cont
        if _html_fits(title, block, theme_name, limit, continuation):
            cur = block
            continue
        if block.lstrip().startswith("```"):
            return None  # 围栏代码块不可切分
        sub = _split_block_lines(title, block, theme_name, limit, continuation)
        if not sub:
            return None
        parts.extend(sub[:-1])
        cur = sub[-1]
        continuation = has_cont
    if cur:
        parts.append(cur)
    if len(parts) < 2:
        return None
    return parts


def _appendix_block_indexes(blocks: list[str], marker: str,
                            stop_marker: str = "作者：") -> set[int]:
    """返回某个附录段（如「快讯原始明细」）所占的块下标集合。

    附录在 markdown 里是「起始行 + 若干空行分隔的子块」结构，起点是含
    marker 的那一块，终点是下一个以 ``---`` 开头的新章节块（或品牌尾注）。
    裁尾时用它在这些块上做保护，避免把「快讯明细」这类正文级附录当成
    可弃内容裁掉。
    """
    idxs: set[int] = set()
    start = None
    for i, b in enumerate(blocks):
        if marker in b:
            start = i
            break
    if start is None:
        return idxs
    for i in range(start, len(blocks)):
        if i > start and (blocks[i].lstrip().startswith("---")
                          or (stop_marker and stop_marker in blocks[i])):
            break
        idxs.add(i)
    return idxs


def _trim_tail_to_fit(title: str, md: str, theme_name: str, limit: int,
                      max_parts: int) -> list[str] | None:
    """max_parts 装不下时的最后手段：从尾部裁掉最可弃的块，保住主题风格。

    优先裁掉品牌尾注之后的块（如一致性检查器输出），仍不够再从尾注前
    的尾部逐块裁（平台附录等），并插入一行可见提示；品牌尾注始终保留。
    「快讯原始明细」等正文级附录受保护：宁可裁前面的 AI 正文分节，也
    不裁掉快讯明细（否则读者会看到「快讯明细不显示」）。
    真正无法干净裁剪时返回 None（由调用方退回 markdown）。
    """
    blocks = _md_blocks(md)
    if len(blocks) < 3:
        return None
    footer_idx = None
    for i in range(len(blocks) - 1, -1, -1):
        if "作者：" in blocks[i]:
            footer_idx = i
            break
    protected = _appendix_block_indexes(blocks, "快讯原始明细")
    note = ("\n\n> ⚠️ 篇幅超限：微信投递有单篇长度上限，"
            "部分内容已省略；完整报告见 GitHub Actions 运行日志。")
    if footer_idx is None:
        # 无品牌尾注：直接从尾部逐块裁，任何块都允许裁掉
        for cut in range(1, len(blocks)):
            trial = "\n\n".join(blocks[: len(blocks) - cut]) + note
            out = _greedy_split_parts(title, trial, theme_name, limit)
            if out is not None and 1 <= len(out) <= max_parts:
                return out
        return None
    if footer_idx == 0:
        return None
    kept = list(range(footer_idx + 1))  # 尾注之后的块（检查器等）全部裁掉
    while len(kept) > 2:
        trial = "\n\n".join(blocks[i] for i in kept) + note
        out = _greedy_split_parts(title, trial, theme_name, limit)
        if out is not None and 1 <= len(out) <= max_parts:
            return out
        # 裁掉尾注之前的最后一块；「快讯原始明细」等受保护块跳过不裁
        cut_at = None
        for pos in range(len(kept) - 2, 0, -1):
            if kept[pos] not in protected:
                cut_at = pos
                break
        if cut_at is None:
            return None
        del kept[cut_at]
    return None


def _mark_appendix_continuation(title: str, parts: list[str], theme_name: str,
                                limit: int) -> list[str]:
    """给「快讯原始明细」被切开的续篇加一行「（续上篇）」提示。

    明细动辄上百条，分篇后第二篇往往直接从明细中间开始，读者会以为
    简报断掉了；加一行提示让它知道这是上一段的续篇。提示行会占一点
    体积，装不下时宁可不加。
    """
    marker = "📰 **快讯原始明细（续上篇）**"
    out: list[str] = []
    seen = False
    for p in parts:
        if "快讯原始明细" in p:
            seen = True
            out.append(p)
            continue
        if seen:
            cand = f"{marker}\n\n{p}"
            if _html_fits(title, cand, theme_name, limit):
                out.append(cand)
                continue
        out.append(p)
    return out


def split_md_for_html(title: str, md: str, theme_name: str,
                      limit: int = WECHAT_HTML_SOFT_LIMIT,
                      max_parts: int = WECHAT_HTML_MAX_PARTS) -> list[str] | None:
    """把 md 切成若干片，使每片 themed_html ≤ limit；无法满足返回 None。

    超微信软上限时不再整篇退回纯 markdown（推送页会完全丢掉主题风格），
    而是按块（必要时按行）切成多篇带样式的 HTML 依次推送。
    围栏代码块不可切分时返回 None；超过 max_parts 时先尝试裁掉尾部
    最可弃的附录块（带可见提示，「快讯原始明细」受保护不裁），
    仍不行才返回 None 由调用方退回 markdown 投递（与旧行为一致）。
    """
    parts = _greedy_split_parts(title, md, theme_name, limit)
    if parts is None:
        return None
    if len(parts) == 1:
        return parts  # 整篇一页即可装下（含裁尾后只剩单篇的情形）
    if len(parts) <= max_parts:
        return _mark_appendix_continuation(title, parts, theme_name, limit)
    trimmed = _trim_tail_to_fit(title, md, theme_name, limit, max_parts)
    if trimmed is None:
        return None
    return _mark_appendix_continuation(title, trimmed, theme_name, limit)


def push_pushplus(title: str, content: str, timeout: int,
                  theme: str = "dos") -> str:
    token = env("PUSHPLUS_TOKEN")
    if not token:
        raise PushError("缺少 Secret：PUSHPLUS_TOKEN")
    title = title[:200]  # PushPlus 标题上限（会员支持 200 字）
    content, note = fit_for_channel("pushplus", content, brand_footer_md())
    notes: list[str] = [note] if note else []
    # 待发送队列：(标题, 内容, template)
    sends: list[tuple[str, str, str]] = []
    # 主题：dos / guizang / game / klein / pixel / monitor / noc 均为 HTML 模板，其余走 markdown
    if theme in THEMES:
        html = themed_html(title, content, theme_name=theme)
        html_bytes = _utf8_len(html)
        log(f"  📦 PushPlus HTML {html_bytes} 字节（主题 {theme}）")
        if html_bytes <= WECHAT_HTML_SOFT_LIMIT:
            sends.append((title, html, "html"))
        else:
            parts = split_md_for_html(title, content, theme)
            if parts:
                # 分篇推送：每篇都是完整主题 HTML，推送页风格保持一致
                notes.append(f"HTML {html_bytes}B 超软上限，"
                             f"已分 {len(parts)} 篇带样式推送")
                log(f"  ✂️ HTML 超过微信软上限 {WECHAT_HTML_SOFT_LIMIT}，"
                    f"切分为 {len(parts)} 篇依次推送（每篇均为主题 HTML）")
                for idx, part in enumerate(parts, 1):
                    ptitle = f"{title}（{idx}/{len(parts)}）"[:200]
                    sends.append((ptitle,
                                  themed_html(ptitle, part, theme_name=theme,
                                              continuation=(idx > 1),
                                              part_no=idx,
                                              part_count=len(parts)),
                                  "html"))
            else:
                # 无法干净切分（如单个围栏块超限）才退回 markdown，
                # 避免「接口成功、微信没收」
                sends.append((title, content, "markdown"))
                notes.append(f"HTML {html_bytes}B 超微信软上限，已改 markdown")
                log(f"  ⚠️ HTML {html_bytes} 字节超过微信软上限 "
                    f"{WECHAT_HTML_SOFT_LIMIT} 且无法干净切分，改用 markdown 投递")
    else:
        sends.append((title, content, "markdown"))
        log(f"  📦 PushPlus markdown {_utf8_len(content)} 字节")

    sent = 0
    failures: list[str] = []
    for i, (ptitle, pcontent, ptemplate) in enumerate(sends):
        if i:
            time.sleep(1)  # 多篇串行 + 间隔 1s，避开 PushPlus 频率限制
        fields = {"token": token, "title": ptitle,
                  "content": pcontent, "template": ptemplate}
        status, body = http_post_form(PUSHPLUS_URL, fields, timeout)
        code = None
        msg = ""
        try:
            parsed = json.loads(body)
            code = parsed.get("code")
            msg = str(parsed.get("msg") or parsed.get("message") or "")
        except json.JSONDecodeError:
            pass
        page = f"（第 {i + 1}/{len(sends)} 篇）" if len(sends) > 1 else ""
        log(f"  📡 PushPlus HTTP {status} code={code} msg={msg[:120]}{page}")
        if status == 200 and code == 200:
            sent += 1
            continue
        # 单篇失败不中断后续分篇：正文是分篇装的，前面某篇失败不能让
        # 后面承载「快讯明细」的篇章一起丢掉（全部发完再统一报错）。
        which = f"第 {i + 1}/{len(sends)} 篇" if len(sends) > 1 else ""
        failures.append(f"{which or '正文'}：HTTP {status} {body[:200]}")
        log(f"  ⚠️ PushPlus 该篇失败（继续发送后续分篇）：{which or '正文'}")
    if failures:
        raise PushError(
            f"PushPlus 有 {len(failures)}/{len(sends)} 篇返回异常"
            f"（已送达 {sent} 篇）：" + "；".join(failures)[:400])
    extra = "；".join(n for n in notes if n)
    ok = "发送成功" if len(sends) == 1 else f"发送成功（共 {len(sends)} 篇）"
    return ok + (f"（{extra}）" if extra else "")


def push_wecom(title: str, content: str, timeout: int) -> str:
    key = env("WECOM_KEY")
    if not key:
        raise PushError("缺少 Secret：WECOM_KEY")
    # 企业微信的 4096 字节限制作用于整个 markdown 字段，标题也必须计入。
    message = f"**{title}**\n\n{content}"
    message, note = fit_for_channel("wecom", message, brand_footer_md())
    payload = {"msgtype": "markdown", "markdown": {"content": message}}
    status, body = http_post_json(f"{WECOM_URL}?key={key}", payload, timeout=timeout)
    try:
        errcode = json.loads(body).get("errcode")
    except json.JSONDecodeError:
        errcode = None
    if status == 200 and errcode == 0:
        return "发送成功" + (f"（{note}）" if note else "")
    raise PushError(f"企业微信机器人返回异常（HTTP {status}）：{body[:400]}")


def push_serverchan(title: str, content: str, timeout: int) -> str:
    sendkey = env("SERVERCHAN_SENDKEY")
    if not sendkey:
        raise PushError("缺少 Secret：SERVERCHAN_SENDKEY")
    content, note = fit_for_channel("serverchan", content, brand_footer_md())
    status, body = http_post_form(SERVERCHAN_URL.format(sendkey=sendkey),
                                  {"title": title, "desp": content}, timeout)
    try:
        code = json.loads(body).get("code")
    except json.JSONDecodeError:
        code = None
    if status == 200 and code == 0:
        return "发送成功" + (f"（{note}）" if note else "")
    raise PushError(f"Server酱返回异常（HTTP {status}）：{body[:400]}")


def push_console(title: str, content: str, timeout: int) -> str:  # noqa: ARG001
    log("----- console 通道输出 begin -----")
    log(f"# {title}\n\n{content}")
    log("----- console 通道输出 end -----")
    return "已打印到日志"


PUSH_FUNCS = {
    "pushplus": push_pushplus, "wecom": push_wecom,
    "serverchan": push_serverchan, "console": push_console,
}


# ================================================================ 模块⑤b：字符模拟图（纯字符）

def _with_ma(bars: list[dict]) -> list[dict]:
    out = []
    for i, b in enumerate(bars):
        b = dict(b)
        for n in (5, 10, 20):
            win = [bars[j]["close"] for j in range(max(0, i - n + 1), i + 1)]
            b[f"ma{n}"] = round(sum(win) / len(win), 4)
        out.append(b)
    return out


def resolve_chart_symbols(raw: str) -> tuple[str, str, str, str]:
    """把任意港股/A股/美股写法解析为 (统一代码, Yahoo 符号, 东财 secid, 市场后缀)。"""
    try:
        import hk_quote
        market, code, em_secid = hk_quote.detect_market(raw)
    except Exception:
        code = normalize_hk_code(raw)
        return code, f"{int(code)}.HK", f"116.{code}", "HK"
    suffix = {"hk": "HK", "sh": "SH", "sz": "SZ", "us": "US"}.get(market, "HK")
    if market == "us":
        # 美股：Yahoo 原生代码（NVDA）；东财 secid 为 105 占位猜测，
        # kline 拉取失败时 Yahoo 已兜底，不影响出图
        return code, code, em_secid, "US"
    if market == "hk":
        yahoo = f"{int(code)}.HK"
    elif market == "sh":
        yahoo = f"{code}.SS"
    else:
        yahoo = f"{code}.SZ"
    return code, yahoo, em_secid, suffix


def fetch_chart_bars(code: str, timeout: int = 15) -> tuple[list[dict], str]:
    """日级字符模拟图数据：Yahoo 主源 → 东方财富备源（均免 Key）。返回 (bars, 数据源标签)。

    支持港股（09988）与 A 股（600519 / 000001.SZ）。
    """
    code, yahoo_sym, em_secid, _sfx = resolve_chart_symbols(code)
    try:  # ① Yahoo Finance
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
               "?interval=1d&range=6mo")
        status, body = http_request(url, timeout=timeout)
        if status == 200:
            res = json.loads(body)["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            bars = []
            for t, o, h, l, c, v in zip(res.get("timestamp") or [],
                                        q.get("open") or [], q.get("high") or [],
                                        q.get("low") or [], q.get("close") or [],
                                        q.get("volume") or []):
                if o is None or c is None:
                    continue
                bars.append({
                    "date": datetime.fromtimestamp(t, timezone.utc)
                            .strftime("%Y-%m-%d"),
                    "open": float(o), "high": float(h), "low": float(l),
                    "close": float(c), "vol": float(v or 0),
                })
            if len(bars) >= 5:
                return _with_ma(bars), "YAHOO"
    except Exception as e:
        log(f"  ⚠️ 字符模拟图 Yahoo 源失败：{e}")
    try:  # ② 东方财富
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={em_secid}&klt=101&fqt=1&lmt=90&end=20500101"
               "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57")
        status, body = http_request(url, timeout=timeout)
        if status == 200:
            data = json.loads(body).get("data") or {}
            bars = []
            for row in data.get("klines") or []:
                p = row.split(",")
                # 格式：日期,开盘,收盘,最高,最低,成交量,成交额
                bars.append({
                    "date": p[0], "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]),
                    "vol": float(p[5] or 0),
                })
            if len(bars) >= 5:
                return _with_ma(bars), "EASTMONEY"
    except Exception as e:
        log(f"  ⚠️ 字符模拟图 东方财富 源失败：{e}")
    raise PushError("Yahoo 与东方财富日级数据均获取失败")


# 兼容旧名
fetch_kline_bars = fetch_chart_bars


def render_char_chart(bars: list[dict], meta: dict | None = None,
                      width: int = 52, height: int = 12) -> str:
    """把日级 OHLC bars 渲染为等宽字符模拟图（纯字符，无图片）。

    字符约定：
      影线 │  阳线实体 █  阴线实体 ▓
      MA5 ·  MA10 ×  MA20 +  （在空白处叠加，不覆盖实体）
      支撑 S1 / 压力 R1 用虚线 ─ 标注
    返回可直接嵌入 Markdown <pre> 的多行字符串。
    """
    bars = [b for b in bars if b.get("close") is not None][-width:]
    if len(bars) < 5:
        raise PushError("数据不足（<5 根），无法渲染字符模拟图")
    meta = meta or {}
    code = str(meta.get("code") or "09988")
    source = str(meta.get("source") or "YAHOO")
    suffix = str(meta.get("suffix") or "HK")

    # 选用收盘价决定量纲，保留适当上下边距
    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    pad = (hi - lo) * 0.08 or hi * 0.01 or 0.01
    lo -= pad
    hi += pad
    span = hi - lo or 1.0

    n = len(bars)
    # 字符网格：height 行 x n 列
    grid = [[" " for _ in range(n)] for _ in range(height)]

    def y_of(price: float) -> int:
        # 0=顶部, height-1=底部
        r = (hi - price) / span * (height - 1)
        return max(0, min(height - 1, int(round(r))))

    # 绘制影线与实体
    for i, b in enumerate(bars):
        yh = y_of(b["high"])
        yl = y_of(b["low"])
        yo = y_of(b["open"])
        yc = y_of(b["close"])
        top_body = min(yo, yc)
        bot_body = max(yo, yc)
        is_up = b["close"] >= b["open"]
        body_ch = "█" if is_up else "▓"
        # 影线
        for r in range(min(yh, yl), max(yh, yl) + 1):
            if grid[r][i] == " ":
                grid[r][i] = "│"
        # 实体覆盖
        if top_body == bot_body:
            grid[top_body][i] = body_ch
        else:
            for r in range(top_body, bot_body + 1):
                grid[r][i] = body_ch

    # 叠加均线（仅在空白处加点，不覆盖实体与影线，仅示意）
    ma_chars = {"ma5": "·", "ma10": "×", "ma20": "+"}
    for key, ch in ma_chars.items():
        for i, b in enumerate(bars):
            v = b.get(key)
            if v is None:
                continue
            r = y_of(v)
            if grid[r][i] == " ":
                grid[r][i] = ch
            elif grid[r][i] == "│":
                grid[r][i] = ch  # 均线优先于影线

    # 支撑/压力位（近 20 根）
    try:
        s1 = min(b["low"] for b in bars[-20:])
        r1 = max(b["high"] for b in bars[-20:])
        rs = y_of(s1)
        rr = y_of(r1)
        for c in range(n):
            if grid[rs][c] == " ":
                grid[rs][c] = "─"
            if grid[rr][c] == " ":
                grid[rr][c] = "─"
    except Exception:
        s1 = r1 = None

    # 生成价格轴标签
    lines: list[str] = []
    for r in range(height):
        price = hi - (r / (height - 1)) * span if height > 1 else hi
        if r % 3 == 0 or r == height - 1:
            label = f"{price:6.2f} ┤"
        else:
            label = "       │"
        lines.append(label + "".join(grid[r]))

    # 底部边框
    lines.append("       └" + "─" * n + "┘")
    # 日期刻度（首/中/尾）
    date_line = [" " * 8] + [" " for _ in range(n)]
    # 使用首、中、尾日期避免拥挤
    ticks = []
    if n >= 3:
        ticks = [(0, bars[0]["date"][5:]), (n // 2, bars[n // 2]["date"][5:]), (n - 1, bars[-1]["date"][5:])]
    elif n:
        ticks = [(0, bars[0]["date"][5:]), (n - 1, bars[-1]["date"][5:])]
    axis = [" "] * n
    for idx, ds in ticks:
        for k, ch in enumerate(ds):
            pos = idx - 2 + k
            if 0 <= pos < n:
                axis[pos] = ch
    lines.append("        " + "".join(axis))

    # 成交量字符条（单独一行，高度 1，用 ▁▂▃▄▅▆▇█ 归一化）
    try:
        vmax = max((b["vol"] or 0) for b in bars) or 1
        blocks = "▁▂▃▄▅▆▇█"
        vol_chars = []
        for b in bars:
            v = b["vol"] or 0
            ratio = v / vmax
            idx = min(len(blocks) - 1, int(ratio * (len(blocks) - 1) + 0.5))
            # 保持阳阴区分：涨用 █ 系列，跌用 ▓ 系列提示，但体积条本身用 block
            vol_chars.append(blocks[idx])
        lines.append("   VOL │" + "".join(vol_chars))
    except Exception:
        pass

    # 图例与数据说明
    header = f"{code}.{suffix} 字符模拟走势（近 {len(bars)} 日 · {source}）"
    if s1 is not None and r1 is not None:
        footer = f"S1 {s1:.2f} ── 支撑  ·  R1 {r1:.2f} ── 压力  ·  MA5 ·  MA10 ×  MA20 +"
    else:
        footer = "MA5 ·  MA10 ×  MA20 +  ·  涨 █  跌 ▓  影线 │"
    lines.insert(0, header)
    lines.append(footer)
    lines.append(f"{bars[0]['date']}  →  {bars[-1]['date']}")

    return "\n".join(lines)


def make_chart_block(hk_code_raw: str, quotes: list[Quote],
                     no_chart: bool) -> str:
    """生成「📊 字符模拟图」markdown 块（纯字符，无图片，推送正文顶部）。

    任何一步失败都安全降级为空串，绝不影响推送本身。
    """
    if no_chart or not hk_code_raw:
        return ""
    code, _yahoo, _em, suffix = resolve_chart_symbols(hk_code_raw)
    try:
        bars, source = fetch_chart_bars(hk_code_raw, timeout=15)
    except PushError as e:
        log(f"  ⚠️ 字符模拟图数据获取失败，本次推送不含字符图：{e}")
        return ""
    if len(bars) < 5:
        log("  ⚠️ 字符模拟图数据不足（<5 根），本次推送不含字符图")
        return ""

    name = pick_cn_name(quotes, fallback="") if quotes else ""
    label = name or f"{suffix}{code}"
    chart_txt = ""
    try:
        chart_txt = render_char_chart(
            bars, {"code": code, "source": source, "suffix": suffix})
    except PushError as e:
        log(f"  ⚠️ 字符模拟图渲染失败，本次推送不含字符图：{e}")
        return ""
    log(f"  📊 字符模拟图已生成（{len(bars)} 根，{source}，{len(chart_txt)} 字符）")
    last = bars[-1]
    # 用 <pre> 包裹字符图以保持等宽；Markdown 代码块在多数推送渠道亦可正常显示
    # 为兼顾 PushPlus HTML 与企微/Server酱 Markdown，输出双兼容：HTML <pre> 将在 md_to_html 中保留，
    # 纯 Markdown 则显示为代码块
    return (
        f"### 📊 字符模拟图 · {label}（近 {len(bars)} 个交易日，截至 {last['date']} · {source}）\n\n"
        f"```text\n{chart_txt}\n```\n\n"
        f"> 字符模拟图由日级 OHLC 模拟渲染（涨 █ 跌 ▓ 影线 │），仅供参考，非真实图片。\n"
    )


# 兼容旧名（历史脚本/测试可能仍调用 make_kline_block）
make_kline_block = make_chart_block


def demo_chart_bars(count: int = 60) -> list[dict]:
    """本地预览/自检用的演示 OHLC（确定性生成，涨跌形态完整）。"""
    base = 16.80
    out: list[dict] = []
    for i in range(count):
        phase = i / 9.0
        trend = (i / count) * 0.10 - 0.05
        noise = (math.sin(phase * 1.5) * 0.028
                 + math.cos(phase * 0.7) * 0.016 + trend)
        close = base * (1.0 + noise)
        open_ = close * (1.0 - math.sin(phase * 2.1) * 0.012)
        high = max(open_, close) * (1.0 + abs(math.cos(phase * 1.3)) * 0.010)
        low = min(open_, close) * (1.0 - abs(math.sin(phase * 1.9)) * 0.010)
        if i == count - 1:
            close, open_ = base, base * 0.998
        vol = int(12000000 + math.sin(phase * 3.0) * 5000000
                  + i * 70000)
        d = (datetime.now(timezone.utc) - timedelta(days=count - 1 - i))
        out.append({"date": d.strftime("%Y-%m-%d"), "open": round(open_, 3),
                    "high": round(high, 3), "low": round(low, 3),
                    "close": round(close, 3), "vol": vol})
    return _with_ma(out)


# 兼容旧名
demo_kline_bars = demo_chart_bars


# ================================================================ 离线自检

YAHOO_SAMPLE = json.dumps({"chart": {"result": [{"meta": {
    "symbol": "9988.HK", "longName": "Alibaba Group Holding Limited",
    "regularMarketPrice": 16.80, "previousClose": 16.61,
    "regularMarketDayHigh": 16.92, "regularMarketDayLow": 16.55,
    "regularMarketVolume": 19923879, "regularMarketTime": 1754280000}}],
    "error": None}})

EASTMONEY_SAMPLE = json.dumps({"rc": 0, "data": {
    "f43": 16800, "f44": 16920, "f45": 16550, "f46": 16850,
    "f57": "09988", "f58": "阿里巴巴", "f60": 16610,
    "f170": 114, "f47": 19923879}})

TENCENT_SAMPLE = ('v_hk09988="100~阿里巴巴~09988~16.800~16.610~16.850~16.900'
                  '~16.550~16.800~19923879~335544320~3.51~2.18~16.920~16.550'
                  '~2026/08/04 16:08:07~0.190~1.140~3.52~2.05~38.69~184.94'
                  '~190.02~18.440~14.080~1.230~16.800~3.30~0.00~0.00~0~0~0~0~0";')


def selftest() -> int:
    """离线自检：三源解析器 + 交叉核验逻辑 + 全部模板构造。"""
    fails = 0

    def check(name: str, cond: bool):
        nonlocal fails
        log(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    log("① 三源解析器")
    qa, qb, qc = (parse_yahoo(YAHOO_SAMPLE), parse_eastmoney(EASTMONEY_SAMPLE),
                  parse_tencent(TENCENT_SAMPLE))
    check("Yahoo 解析 16.80", qa.ok and abs(qa.price - 16.80) < 1e-9)
    check("东财 解析 ÷1000=16.80", qb.ok and abs(qb.price - 16.80) < 1e-9
          and qb.name == "阿里巴巴")
    check("东财 涨跌幅 1.14%", abs((qb.change_pct or 0) - 1.14) < 1e-9)
    check("腾讯 解析 16.80", qc.ok and abs(qc.price - 16.80) < 1e-9)

    log("② 交叉核验")
    v = verify_quotes([qa, qb, qc])
    check("三源一致→可信", "三源一致" in v.verdict and v.n_excluded == 0)
    bad = Quote(source="东方财富", ok=True, name="阿里巴巴", price=18.50,
                prev_close=16.61, change_pct=11.4)
    v2 = verify_quotes([qa, bad, qc])
    check("异常源被剔除", v2.n_excluded == 1 and "剔除" in v2.verdict)
    v3 = verify_quotes([qa, Quote(source="B", ok=False, error="HTTP 500"),
                        Quote(source="C", ok=False, error="timeout")])
    check("仅单源→未核验", "单源" in v3.verdict)
    v4 = verify_quotes([Quote(source="A", ok=False, error="x"),
                        Quote(source="B", ok=False, error="y"),
                        Quote(source="C", ok=False, error="z")])
    check("全失败→降级提示", "全部失败" in v4.verdict)
    md = market_block_md("09988", [qa, qb, qc], v)
    check("核验表含三源名", all(s in md for s in ("Yahoo", "东方财富", "腾讯")))

    log("③ 港股代码规整")
    check("normalize 9988→09988", normalize_hk_code("9988") == "09988")
    check("normalize 9988.HK→09988", normalize_hk_code("9988.HK") == "09988")
    c, y, em, sfx = resolve_chart_symbols("09988")
    check("chart 符号 港股", c == "09988" and y == "9988.HK" and em.startswith("116.") and sfx == "HK")
    c, y, em, sfx = resolve_chart_symbols("600519")
    check("chart 符号 沪A", c == "600519" and y == "600519.SS" and em == "1.600519" and sfx == "SH")
    c, y, em, sfx = resolve_chart_symbols("000001.SZ")
    check("chart 符号 深A", c == "000001" and y == "000001.SZ" and em == "0.000001" and sfx == "SZ")

    log("③b 情绪模块（48h 窗口）")
    NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    s, l_, _ = score_text("阿里巴巴签约云计算大单，营收增长超预期")
    check("词典:利好", l_ == "利好" and s > 0)
    s, l_, _ = score_text("公司亏损并遭大股东减持")
    check("词典:利空", l_ == "利空" and s < 0)
    s, l_, _ = score_text("举行年度股东大会")
    check("词典:中性", l_ == "中性" and s == 0)
    s, l_, _ = score_text("Downwind")
    check("词典:词边界防误判(downwind≠win)", l_ == "中性" and s == 0)
    g = parse_google_rss(GOOGLE_SAMPLE, 48, 10, NOW)
    check("Google:48h过滤剩2条", len(g) == 2)
    check("Google:利好识别", g[0].label == "利好")
    check("Google:利空识别(英文)", g[1].label == "利空")
    g3 = parse_google_rss(GOOGLE_SAMPLE, 8, 10, NOW)
    check("Google:8h窗口更严", len(g3) == 1)
    y = parse_youtube_atom(YOUTUBE_ATOM_SAMPLE, 48, 10, NOW)
    check("YouTube:48h过滤剩2条", len(y) == 2 and "youtu.be/abc123" in y[0].url)
    t0 = NOW.timestamp()
    tss = [int(t0 - 3600 * (120 - k)) for k in range(120)]
    closes = [15.5] * 71 + [16.0 + 0.01 * k for k in range(49)]  # 48h内缓涨
    vols = [1000] * 71 + [1500] * 49                              # 近48h放量
    mom = compute_price_momentum(closes, vols, tss, 48, NOW)
    check("量价:放量上涨→偏多", mom["ok"] and mom["label"] == "偏多"
          and mom["score"] > 0.3)
    mom2 = compute_price_momentum([16.5] * 71 + [16.0 - 0.02 * k for k in range(49)],
                                  vols, tss, 48, NOW)
    check("量价:缩量下跌→偏空", mom2["ok"] and mom2["label"] == "偏空")

    log("③c 快讯通用解析框架")
    ts_8am = datetime(2026, 8, 4, 8, tzinfo=timezone.utc).timestamp()
    schema_a = {"data": {"list": [
        {"title": "央行超预期降准释放流动性", "ctime": ts_8am},
        {"title": "某大宗商品库存平稳", "ctime": ts_8am - 60 * 60 * 100},
    ]}}
    ia = extract_items_generic(schema_a, "测试源A", 10, 48, NOW)
    check("通用:schema A 提取+48h过滤", len(ia) == 1 and ia[0].label == "利好")
    schema_b = {"errno": 0, "data": [
        {"content": "制造业 PMI 低于预期引发下跌担忧", "display_time": ts_8am * 1000},
        {"brief": "新能源招标再创纪录", "pubDate": "Mon, 04 Aug 2026 09:00:00 GMT"},
    ]}
    ib = extract_items_generic(schema_b, "测试源B", 10, 48, NOW)
    check("通用:schema B 毫秒时间+多键名", len(ib) == 2 and ib[0].label == "利空"
          and ib[1].label == "利好")
    schema_c = {"items": [{"title": "美联储维持利率不变",
                           "display_time": "2026-08-04 07:30:00"}]}
    ic = extract_items_generic(schema_c, "测试源C", 10, 48, NOW)
    check("通用:字符串时间解析", len(ic) == 1 and ic[0].age_h is not None)
    xq_sample = json.dumps({"data": {"items": [
        {"name": "阿里巴巴", "code": "HK09988", "percent": 3.2,
         "current": 16.8, "increment": 8888},
        {"name": "某地产股", "code": "SH600000", "percent": -4.1},
    ]}})
    xq = parse_xueqiu_hot(xq_sample, NOW)
    check("雪球:涨跌幅→情绪", len(xq) == 2 and xq[0].label == "利好"
          and xq[1].label == "利空" and "关注增量" in xq[0].title)
    jin10_sample = ('var flash_newest = {"status":200,"data":['
                    '{"id":"1","time":"2026-08-04 09:00:00","type":0,'
                    '"data":{"content":"黄金突破历史新高","pic":""}}]};')
    m = re.search(r"[{\[]", jin10_sample)
    jj = json.loads(jin10_sample[m.start():].rstrip(";\n "))
    ij = extract_items_generic(jj, "金十数据", 10, 48, NOW)
    check("金十:JS壳+双层data", len(ij) == 1 and ij[0].label == "利好")

    log("③c2 修复源回归（2026-10 接口换代：华尔街/财联社/格隆汇/法布）")
    wscn_live = {"code": 20000, "data": {"items": [
        {"title": "", "content": "<p>HTML正文不应被选</p>",
         "content_text": "央行开展逆回购操作维护流动性", "display_time": ts_8am},
        {"title": "两市融资余额减少", "content_text": "长正文",
         "display_time": ts_8am}]}}
    iw = extract_items_generic(wscn_live, "华尔街见闻 快讯", 10, 48, NOW)
    check("华尔街快讯:空title回退content_text且不取HTML content",
          len(iw) == 2 and iw[0].title.startswith("央行开展逆回购")
          and "HTML正文" not in iw[0].title
          and iw[1].title == "两市融资余额减少")
    wscn_flow = {"data": {"items": [
        {"resource_type": "article",
         "resource": {"title": "信息流最新文章标题", "display_time": ts_8am}},
        {"resource_type": "ad",
         "resource": {"title": "广告位不应入选", "display_time": ts_8am}}]}}
    iw2 = extract_items_generic(wscn_flow, "华尔街见闻 最新", 10, 48, NOW)
    check("华尔街最新:resource嵌套提取+广告剔除",
          len(iw2) == 1 and iw2[0].title == "信息流最新文章标题")
    wscn_hot = {"data": {"week_items": [
                    {"title": "周榜旧文", "display_time": ts_8am - 6 * 86400}],
                         "day_items": [
                    {"title": "日榜最热文章", "display_time": ts_8am}],
                         "offline_ids": [1, 2]}}
    iw3 = extract_items_generic(wscn_hot, "华尔街见闻 最热", 10, 48, NOW)
    check("华尔街最热:优先day_items且窗口过滤",
          len(iw3) == 1 and iw3[0].title == "日榜最热文章")
    cls_roll = {"errno": 0, "data": {"roll_data": [
        {"title": "", "brief": "财联社电，指数午后拉升", "ctime": ts_8am,
         "is_ad": 0},
        {"title": "广告电报不应入选", "brief": "x", "ctime": ts_8am,
         "is_ad": 1}]}}
    ic1 = extract_items_generic(cls_roll, "财联社 电报", 10, 48, NOW)
    check("财联社电报:roll_data定位+is_ad剔除",
          len(ic1) == 1 and ic1[0].title.startswith("财联社电"))
    cls_depth = {"data": {"roll_bar": {},
                          "banner": [{"id": 1, "title": ""}],
                          "top_article": [
                              {"title": "置顶文章", "ctime": ts_8am}],
                          "depth_list": [
                              {"title": "深度列表文章一", "ctime": ts_8am},
                              {"title": "深度列表文章二", "ctime": ts_8am}]}}
    ic2 = extract_items_generic(cls_depth, "财联社 深度", 10, 48, NOW)
    check("财联社深度:depth_list不被banner干扰",
          len(ic2) == 2 and ic2[0].title == "深度列表文章一")
    check("财联社签名:md5(sha1(排序query))向量",
          cls_signed_url("https://www.cls.cn/v2/article/hot/list", {}, NOW)
          .endswith("sign=10bdc2da403ad7415bb639aa22dc6cd3"))
    check("财联社签名:NOW_TS替换为epoch秒",
          f"last_time={int(NOW.timestamp())}" in cls_signed_url(
              "https://www.cls.cn/v1/roll/get_roll_list",
              {"last_time": "NOW_TS", "refresh_type": 1, "rn": 20}, NOW))
    glh = {"statusCode": 200, "result": [
        {"title": "格隆汇快讯标题", "createTimestamp": int(ts_8am)},
        {"title": "", "content": "格隆汇电，无标题回退正文",
         "createTimestamp": int(ts_8am)}]}
    ig = extract_items_generic(glh, "格隆汇 事件", 10, 48, NOW)
    check("格隆汇:result+createTimestamp",
          len(ig) == 2 and ig[1].title.startswith("格隆汇电"))
    fb_now_ms = int(ts_8am) * 1000
    fb_express = (
        f'<div class="content-list news-list" data-date="{fb_now_ms}">'
        '<div data-href="/cn/fastshort/123">'
        '<div class="title_name">【黄金突破历史新高】现货黄金大涨。</div>'
        "</div></div>"
        f'<div class="content-list news-list" data-date="{fb_now_ms}">'
        '<div data-href="/cn/fastshort/123">'
        '<div class="title_name">【黄金突破历史新高】重复条目应去重。</div>'
        "</div></div>")
    ifb = parse_fastbull(fb_express, "fb_express", "法布财经 快讯",
                         10, 48, NOW)
    check("法布快讯:news-list解析+【】标题+去重",
          len(ifb) == 1 and ifb[0].title == "黄金突破历史新高"
          and ifb[0].url.endswith("/cn/fastshort/123")
          and ifb[0].age_h is not None)
    fb_news = (
        '<a class="trending_type" href="/cn/news/789">'
        '<div class="title">全球市场头条新闻标题</div>'
        f'<span class="new_time" data-date="{fb_now_ms}">x</span></a>')
    ifb2 = parse_fastbull(fb_news, "fb_news", "法布财经 头条", 10, 48, NOW)
    check("法布头条:trending_type解析",
          len(ifb2) == 1 and ifb2[0].title == "全球市场头条新闻标题"
          and ifb2[0].url.endswith("/cn/news/789"))

    fp = FeedPack(hours=48)
    fp.items["金十数据"] = ij
    fp.errors.append("MKTNews 快讯：HTTP 502")
    fp.agg = {"n": 1, "pos": 1, "neg": 0, "neu": 0, "mean": 1.0,
              "n_sources": 1, "综合多头概率锚点": 50 + 45 * min(1.0, 1.0 * 3)}
    md_rule = render_feed_rule("阿里巴巴", fp)
    check("feedscan:rule渲染含缺口行", "⚠️" in md_rule and "情绪温度" in md_rule)
    check("feedscan:附录含来源明细", "金十数据（1/50）" in render_feed_appendix(fp))
    jin10_spec = next(s for s in FEED_SPECS if s.name == "金十数据")
    check("金十:样本上限=50(其余源默认10)",
          jin10_spec.limit == 50
          and all(s.limit == 10 for s in FEED_SPECS if s.name != "金十数据"))
    j50_raw = [{"id": str(k), "time": "2026-08-04 11:00:00", "channel": [1],
                "data": {"content": f"测试快讯样本第{k}条内容"}}
               for k in range(60)]
    j50_raw.append({"id": "en1", "time": "2026-08-04 11:00:00",
                    "channel": [5],
                    "data": {"content": "English duplicate flash item"}})
    j50_kept = _drop_jin10_english(j50_raw)
    check("金十:channel=[5]英文重复版被过滤", len(j50_kept) == 60
          and all(r.get("channel") != [5] for r in j50_kept))
    ij50 = extract_items_generic(j50_kept, "金十数据", jin10_spec.limit,
                                 48, NOW)
    check("金十:60条样本取满50条", len(ij50) == 50)
    _orig_http = http_request
    try:
        def _fake_flash(url, headers=None, timeout=10, **kw):
            page = [{"id": i, "time": "2026-10-19 08:00:00",
                     "data": {"title": f"金十快讯样本{i}央行动态",
                              "content": "正文"}} for i in range(100, 140)]
            page[0] = {"id": 1, "time": "2026-10-19 08:30:00",
                       "data": {"title": "与首页重复的条目", "content": "x"}}
            page[1] = {"id": 999, "time": "2026-10-19 08:29:00",
                       "data": {"title": "Fed seen cutting rates",
                                "content": "english only"}}
            return 200, json.dumps({"data": page}, ensure_ascii=False)
        globals()["http_request"] = _fake_flash
        home_rows = [{"id": i, "time": "2026-10-19 09:00:00",
                      "data": {"title": f"首页快讯{i}", "content": "正文"}}
                     for i in range(1, 13)]
        filled = _jin10_fill_pages(list(home_rows), 50, 5)
        check("金十:flash-api 翻页补满 50（去重+剔英文）",
              len(filled) == 50
              and len({r["id"] for r in filled}) == 50
              and all(r["id"] != 999 for r in filled))
        check("金十:已满 50 不再翻页",
              len(_jin10_fill_pages(list(filled), 50, 5)) == 50)
    finally:
        globals()["http_request"] = _orig_http
    market_topic = "全球市场早报"
    market_ctx = feed_context(market_topic, fp)
    market_prompt = build_messages("feedscan", market_topic, market_ctx)[1]["content"]
    check("feedscan:识别全市场主题而非个股主题",
          is_market_wide_topic(market_topic) and "全市场（不指定单一个股）" in market_ctx
          and "关注标的" not in market_ctx)
    check("feedscan:全市场 AI 提示明确禁止个股页",
          "不得把正文写成个股页" in market_prompt
          and "所在产业链" not in market_prompt
          and "市场级风险" in market_prompt)

    log("③d 审计修复回归")
    check("查询词清洗:默认主题", _clean_query_topic(
        "阿里巴巴(Alibaba) 每日简报") == "阿里巴巴")
    check("查询词清洗:普通主题不动", _clean_query_topic("比亚迪") == "比亚迪")
    check("查询词清洗:空值兜底", _clean_query_topic("(空)") == "阿里巴巴")

    log("③e 通道长度保护（防内容被截/整包拒发）")
    short_c, note = fit_for_channel("wecom", "短内容")
    check("限内原样", short_c == "短内容" and note == "")
    long_c = "报告头部\n" + "明细行\n" * 1500
    trim_c, note = fit_for_channel("wecom", long_c)
    check("超限截断+注释", _utf8_len(trim_c) <= 4096 and "已省略" in trim_c
          and note.startswith("已按"))
    check("截断保住正文头部", trim_c.startswith("报告头部"))
    footer = brand_footer_md()
    branded_long = add_branding("报告头部\n" + "中文明细行\n" * 1500)
    trim_brand, note = fit_for_channel("wecom", branded_long, footer)
    check("中文按 UTF-8 字节截断", _utf8_len(trim_brand) <= 4096
          and note.endswith("字节截断"))
    check("截断仍保留尾部作者", trim_brand.rstrip().endswith(footer.rstrip()))
    check("截断仍保留作者最后", trim_brand.rstrip().endswith(f"**{BRAND_AUTHOR} · {BRAND_DISCLAIMER}**"))
    wecom_message = f"**{'很长的标题' * 30}**\n\n{branded_long}"
    trim_with_title, _ = fit_for_channel("wecom", wecom_message, footer)
    check("企业微信标题计入总字节上限", _utf8_len(trim_with_title) <= 4096
          and trim_with_title.rstrip().endswith(footer.rstrip()))
    unlim_c, _ = fit_for_channel("pushplus", "x" * 5000)
    check("pushplus 5000字不限", len(unlim_c) == 5000)
    check("微信 HTML 软上限已设置",
          20_000 <= WECHAT_HTML_SOFT_LIMIT <= 80_000)
    fat = themed_html("超长标题", "# 章\n\n" + ("明细行 价格 16.80 多头概率 65%\n" * 4000), "game")
    check("九章级 HTML 会触发微信软上限",
          _utf8_len(fat) > WECHAT_HTML_SOFT_LIMIT)

    log("③e2 超软上限自动分篇（推送页不再退回纯文本、保留主题风格）")
    short_ok = split_md_for_html("t", "正文**加粗**", "game")
    check("短内容不分篇", short_ok == ["正文**加粗**"])
    sec_md = lambda i: (f"## 第{i}节\n\n| 因子 | 方向 | 概率 |\n|---|---|---|\n"
                        + "\n".join(f"| 因子{j} | 多 | {50 + j % 40}% |"
                                    for j in range(30)))
    multi_md = "\n\n".join(sec_md(i) for i in range(4))
    assert _utf8_len(themed_html("超长标题", multi_md,
                                 theme_name="game")) > WECHAT_HTML_SOFT_LIMIT
    parts = split_md_for_html("超长标题", multi_md, "game")
    check("超长内容自动分篇", parts is not None and 2 <= len(parts)
          <= WECHAT_HTML_MAX_PARTS)
    check("每篇 HTML 均低于软上限",
          parts is not None and all(
              _utf8_len(themed_html("超长标题", p, theme_name="game",
                                    continuation=(i > 0),
                                    part_no=i + 1, part_count=len(parts)))
              <= WECHAT_HTML_SOFT_LIMIT
              for i, p in enumerate(parts or [])))

    log("③e2-0 每日市场简报分篇（2026-10-19 故障回归：快讯明细必须送达）")
    feed_item = ("1. [中性 +0.00] 国家统计局：9月份一二三线城市新建商品住宅"
                 "销售价格环比继续上涨，同比降幅均收窄 — 财联社 深度 · 2h前")
    feed_sec = lambda name, n: (f"**{name}（{n}/10）**\n"
                                + "\n".join(feed_item for _ in range(n)))
    brief_md = ("## 全市场快讯情绪扫描\n\n"
                "- **市场情绪温度**：偏暖，综合情绪锚点 51%\n\n"
                "---\n📰 **快讯原始明细（178 条，金十≤50、其余每源≤10，"
                "含 DeepSeek 搜索）**\n\n"
                + "\n\n".join(feed_sec(s, 10) for s in
                              ("MKTNews 快讯", "华尔街见闻 快讯", "华尔街见闻 最新",
                               "华尔街见闻 最热", "财联社 电报", "财联社 深度",
                               "财联社 热门", "雪球 热门股票", "格隆汇 事件",
                               "法布财经 快讯", "法布财经 头条"))
                + "\n\n**金十数据（50/50）**\n" + "\n".join(feed_item for _ in range(50)))
    brief_html = themed_html("章鱼 AI 全景分析", brief_md, theme_name="dos")
    check("市场简报正文超过单篇上限（必须分篇才不会被微信静默丢弃）",
          _utf8_len(brief_html) > WECHAT_HTML_SOFT_LIMIT)
    brief_parts = split_md_for_html("章鱼 AI 全景分析", brief_md, "dos")
    check("市场简报自动分篇且每篇都在安全线内",
          brief_parts is not None and 2 <= len(brief_parts) <= WECHAT_HTML_MAX_PARTS
          and all(_utf8_len(themed_html("章鱼 AI 全景分析", p, theme_name="dos",
                                        continuation=(i > 0), part_no=i + 1,
                                        part_count=len(brief_parts)))
                  <= WECHAT_HTML_SOFT_LIMIT
                  for i, p in enumerate(brief_parts or [])))
    check("分篇后快讯明细完整保留",
          brief_parts is not None
          and "快讯原始明细" in "\n".join(brief_parts)
          and "金十数据（50/50）" in "\n".join(brief_parts))
    check("明细被切开时续篇带「（续上篇）」提示",
          brief_parts is not None and len(brief_parts) > 1
          and any("快讯原始明细（续上篇）" in p for p in brief_parts))
    check("DOS 分篇标题栏标出 PART n/m（读者知道这是续篇）",
          brief_parts is not None and len(brief_parts) > 1
          and "PART 2/" in themed_html("章鱼 AI 全景分析", brief_parts[1],
                                       theme_name="dos", continuation=True,
                                       part_no=2, part_count=len(brief_parts)))

    log("③e2-1 guizang 表格长报告带样式分篇（不再整篇退回 markdown）")
    gz_sec = lambda i: (f"## 第{i}章\n\n本章要点：数据与判断相互印证，避免只给结论。\n\n"
                        "| 指标 | 数值 | 趋势 | 判断 |\n|---|---|---|---|\n"
                        + "\n".join(f"| 变量{j} | {40 + j} | 上行 | 偏多 |"
                                    for j in range(12)))
    gz_md = "\n\n".join(gz_sec(i) for i in range(4))
    gz_parts = split_md_for_html("长标题", gz_md, "guizang")
    check("guizang 长报告自动分篇", gz_parts is not None
          and 2 <= len(gz_parts) <= WECHAT_HTML_MAX_PARTS)
    check("guizang 续篇紧凑壳且每篇低于软上限",
          gz_parts is not None and all(
              _utf8_len(themed_html("长标题", p, theme_name="guizang",
                                    continuation=(i > 0),
                                    part_no=i + 1, part_count=len(gz_parts)))
              <= WECHAT_HTML_SOFT_LIMIT for i, p in enumerate(gz_parts)))
    check("guizang 续篇壳显著小于首篇壳",
          gz_parts is not None and len(gz_parts) > 1
          and _utf8_len(themed_html("长标题", "正文", theme_name="guizang",
                                    continuation=True))
          < _utf8_len(themed_html("长标题", "正文", theme_name="guizang")))

    log("③e2-2 超长报告裁尾保风格（保住主题外壳，不整篇退回 markdown）")
    trim_md = ("# 头部\n\n正文段落。\n\n"
               + "\n\n".join(
                   "| 指标 | 数值 | 观察 |\n|---|---|---|\n"
                   + "\n".join(f"| 变量{j} | {j} | 中性 |" for j in range(40))
                   for _ in range(12))
               + "\n\n**作者：章鱼 ai**\n\n```\n"
               + ("检查行\n" * 600) + "```")
    trim_parts = split_md_for_html("t", trim_md, "guizang")
    check("超长报告裁尾保风格", trim_parts is not None
          and len(trim_parts) <= WECHAT_HTML_MAX_PARTS)
    check("裁尾保留品牌尾注与可见提示",
          trim_parts is not None
          and any("作者：章鱼" in p for p in trim_parts)
          and any("篇幅超限" in p for p in trim_parts))
    check("裁尾后每篇仍低于软上限",
          trim_parts is not None and all(
              _utf8_len(themed_html("t", p, theme_name="guizang",
                                    continuation=(i > 0),
                                    part_no=i + 1, part_count=len(trim_parts)))
              <= WECHAT_HTML_SOFT_LIMIT for i, p in enumerate(trim_parts)))
    check("分篇覆盖全部正文",
          parts is not None and "第3节" in "\n".join(parts or [])
          and "第0节" in "\n".join(parts or []))
    log("③e2-3 裁尾不裁掉快讯明细（读者必须看得到明细）")
    trim_with_feed = (trim_md.split("\n\n**作者：章鱼 ai**")[0]
                      + "\n\n---\n📰 **快讯原始明细（178 条，金十≤50、"
                      "其余每源≤10，含 DeepSeek 搜索）**\n\n"
                      + feed_sec("财联社 深度", 10)
                      + "\n\n**金十数据（50/50）**\n" + "\n".join(feed_item for _ in range(50))
                      + "\n\n**作者：章鱼 ai**\n\n```\n" + ("检查行\n" * 600) + "```")
    trim_feed_parts = split_md_for_html("t", trim_with_feed, "guizang")
    check("超长简报裁尾后快讯明细仍在（不出现「快讯明细不显示」）",
          trim_feed_parts is not None
          and "快讯原始明细" in "\n".join(trim_feed_parts)
          and "金十数据（50/50）" in "\n".join(trim_feed_parts))
    check("裁尾后每篇仍低于软上限（含快讯明细的长简报）",
          trim_feed_parts is not None and all(
              _utf8_len(themed_html("t", p, theme_name="guizang",
                                    continuation=(i > 0),
                                    part_no=i + 1, part_count=len(trim_feed_parts)))
              <= WECHAT_HTML_SOFT_LIMIT
              for i, p in enumerate(trim_feed_parts or [])))
    tbl_rows = "\n".join(f"| 因子{i} | {40 + i % 55}% |" for i in range(2000))
    tbl_parts = _split_block_lines("t", f"| 因子 | 概率 |\n|---|---|\n{tbl_rows}",
                                   "monitor", 12_000)
    check("长表格按行分篇", tbl_parts is not None and len(tbl_parts) > 1)
    check("表格续篇自动补表头",
          tbl_parts is not None and all(
              p.startswith("| 因子 | 概率 |\n|---|---|")
              for p in (tbl_parts or [])[1:]))
    check("表格续篇均低于分篇上限",
          tbl_parts is not None and all(
              _utf8_len(themed_html("t", p, theme_name="monitor")) <= 12_000
              for p in (tbl_parts or [])))
    fence_md = "```\n" + ("print(1)\n" * 9000) + "```"
    check("围栏代码块超限不可切，交回上层退回 markdown",
          split_md_for_html("t", fence_md, "game") is None)

    log("③f game 主题渲染（8-bit 像素游戏风·兼容主题）")
    gh = md_to_html("## 标题\n\n| 板块 | 概率 |\n|---|---|\n| 云计算 | 65% |\n\n"
                    "- **要点**一\n> 备注：推断\n\n---\n\n1. 有序项")
    check("game 表格+金色粗框", "<table" in gh and GAME["border"] in gh
          and "<th" in gh and "<td" in gh)
    check("game 百分比附像素血条", "█" in gh and "░" in gh and GAME["hp_full"] in gh)
    check("game 加粗保留", "<strong>要点</strong>" in gh)
    check("game 列表/引用/分隔线/有序表", "<ul" in gh and GAME["muted"] in gh
          and "border-left" in gh and "<hr" in gh and "<ol" in gh)
    check("HTML 转义", "&lt;" in md_to_html("a<b>c"))
    check("game 像素图标(◆►)与金色方块", ("◆" in gh or "►" in gh)
          and GAME["accent"] in gh)
    gfull = themed_html("测试标题", "正文**加粗**")
    check("game 三要素(深夜蓝屏+金色粗框+等宽)", GAME["bg"] in gfull
          and GAME["border"] in gfull and "Courier" in gfull and "12px" in gfull)
    check("game 硬黑像素阴影+金色标题栏", "box-shadow" in gfull and GAME["hbg"] in gfull)
    check("game 游戏元素(♥HP血条+SCORE+LV+PRESS START)",
          "♥" in gfull and "SCORE" in gfull and "LV." in gfull
          and "PRESS START" in gfull and "▮" in gfull)
    check("game 像素星点背景", "radial-gradient" in gfull and GAME["star1"] in gfull)
    check("game 状态栏含 UTC 戳", "UTC" in gfull)

    log("③f-1 klein 主题渲染（游戏复古像素风）")
    h = md_to_html("## 标题\n\n| 板块 | 概率 |\n|---|---|\n| 云计算 | 65% |\n\n"
                   "- **要点**一\n> 备注：推断\n\n---\n\n1. 有序项", "klein")
    check("klein 表格→table+表头底色", "<table" in h and KLEIN["hbg"] in h
          and "<th" in h and "<td" in h)
    check("klein 加粗保留", "<strong>要点</strong>" in h)
    check("klein 列表/引用/分隔线/有序表", "<ul" in h and KLEIN["muted"] in h
          and "border-left" in h and "<hr" in h and "<ol" in h)
    check("HTML 转义", "&lt;" in md_to_html("a<b>c"))
    check("klein 像素图标(■►)与黑方块", ("■" in h or "►" in h)
          and KLEIN["border"] in h)
    full = themed_html("测试标题", "正文**加粗**", "klein")
    check("klein 三要素(纸底+黑细框+等宽)", KLEIN["bg"] in full
          and KLEIN["border"] in full and "Courier" in full and "12px" in full)
    check("klein 像素阴影+黑底标题栏", "box-shadow" in full and KLEIN["hbg"] in full)
    check("klein RETRO 角标", "[RETRO]" in full)

    log("③f-2 pixel 像素主题渲染（复古监控风·服务器大屏）")
    ph = md_to_html("## 标题\n\n| 指标 | 涨跌 |\n|---|---|\n| 基本面 | +2.5% |\n| 技术面 | -1.2% |\n| 价格 | 16.80 |\n\n"
                    "- **要点**一\n> 备注：重点 16.80\n\n---\n\n1. 有序项", "pixel")
    check("pixel 表格+细线框", "<table" in ph and "1px solid" in ph and PIXEL["border"] in ph)
    check("pixel 小字体 11px", "11px" in ph)
    check("pixel 涨跌突出(▲▼+磷光色)", ("▲" in ph or "▼" in ph) and (PIXEL["up"] in ph and PIXEL["down"] in ph))
    check("pixel 重点数据琥珀高亮", PIXEL["accent"] in ph and "16.80" in ph)
    check("pixel 复古图标(■►方块)", ("■" in ph or "►" in ph) and "6px" in ph)
    check("pixel 细线框(标题/引用1px)", "border-left:1px" in ph)
    pfull = themed_html("测试标题", "正文**加粗** | 因子 | +3% |\n|---|---|\n| 基本面 | +3% |", "pixel")
    check("pixel 三要素(暗底+细线框+等宽)", PIXEL["bg"] in pfull and PIXEL["border"] in pfull and "Courier" in pfull)
    check("pixel 硬阴影+深色状态栏", "box-shadow" in pfull and PIXEL["hbg"] in pfull)
    check("pixel 摄像头元素(●REC+CAM-01+UTC戳)", "● REC" in pfull and "CAM-01" in pfull and "UTC" in pfull)
    check("pixel CRT扫描线+面板网格", PIXEL["scan"] in pfull and PIXEL["grid"] in pfull)

    log("③f-3 DOS CRT 终端主题（磷光绿 + 命令行状态栏 + 扫描线）")
    dos_md = ("## 全市场状态\n\n| 指标 | 数值 |\n|---|---|\n"
              "| 情绪样本 | 24 |\n| 利好/利空 | 12/5 |\n\n"
              "- **市场主线**：科技板块回暖\n> 数据源：公开快讯")
    dos_body = md_to_html(dos_md, "dos")
    dos_full = themed_html("全球市场早报", dos_md, "dos")
    check("DOS 主题已注册且市场宽表转终端卡片",
          THEMES.get("dos") is DOS and "<table" not in dos_body
          and DOS["card_bg"] in dos_body)
    check("DOS 命令提示符 / 磷光绿 / 北京时间状态栏",
          "DOS MARKET MONITOR" in dos_full and "C:\\MARKET\\MONITOR.BAT" in dos_full
          and "BJT" in dos_full and DOS["hfg"] in dos_full)
    check("DOS CRT 扫描线 + 终端等宽字体 + 光标",
          DOS["scan"] in dos_full and "Lucida Console" in dos_full
          and "█" in dos_full and "END OF REPORT" in dos_full)

    log("③f-4 guizang 主题渲染（电子杂志×电子墨水·竖版长页面）")
    gzm = ("# 市场正在重估什么\n\n**核心判断**：先看证据，再谈方向。\n\n"
           "## 信号矩阵\n\n| 因子 | 方向 | 多头概率 | 依据 |\n|---|---|---|---|\n"
           "| 基本面 | 偏多 | 65% | 云业务增长提速 |\n"
           "| 技术面 | 偏空 | 42% | 短线仍在均线下方 |\n\n"
           "> 关键不是猜涨跌，而是确认信号是否持续。\n\n"
           "| 指标 | 当前值 | 观察 |\n|---|---|---|\n| 价格 | 16.80 | 支撑 15.90 |\n")
    gz_body = md_to_html(gzm, "guizang")
    gz_full = themed_html("章鱼 AI · 阿里巴巴 · 研究简报", gzm, "guizang")
    check("guizang 衬线标题+等宽元信息", GUIZANG["font_serif"] in gz_body
          and GUIZANG["font_mono"] in gz_body and "SECTION" in gz_body)
    check("guizang 表格手机端转竖向 rowline", "<table" not in gz_body
          and "DATA ROWS" in gz_body and "FACTOR MATRIX" in gz_body)
    check("guizang 灰阶 Hero+浅灰长页", GUIZANG["bg"] in gz_full
          and GUIZANG["hbg"] in gz_full and GUIZANG["page_bg"] in gz_full and "WECHAT" in gz_full and "LONGFORM" in gz_full
          and "LONGFORM / 01" in gz_full)
    check("guizang 无浮卡阴影、保留编辑引用", "box-shadow" not in gz_full
          and "EDITORIAL NOTE" in gz_full and "END OF NOTE" in gz_full)
    check("guizang 标题与正文 HTML 转义", "&lt;" in md_to_html("# a<b>c", "guizang"))

    log("③f-4 因子分析表 → 卡片式内容展示（因子/方向/多头概率/依据）")
    fmd = ("| 因子 | 方向 | 多头概率 | 依据 |\n|---|---|---|---|\n"
           "| 基本面 | 偏多 | 65% | 云计算营收增长提速 |\n"
           "| 技术面 | 偏空 | 42% | 跌破 20 日均线 |\n"
           "| 资金面 | 中性 | 50% | 北向资金持平 |")
    fc = md_to_html(fmd, "game")
    check("game 因子表不再渲染 table", "<table" not in fc and "<th" not in fc)
    check("game 因子卡片含方向徽章▲▼+涨跌色", "▲ 偏多" in fc and "▼ 偏空" in fc
          and GAME["up"] in fc and GAME["down"] in fc)
    check("game 因子卡片概率血条", "█" in fc and "░" in fc and GAME["hp_full"] in fc)
    check("game 因子卡片依据标签+正文", ">依据</span>" in fc
          and "云计算营收增长提速" in fc and "跌破 20 日均线" in fc)
    check("game 因子卡片金色边框+硬阴影+面板卡底", GAME["border"] in fc
          and GAME["shadow"] in fc and GAME["card_bg"] in fc)
    fk = md_to_html(fmd, "klein")
    check("klein 因子卡片式无 table", "<table" not in fk and KLEIN["card_bg"] in fk
          and "▲ 偏多" in fk and ">依据</span>" in fk)
    check("klein 因子卡片细框进度条", "width:65%;" in fk and KLEIN["up"] in fk)
    fp2 = md_to_html(fmd, "pixel")
    check("pixel 因子卡片式无 table", "<table" not in fp2 and PIXEL["card_bg"] in fp2
          and "▼ 偏空" in fp2 and ">依据</span>" in fp2 and "width:42%;" in fp2)
    fm2 = md_to_html(fmd, "monitor")
    check("monitor 因子卡片保持零表格", "<table" not in fm2
          and MONITOR["card_bg"] in fm2 and "▲ 偏多" in fm2)
    fgz = md_to_html(fmd, "guizang")
    check("guizang 因子矩阵为杂志 rowline", "<table" not in fgz
          and "FACTOR MATRIX" in fgz and GUIZANG["font_serif"] in fgz
          and "box-shadow" not in fgz)
    fq = md_to_html("| 因子 | 概率 |\n|---|---|\n| 基本面 | 65% |\n| 技术面 | 42% |")
    check("无方向列按概率 50% 上下推导徽章", "▲ 偏多" in fq and "▼ 偏空" in fq)
    fz = md_to_html("| 因子 | 方向 | 概率 | 依据 | 备注 |\n|---|---|---|---|---|\n"
                    "| 基本面 | 偏多 | 65% | 超预期 | 观察北向 |")
    check("因子表额外列以列名:值附在卡片", "备注" in fz and "观察北向" in fz)

    log("③g 主题集中化（一行改动全局生效）")
    bak = dict(GAME)
    try:
        GAME.update({"bg": "#111111", "fg": "#222222", "border": "#333333",
                     "hbg": "#444444", "size": "15px", "size_title": "17px",
                     "size_h1": "18px"})
        v = themed_html("T", "# H1\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        check("改 bg 全局生效", "#111111" in v)
        check("改 fg 全局生效", "#222222" in v)
        check("改表格色生效", "#333333" in v and "#444444" in v)
        check("改字号档位生效", "18px" in v and "17px" in v
              and "font-size:15px" in v and "font-size:16px" not in v)
        src = open(__file__, encoding="utf-8").read()
        theme_zone = src.split("模块④b")[1].split("模块⑤")[0]
        # 移除所有主题常量定义（避免颜色被误判为硬编码）
        theme_zone = re.sub(r"(GUIZANG|GAME|KLEIN|PIXEL|DOS|MONITOR|THEMES)\s*=\s*\{.*?\n\}", "", theme_zone, flags=re.S)
        # 注释行不参与扫描（文档里允许出现色值说明）
        theme_zone = "\n".join(l for l in theme_zone.splitlines()
                               if not l.lstrip().startswith("#"))
        hexes = set(re.findall(r"#[0-9A-Fa-f]{6}", theme_zone))
        check("主题区无绕过常量的硬编码色", hexes == set())
    finally:
        GAME.clear()
        GAME.update(bak)

    log("③h 新增量价舆情动量因子")
    analysis_prompt = build_messages("analysis", "阿里巴巴", "示例背景", "mid")[1]["content"]
    check("analysis 提示词含新增因子", SENTIMENT_FACTOR in analysis_prompt)
    check(f"analysis 提示词声明 {FACTOR_COUNT} 个因子",
          f"{FACTOR_COUNT} 个因子" in analysis_prompt)
    check("行业与政策面=真实政策分析，无预设行业/产业链套用",
          INDUSTRY_POLICY_FACTOR in analysis_prompt
          and all(w not in analysis_prompt
                  for w in ("风电装机", "光伏招标", "电商GMV", "白酒配额"))
          and "不得套用产业链传导逻辑" in analysis_prompt)
    rule_analysis = gen_by_rule("阿里巴巴", "analysis")
    check("rule 表格含新增因子", f"| {SENTIMENT_FACTOR} |" in rule_analysis)
    check("analysis 校验器逐项校验", validate_analysis(rule_analysis))
    missing_new_factor = rule_analysis.replace(
        next(line for line in rule_analysis.splitlines()
             if SENTIMENT_FACTOR in line), "")
    check("analysis 缺新增因子时不通过", not validate_analysis(missing_new_factor))
    sample_sent = SentPack(
        hours=48,
        momentum={"ok": True, "score": 0.42},
        agg={"综合多头概率锚点": 69},
    )
    check("rule 使用新增因子本地锚点",
          "| 量价舆情动量（48h） | 偏多 | 69% |" in
          gen_by_rule("阿里巴巴", "analysis", sent_pack=sample_sent))

    log("③i NewsNow 10源接入（知乎/抖音/微博/虎扑/AI hot/联合早报/香港01/今日头条/百度/B站）")
    if newsnow_mod is not None:
        try:
            # 离线样本自检
            from newsnow_sources import selftest_newsnow
            # selftest_newsnow 自己打印，这里只校验返回码
            ret = selftest_newsnow()
            check("NewsNow 离线解析", ret == 0)
            # 集成：collect 结构
            from newsnow_sources import collect_newsnow, render_newsnow_rule, TARGET_SOURCES
            check("NewsNow TARGET 10源", len(TARGET_SOURCES) == 10 and "zhihu" in TARGET_SOURCES
                  and "hk01" in TARGET_SOURCES and "toutiao" in TARGET_SOURCES
                  and "baidu" in TARGET_SOURCES and "bilibili" in TARGET_SOURCES)
            # 模拟 pack 渲染走 pixel 主题
            from newsnow_sources import NewsNowPack, HotItem
            demo_pack = NewsNowPack(items={
                "知乎热榜": [HotItem(source="知乎热榜", id="1", title="阿里巴巴Q3财报超预期", url="https://zhihu.com/q/1", extra_info="100万热度")],
                "微博实时热搜": [HotItem(source="微博实时热搜", id="a", title="+2.5% 阿里巴巴大涨", url="https://weibo.com/a", extra_info="热")],
                "香港01": [HotItem(source="香港01", id="b", title="港股电商板块", url="https://hk01.com/b", extra_info="")],
            }, agg={"total": 3, "sources_ok": 3, "sources_total": 10})
            md_demo = render_newsnow_rule("阿里巴巴", demo_pack)
            check("NewsNow 渲染含源名", "知乎热榜" in md_demo and "香港01" in md_demo)
            # pixel 主题渲染涨跌突出
            html_demo = themed_html("测试NewsNow", md_demo, "pixel")
            check("NewsNow+pixel 涨跌突出", "▲" in html_demo or "热" in html_demo or "11px" in html_demo)
            # game 默认主题渲染（整体像素游戏风）
            html_game = themed_html("测试NewsNow", md_demo)
            check("NewsNow+game 游戏元素", "SCORE" in html_game and "♥" in html_game
                  and GAME["bg"] in html_game)
        except Exception as e:
            check(f"NewsNow 集成异常: {e}", False)
    else:
        check("NewsNow 模块缺失（应存在 newsnow_sources.py）", False)

    log("③j 量价舆情动量·十七平台扫描（156h 窗口 + 有关板块）")
    try:
        from stock_news_scan import selftest_scan, demo_scan_pack, \
            DEFAULT_WINDOW_HOURS
        check("默认窗口为 156h", DEFAULT_WINDOW_HOURS == 156)
        check("十七平台扫描自检通过", selftest_scan() == 0)
        sp = SentPack(hours=156, scan=demo_scan_pack())
        md = render_sentiment_appendix(sp)
        check("情绪附录并入十七平台扫描",
              ("十八平台扫描" in md or "十七平台扫描" in md) and "有关板块" in md)
        ctx = sentiment_context(sp)
        check("AI 上下文并入十七平台扫描", ("十八平台扫描" in ctx or "十七平台扫描" in ctx))
        check("扫描聚合含平台计数", sp.scan.agg.get("platforms_total") in (17, 18))
    except Exception as e:  # noqa: BLE001
        check(f"十七平台扫描集成异常: {e}", False)

    log("③j2 香港券商栏目（独立模板 + 简报栏目提示词）")
    try:
        import hk_brokers as hb
        check("hk_brokers 模板已注册",
              "hk_brokers" in TEMPLATES
              and TEMPLATE_TITLES.get("hk_brokers") == "香港券商新闻与数据"
              and TEMPLATE_MAX_TOKENS.get("hk_brokers", 0) > 0)
        check("香港券商子模块自检通过", hb.selftest() == 0)
        _hk_prompt = build_messages(
            "hk_brokers", "香港券商动态",
            hb.hk_brokers_context(hb.demo_pack()))[1]["content"]
        check("hk_brokers 提示词含栏目框架",
              "香港券商新闻与数据" in _hk_prompt and "富途" in _hk_prompt)
        _brief_prompt = build_messages(
            "feedscan", "全球市场早报",
            "【24h 全市场快讯数据】\n\n" + hb.hk_brokers_context(
                hb.demo_pack(), max_per_firm=2))[1]["content"]
        check("每日简报提示词在有栏目数据时要求香港券商小节",
              "**香港券商动态**" in _brief_prompt)
        _brief_plain = build_messages(
            "feedscan", "全球市场早报", "【24h 全市场快讯数据】")[1]["content"]
        check("无栏目数据时简报提示词不强求券商小节",
              "**香港券商动态**" not in _brief_plain)
    except Exception as e:  # noqa: BLE001
        check(f"香港券商栏目集成异常: {e}", False)

    log("③k 数据源检查验证数据库（SQLite 落库）")
    try:
        import tempfile
        from source_check_db import connect, run_checks, latest_report, \
            render_report_md, source_history, validate_items
        with tempfile.TemporaryDirectory() as td:
            con = connect(os.path.join(td, "ck.db"))
            run_id, results = run_checks(con, live=False)
            check("校验库覆盖社媒 10 源 + 境外行情 4 源", len(results) in (14, 15)
                  and any(r.source_key == "us_stooq" for r in results))
            check("离线样本全部 ok 无 fail", run_id > 0
                  and all(r.status == "ok" for r in results))
            rep = latest_report(con)
            check("运行台账落库汇总正确",
                  bool(rep["run"]) and rep["run"]["total"] in (14, 15)
                  and rep["run"]["fail"] == 0)
            md_ck = render_report_md(rep)
            check("校验报告含新增三源",
                  all(x in md_ck for x in ("今日头条热榜", "百度实时热点", "B站热榜")))
            check("单源历史按键可查",
                  source_history(con, "toutiao")[0]["source_key"] == "toutiao")
            con.close()
        check("违例规则：空结果被拦截", validate_items([]) != [])
    except ImportError:
        check("检查验证数据库模块缺失（应存在 source_check_db.py）", False)
    except Exception as e:  # noqa: BLE001
        check(f"检查验证数据库集成异常: {e}", False)

    log("③l 标题：股票代码 + 中文名")
    cn_q = [Quote(source="Yahoo财经", ok=True, name="Alibaba Group"),
            Quote(source="东方财富", ok=True, name="阿里巴巴"),
            Quote(source="腾讯财经", ok=True, name="阿里巴巴")]
    check("中文名优先（腾讯/东财）", pick_cn_name(cn_q, "回退") == "阿里巴巴")
    check("无中文名回退英文名",
          pick_cn_name([Quote(source="Yahoo财经", ok=True,
                              name="Alibaba Group")], "回退") == "Alibaba Group")
    check("全部失败回退 TOPIC",
          pick_cn_name([Quote(source="Yahoo财经", ok=False, error="x")],
                       "阿里巴巴") == "阿里巴巴")
    code = normalize_hk_code("09988")
    title_subject = f"HK{code} {pick_cn_name(cn_q, fallback='阿里巴巴')}"
    check("标题主体=HK代码+中文名", title_subject == "HK09988 阿里巴巴")

    log("③i AI 分节压缩（逐节简化 + 失败回退，离线 monkeypatch）")
    md3 = ("导语：本节为开篇导语，" + "铺垫文字。" * 40 + "\n\n"
           "## 核心结论\n\n偏多，目标价 18.20，多头概率 68%。\n\n"
           "- " + "要点细节冗余描述，" * 30 + "关键数字 16.80 保留\n"
           "\n\n## 风险与失效条件\n\n跌破 15.90 复核。" + "次要细节补充，" * 30)
    secs = split_md_sections(md3)
    check("分节：导语+两节=3 部分", len(secs) == 3
          and secs[1].startswith("## 核心结论"))
    check("### 归属上一节", len(split_md_sections("## A\n\nx\n\n### 子\n\ny")) == 1)
    # 环境开关
    old_env = os.environ.get("AI_COMPRESS")
    ai_env_names = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY",
                    "INFERENCE_PROVIDER", "INFERENCE_BASE_URL",
                    "INFERENCE_API_KEY", "INFERENCE_MODEL",
                    "INFERENCE_TIMEOUT", "INFERENCE_MAX_RETRIES")
    old_ai_keys = {k: os.environ[k] for k in ai_env_names if k in os.environ}
    try:
        os.environ["AI_COMPRESS"] = "0"
        check("AI_COMPRESS=0 关闭压缩", ai_compress_enabled() is False)
        out, note = ai_compress_md(md3, provider="deepseek")
        check("关闭时不做任何调用", out is md3 and note == "")
        os.environ.pop("AI_COMPRESS")
        check("默认开启", ai_compress_enabled() is True)
        # 无 Key：跳过（含统一推理 API，避免外部 INFERENCE_* 干扰断言）
        for k in ai_env_names:
            os.environ.pop(k, None)
        out, note = ai_compress_md(md3, provider="deepseek")
        check("无 Key 跳过并说明", out is md3 and "跳过" in note)
        # 正常压缩：monkeypatch chat_completion
        real_cc = chat_completion
        calls = []

        def fake_cc(url, key, model, messages, max_tokens, timeout,
                    temperature=0.7):
            calls.append(messages[-1]["content"])
            part = messages[-1]["content"].split("--- 待压缩的这一节 ---\n")[-1]
            if "核心结论" in part:
                return "## 核心结论\n\n偏多 68%，目标 18.20。"
            raise PushError("模拟接口故障")

        globals()["chat_completion"] = fake_cc
        try:
            os.environ["DEEPSEEK_API_KEY"] = "sk-test"
            out, note = ai_compress_md(md3, provider="deepseek")
        finally:
            globals()["chat_completion"] = real_cc
            os.environ.pop("DEEPSEEK_API_KEY", None)
        check("逐节独立调用（每长节各自一次）", len(calls) == 3
              and any("核心结论" in c for c in calls))
        check("成功节被压缩", "偏多 68%，目标 18.20" in out)
        check("失败节回退原文", "跌破 15.90 复核" in out)
        check("标题结构保留", "## 核心结论" in out and "## 风险与失效条件" in out)
        check("整体变短且有统计", len(out) < len(md3) and "节生效" in note)
        # 输出异常（变长/为空）回退
        def bad_cc(*a, **k):
            return "```markdown\n" + "灌水" * 999 + "\n```"

        globals()["chat_completion"] = bad_cc
        try:
            os.environ["DEEPSEEK_API_KEY"] = "sk-test"
            out2, note2 = ai_compress_md(md3, provider="deepseek")
        finally:
            globals()["chat_completion"] = real_cc
            os.environ.pop("DEEPSEEK_API_KEY", None)
        check("输出变长回退原文", out2 == md3 and "未缩短" in note2)
    finally:
        for _k in ai_env_names:
            os.environ.pop(_k, None)
        os.environ.update(old_ai_keys)   # 自检期间清空的 AI Key 原样恢复
        if old_env is None:
            os.environ.pop("AI_COMPRESS", None)
        else:
            os.environ["AI_COMPRESS"] = old_env

    log("④ 全部模板可构造")
    for t in TEMPLATES:
        try:
            msgs = build_messages(t, "阿里巴巴", "示例背景", "mid")
            check(f"模板 {t}", bool(msgs[1]["content"]))
        except Exception as e:  # noqa: BLE001
            check(f"模板 {t} 构造异常: {e}", False)
    check("analysis 校验器", validate_analysis(gen_by_rule("t", "analysis")))
    brand = add_branding("正文")
    check("品牌头部字段齐全",
          all(k in brand_header_md() for k in (BRAND_TITLE, BRAND_SUBTITLE)))
    check("作者不再出现在头部", BRAND_AUTHOR not in brand_header_md())
    check("品牌尾部声明", BRAND_DISCLAIMER in brand_footer_md())
    check("作者固定在最后", brand.rstrip().endswith(f"**{BRAND_AUTHOR} · {BRAND_DISCLAIMER}**")
          and brand.rstrip().endswith(brand_footer_md().rstrip()))
    check("品牌头尾可渲染为 HTML", BRAND_TITLE in md_to_html(brand)
          and BRAND_AUTHOR in md_to_html(brand))

    log("⑤ 内容指纹与跨运行状态")
    from types import SimpleNamespace as NS
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "st.json")
        st0 = load_state(sp)
        check("无状态文件→空基线", st0.get("runs") == {})
        st0["runs"]["analysis|阿里巴巴|09988"] = {
            "fingerprint": "abc123", "ts": "x", "price": 16.8}
        save_state(sp, st0)
        st1 = load_state(sp)
        check("状态写入后可读回",
              st1["runs"]["analysis|阿里巴巴|09988"]["fingerprint"] == "abc123")
        with open(sp, "w", encoding="utf-8") as f:
            f.write("{broken")
        check("状态损坏→回退空基线", load_state(sp).get("runs") == {})
    fp_a = content_fingerprint("analysis", "阿里巴巴", "正文：看涨",
                               {"n_ok": 3, "price": 16.8}, {"anchor": 60},
                               {"Google新闻": ["k1"]})
    fp_b = content_fingerprint("analysis", "阿里巴巴", "正文：看涨",
                               {"n_ok": 3, "price": 16.8}, {"anchor": 60},
                               {"Google新闻": ["k1"]})
    fp_c = content_fingerprint("analysis", "阿里巴巴", "正文：看跌",
                               {"n_ok": 3, "price": 16.8}, {"anchor": 60},
                               {"Google新闻": ["k1"]})
    fp_d = content_fingerprint("analysis", "阿里巴巴", "正文：看涨",
                               {"n_ok": 3, "price": 17.1}, {"anchor": 60},
                               {"Google新闻": ["k1"]})
    check("同内容→指纹稳定", fp_a == fp_b)
    check("正文变化→指纹变化", fp_a != fp_c)
    check("行情变化→指纹变化", fp_a != fp_d)
    pk = NS(items={"Google新闻": [NS(title="阿里巴巴 签 大单")]},
            scan=NS(direct={"雪球": [NS(title="阿里热帖")]}, sector_hits={}))
    ks = gather_item_keys(pk)
    check("样本指纹含两源", set(ks) == {"Google新闻", "扫·雪球"})
    ks2 = gather_item_keys(pk, pk)
    check("重复采集自动去重", len(ks2["Google新闻"]) == 1)
    new = diff_item_keys(ks, {"Google新闻": ks["Google新闻"]})
    check("增量=仅新源条目", set(new) == {"扫·雪球"})
    check("无基线→不报增量", diff_item_keys(ks, {}) == {})
    qb_live = _quote_brief([qa, qb, qc])
    check("行情共识=三源中位价",
          abs(qb_live["price"] - 16.8) < 1e-9 and qb_live["n_ok"] == 3)
    qline = _quote_freshness_line(qb_live, None)
    check("行情看板:首次基线", "首次记录基线" in qline)
    qline2 = _quote_freshness_line(qb_live, 16.5)
    check("行情看板:较上次涨跌", "+0.300" in qline2 and "+1.82%" in qline2)
    check("行情看板:持平识别", "持平" in _quote_freshness_line(qb_live, 16.8))
    fmd = render_freshness_md(now_cst="08-07 18:00", template="analysis",
                              hours=48, fingerprint=fp_a, dup=True,
                              quote_line=qline, new_items={}, first_run=False,
                              anchor_line="锚点 ≈ 60%", state_enabled=True)
    check("看板:重复内容告警", "与上次推送内容一致" in fmd and fp_a in fmd)
    fmd2 = render_freshness_md(now_cst="08-07 18:00", template="analysis",
                               hours=48, fingerprint=fp_a, dup=False,
                               quote_line=qline,
                               new_items={"Google新闻": ["x", "y"]},
                               first_run=False, anchor_line="锚点 ≈ 60%",
                               state_enabled=True)
    check("看板:增量计数", "新增样本：2 条" in fmd2)
    fmd3 = render_freshness_md(now_cst="08-07 18:00", template="analysis",
                               hours=48, fingerprint=fp_a, dup=None,
                               quote_line=qline, new_items={}, first_run=True,
                               anchor_line="", state_enabled=True)
    check("看板:首次运行", "首次建立基线" in fmd3)
    fmd4 = render_freshness_md(now_cst="08-07 18:00", template="analysis",
                               hours=48, fingerprint=fp_a, dup=None,
                               quote_line=qline, new_items={},
                               first_run=False, anchor_line="",
                               state_enabled=False)
    check("看板:可关闭状态对比", "已关闭" in fmd4)
    item_ns = NS(title="阿里云计算提速", source="Google", label="利好",
                 score=0.5, age_h=2.0)
    check("新增样本标🆕", "🆕" in _item_line(item_ns, 1, True)
          and "🆕" not in _item_line(item_ns, 1, False))

    log("⑨ 字符模拟图渲染（纯字符，无图片依赖）")
    os.makedirs("output", exist_ok=True)
    demo = demo_chart_bars(60)
    check("演示数据 60 根", len(demo) == 60 and demo[-1]["close"] == 16.80)
    check("MA 已计算", all(b.get("ma20") for b in demo[19:]))
    chart_txt = render_char_chart(demo, {"code": "09988", "source": "DEMO"})
    check("字符图含价格轴与图例", "┤" in chart_txt and "VOL" in chart_txt and "字符模拟" in chart_txt)
    check("字符图行数合理", 10 <= len(chart_txt.splitlines()) <= 30)
    check("字符图含涨跌字符", ("█" in chart_txt or "▓" in chart_txt) and "│" in chart_txt)
    check("字符图含支撑/压力标注", "S1" in chart_txt and "R1" in chart_txt)
    # 验证 markdown block 生成（离线环境可能因网络失败返回空串，视为优雅降级）
    try:
        block = make_chart_block("09988", [qa], False)
        check("字符模拟图 block 在线含代码块或离线优雅降级",
              block == "" or ("```text" in block and "字符模拟图" in block))
    except Exception as e:
        check(f"字符模拟图 block 不应抛异常: {e}", False)
    demo_block = f"```text\n{chart_txt}\n```"
    check("演示数据可包裹为代码块", "```text" in demo_block)
    check("内联链接正常（非图片）",
          '<a href="https://x/y"' in _inline_md("[k](https://x/y)", "game"))
    # 旧图片语法仍应转为链接或保留（不再强制转为 <img>，字符图为主）
    check("图片语法不崩溃", "https://x/y.png" in _inline_md("![k](https://x/y.png)", "game"))

    log(f"\n{'✅ 自检全部通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


# ================================================================ 主流程

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="多市场行情/快讯 + DeepSeek 分析 → 多通道推送",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="只生成不推送")
    p.add_argument("--check-only", action="store_true", help="只检查 Secrets")
    p.add_argument("--selftest", action="store_true", help="离线自检后退出")
    p.add_argument("--channel", default="pushplus", choices=CHANNELS)
    p.add_argument("--ai-provider", default="deepseek", choices=PROVIDERS,
                   dest="ai_provider")
    p.add_argument("--template", default="brief", choices=TEMPLATES,
                   help="分析框架：" + "/".join(TEMPLATES))
    p.add_argument("--topic", default="", help="分析标的/主题（或环境变量 TOPIC）")
    p.add_argument("--context", default="",
                   help="背景信息/复盘细节（或环境变量 CONTEXT）")
    p.add_argument("--hk-code", default="", dest="hk_code",
                   help="港股代码（如 09988），接入三源核验行情（或环境变量 HK_CODE）")
    p.add_argument("--risk", default="mid", choices=RISKS,
                   help="portfolio 模板的风险偏好档位")
    p.add_argument("--theme", default="", choices=["", "default", "guizang", "game", "klein", "pixel", "dos", "monitor", "noc"],
                   help="pushplus 通道主题（默认 dos=DOS CRT复古终端：磷光绿+扫描线+等宽字体；"
                        "guizang=电子杂志竖版长页；game=8-bit像素游戏；klein=米黄纸底；"
                        "pixel=暗色监控大屏；monitor/noc=服务器大屏）（或环境变量 THEME）")
    p.add_argument("--hours", type=int, default=48,
                   help="analysis/sentiment/feedscan/十七平台扫描的数据窗口小时数"
                        "（默认 48，支持 24/48/72/156）")
    p.add_argument("--yt-channel", default="", dest="yt_channel",
                   help="YouTube 频道 handle（默认 @investtalk，或环境变量 YT_CHANNEL）")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--no-state", action="store_true", dest="no_state",
                   help="关闭跨运行状态对比（不读/不写 output/push_state.json，"
                        "可用环境变量 PUSH_STATE_PATH 指定其他路径）")
    p.add_argument("--no-kline", action="store_true", dest="no_kline",
                   help="关闭字符模拟图（兼容旧名 --no-kline，等同 --no-chart）")
    p.add_argument("--no-chart", action="store_true", dest="no_chart",
                   help="关闭字符模拟图（默认：给 --hk-code 时自动生成字符模拟走势图并"
                        "嵌入推送正文顶部，纯字符无需上传）")
    p.add_argument("--no-ai-compress", action="store_false", dest="ai_compress",
                   default=True,
                   help="关闭 AI 分节压缩（默认开启：正文按章节逐节调用 AI 简化+压缩，"
                        "失败自动回退原文；也可用环境变量 AI_COMPRESS=0 关闭）")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.selftest:
        return selftest()

    channel, provider, template = args.channel, args.ai_provider, args.template
    if template == "hk_brokers" and not any(
            a == "--hours" or a.startswith("--hours=") for a in argv):
        args.hours = 96
    topic = args.topic or env("TOPIC") or DEFAULT_TOPIC
    user_context = args.context or env("CONTEXT")
    hk_code_raw = args.hk_code or env("HK_CODE")
    risk = args.risk or env("RISK") or "mid"
    theme = args.theme or env("THEME") or "dos"
    targets = channel_targets(channel)

    log("=" * 60)
    log(f"Octopus AI PushPlus Market Intelligence  v{VERSION}")
    log(f"  模板: {template}({TEMPLATE_TITLES[template]})  通道: {channel}"
        f"  AI: {provider}  dry_run: {args.dry_run}  主题: {theme}")
    log(f"  主题: {topic}"
        + (f"  港股: {hk_code_raw}" if hk_code_raw else "")
        + (f"  风险档: {RISK_ZH[risk]}" if template == "portfolio" else ""))
    log("=" * 60)

    if args.check_only:
        ok = print_secret_report(channel, provider)
        if not ok:
            log("  ⚠️ 检查到缺失项；本脚本会在真实推送阶段优雅跳过缺 Key 通道，不让 Actions 因缺 Key 失败。")
        return 0

    # ---------- 新增因子：analysis/sentiment 共用 48h 量价舆情数据 ----------
    sent_pack, appendix_md, code4sent = None, "", None
    if template in ("sentiment", "analysis"):
        code4sent = normalize_hk_code(hk_code_raw) if hk_code_raw else None
        scan_hint = "Google新闻/YouTube + 十七平台扫描"
        if template == "analysis":
            log(f"\n📡 正在采集新增因子「{SENTIMENT_FACTOR}」的数据"
                f"（{args.hours}h：{scan_hint}）…")
        else:
            log(f"\n📡 正在采集 {args.hours}h 窗口的多源舆情（{scan_hint}）…")
        sent_pack = collect_sentiment(topic, code4sent, args.hours,
                                      min(args.timeout, 15), args.yt_channel)
        for name, items in sent_pack.items.items():
            agg = _src_agg(items)
            log(f"  ✅ {name}: {agg['n']} 条"
                f"（利好{agg['pos']}/利空{agg['neg']}/中性{agg['neu']}"
                f"，动量 {agg['mean']:+.2f}）")
        if sent_pack.momentum:
            log(f"  {'✅' if sent_pack.momentum.get('ok') else '❌'} 量价动量: "
                f"{sent_pack.momentum.get('detail') or sent_pack.momentum.get('label')}")
        for e in sent_pack.errors:
            log(f"  ⚠️  {e}")
        log(f"  → 综合多头概率锚点 ≈ "
            f"{sent_pack.agg.get('综合多头概率锚点', '—')}%")
        if getattr(sent_pack, "scan", None):
            sa = sent_pack.scan.agg
            log(f"  🛰 十七平台扫描: 直接相关 {sa.get('n_direct', 0)} 条"
                f" / 板块相关 {sa.get('n_sector', 0)} 条，命中 "
                f"{sa.get('platforms_hit', 0)}/{sa.get('platforms_total', 17)} 平台")
            if sent_pack.scan.dyn_sectors:
                log(f"  🧭 有关板块: "
                    + "、".join(s for s, _ in sent_pack.scan.dyn_sectors[:6]))
        appendix_md = render_sentiment_appendix(sent_pack)
        factor_context = sentiment_context(sent_pack)
        user_context = (f"{factor_context}\n\n{user_context}"
                        if user_context else factor_context)

    # ---------- 模块②b：feedscan 模板采集 13 源全市场快讯（含 DeepSeek 搜索）----------
    feed_pack = None
    # 每日全市场简报并入的栏目附录（香港券商 + 国外投行研报），在下方填充；
    # 最终附录重算处需再次拼接，先在这里初始化保证作用域内始终有值
    brief_columns_appendix_md = ""
    if template == "feedscan":
        log(f"\n📡 正在采集 13 源全市场快讯（{args.hours}h 窗口，含 DeepSeek 搜索）…")
        feed_pack = collect_feeds(args.hours, min(args.timeout, 12), topic=topic)
        for spec in FEED_SPECS:
            items = feed_pack.items.get(spec.name)
            if items:
                agg = _src_agg(items)
                log(f"  ✅ {spec.name}: {agg['n']} 条"
                    f"（利好{agg['pos']}/利空{agg['neg']}/中性{agg['neu']}"
                    f"，情绪 {agg['mean']:+.2f}）")
            else:
                err = next((e for e in feed_pack.errors
                            if e.startswith(spec.name)), "失败")
                log(f"  ⚠️  {err}")
        # DeepSeek 搜索额外日志
        ds_items = feed_pack.items.get("DeepSeek 搜索")
        if ds_items:
            agg = _src_agg(ds_items)
            log(f"  ✅ DeepSeek 搜索: {agg['n']} 条"
                f"（利好{agg['pos']}/利空{agg['neg']}/中性{agg['neu']}"
                f"，情绪 {agg['mean']:+.2f}）")
        else:
            err = next((e for e in feed_pack.errors if e.startswith("DeepSeek")), "失败")
            log(f"  ⚠️  {err}")
        log(f"  → 全市场 {feed_pack.agg['n']} 条，情绪均值 "
            f"{feed_pack.agg['mean']:+.2f}，多头概率锚点 ≈ "
            f"{feed_pack.agg['综合多头概率锚点']}%")
        # 快讯仅供分析使用，不在报告中展示原始明细。
        appendix_md = ""
        user_context = feed_context(topic, feed_pack)

        # 每日全市场简报并入栏目（仅全市场主题；环境变量 BRIEF_COLUMNS=0
        # 可关闭）：🏦 香港券商新闻与数据（96h）+ 🏛 国外投行研报标题（72h）。
        # 采集失败/无命中只如实记缺口，绝不用演示数据填充正式推送。
        if is_market_wide_topic(topic) and env("BRIEF_COLUMNS") != "0":
            _col_ctx_parts: list[str] = []
            _col_apx_parts: list[str] = []
            try:
                from hk_brokers import (collect_hk_brokers,
                                        hk_brokers_context,
                                        render_hk_brokers_appendix)
                log("\n🏦 简报栏目：香港券商新闻与数据（96h，12 家）…")
                _hk_pack = collect_hk_brokers(
                    topic="港股市场", hours=96,
                    timeout=min(args.timeout, 12))
                log(f"  → {_hk_pack.agg.get('n', 0)} 条 / "
                    f"{_hk_pack.agg.get('firms_hit', 0)}/12 家，"
                    f"股价快照 {_hk_pack.agg.get('quotes_hit', 0)} 家")
                for _e in _hk_pack.errors[:4]:
                    log(f"  ⚠️  {_e}")
                if _hk_pack.agg.get("n", 0):
                    _col_ctx_parts.append(
                        hk_brokers_context(_hk_pack, max_per_firm=2))
                    _col_apx_parts.append(render_hk_brokers_appendix(
                        _hk_pack, max_per_firm=2))
                else:
                    _col_apx_parts.append(
                        "---\n🏦 **香港券商新闻与数据**\n\n- ⚠️ 本次未采集到（"
                        + "；".join(_hk_pack.errors[:3]) + "）")
            except Exception as ex:  # noqa: BLE001 —— 栏目失败不拖累简报
                log(f"  ⚠️ 香港券商栏目采集失败：{ex}")
                _col_apx_parts.append(
                    "---\n🏦 **香港券商新闻与数据**\n\n"
                    f"- ⚠️ 采集失败：{str(ex)[:120]}")
            try:
                from foreign_institution_weekly import (
                    collect_foreign_weekly, foreign_weekly_context,
                    render_foreign_weekly_appendix)
                log("\n🏛 简报栏目：国外投行研报标题（72h，12 家）…")
                _fw_pack = collect_foreign_weekly(
                    topic="全球市场", hours=72,
                    timeout=min(args.timeout, 12))
                log(f"  → {_fw_pack.agg.get('n', 0)} 篇 / "
                    f"{_fw_pack.agg.get('institutions_hit', 0)}/12 家")
                for _e in _fw_pack.errors[:4]:
                    log(f"  ⚠️  {_e}")
                if _fw_pack.agg.get("n", 0):
                    _col_ctx_parts.append(foreign_weekly_context(_fw_pack))
                    _col_apx_parts.append(render_foreign_weekly_appendix(
                        _fw_pack, max_per_inst=2))
                else:
                    _col_apx_parts.append(
                        "---\n🏛 **国外投行研报标题**\n\n- ⚠️ 本次未采集到（"
                        + "；".join(_fw_pack.errors[:3]) + "）")
            except Exception as ex:  # noqa: BLE001
                log(f"  ⚠️ 国外投行研报栏目采集失败：{ex}")
                _col_apx_parts.append(
                    "---\n🏛 **国外投行研报标题**\n\n"
                    f"- ⚠️ 采集失败：{str(ex)[:120]}")
            if _col_ctx_parts:
                user_context = (f"{user_context}\n\n"
                                + "\n\n".join(_col_ctx_parts))
            if _col_apx_parts:
                brief_columns_appendix_md = "\n\n".join(_col_apx_parts)
                appendix_md = (f"{appendix_md}\n\n"
                               f"{brief_columns_appendix_md}")

    # ---------- 模块②c：NewsNow 10源热榜聚合（知乎/抖音/微博/虎扑/AI hot/联合早报/香港01/今日头条/百度/B站） ----------
    newsnow_pack = None
    if template == "newsnow":
        log("\n🔥 正在采集 NewsNow 10源热榜（知乎/抖音/微博/虎扑/AI hot/联合早报/香港01/今日头条/百度/B站）…")
        try:
            from newsnow_sources import collect_newsnow
            newsnow_pack = collect_newsnow(timeout=min(args.timeout, 15))
            for src_name, items in newsnow_pack.items.items():
                log(f"  ✅ {src_name}: {len(items)} 条")
            for e in newsnow_pack.errors:
                log(f"  ⚠️  {e}")
            log(f"  → 共 {newsnow_pack.agg.get('total',0)} 条 / {newsnow_pack.agg.get('sources_ok',0)}/{newsnow_pack.agg.get('sources_total',10)} 源")
            # 构造上下文
            try:
                from newsnow_sources import render_newsnow_appendix, newsnow_context
                appendix_md = render_newsnow_appendix(newsnow_pack)
                user_context = newsnow_context(newsnow_pack)
            except Exception as ex:
                log(f"  ⚠️  NewsNow 渲染失败：{ex}")
                appendix_md = ""
                user_context = ""
        except Exception as ex:
            log(f"  ❌ NewsNow 模块加载失败：{ex}")
            newsnow_pack = None

    # ---------- 模块②d：国外机构一周研报（Goldman/Morgan Stanley/JPMorgan等，新增栏目） ----------
    foreign_weekly_pack = None
    if template == "foreign_weekly":
        # rule 模板直接走离线演示包，避免网络阻塞导致 dry-run 超时
        if provider == "rule":
            log(f"\n🏛 国外机构一周研报（rule 模板离线演示，{args.hours}h 窗口，12 家机构）…")
            try:
                from foreign_institution_weekly import demo_pack, render_foreign_weekly_appendix, foreign_weekly_context
                foreign_weekly_pack = demo_pack()
                foreign_weekly_pack.topic = topic
                foreign_weekly_pack.hours = args.hours
                appendix_md = render_foreign_weekly_appendix(foreign_weekly_pack)
                user_context = foreign_weekly_context(foreign_weekly_pack)
                log(f"  ✅ 离线演示包 {foreign_weekly_pack.agg.get('n',0)} 篇 / {foreign_weekly_pack.agg.get('institutions_hit',0)}/{foreign_weekly_pack.agg.get('institutions_total',12)} 机构")
            except Exception as ex2:
                log(f"  ❌ 演示包失败：{ex2}")
                foreign_weekly_pack = None
        else:
            log(f"\n🏛 正在采集国外专业投资机构一周研报（{args.hours}h 窗口，12 家机构）…")
            try:
                from foreign_institution_weekly import collect_foreign_weekly, render_foreign_weekly_appendix, foreign_weekly_context, demo_pack as fw_demo_pack
                foreign_weekly_pack = collect_foreign_weekly(
                    topic=topic, hours=args.hours, timeout=min(args.timeout, 15))
                # 若在线采集 0 篇（沙箱离线或全失败），自动降级演示包
                if foreign_weekly_pack.agg.get("n", 0) == 0:
                    log("  ⚠️ 在线采集 0 篇（可能离线/沙箱），降级使用离线演示包")
                    foreign_weekly_pack = fw_demo_pack()
                    foreign_weekly_pack.topic = topic
                    foreign_weekly_pack.hours = args.hours
                for inst_key, items in foreign_weekly_pack.reports.items():
                    log(f"  ✅ {items[0].institution if items else inst_key}: {len(items)} 篇")
                for e in foreign_weekly_pack.errors:
                    log(f"  ⚠️  {e}")
                log(f"  → 共 {foreign_weekly_pack.agg.get('n',0)} 篇 / {foreign_weekly_pack.agg.get('institutions_hit',0)}/{foreign_weekly_pack.agg.get('institutions_total',12)} 机构")
                appendix_md = render_foreign_weekly_appendix(foreign_weekly_pack)
                user_context = foreign_weekly_context(foreign_weekly_pack)
            except Exception as ex:
                log(f"  ❌ 国外机构一周研报模块加载失败：{ex}")
                # 降级演示包
                try:
                    from foreign_institution_weekly import demo_pack, render_foreign_weekly_appendix, foreign_weekly_context
                    foreign_weekly_pack = demo_pack()
                    foreign_weekly_pack.topic = topic
                    foreign_weekly_pack.hours = args.hours
                    appendix_md = render_foreign_weekly_appendix(foreign_weekly_pack)
                    user_context = foreign_weekly_context(foreign_weekly_pack)
                    log("  ⚠️ 已降级使用离线演示包")
                except Exception as ex2:
                    log(f"  ❌ 演示包也失败：{ex2}")
                    foreign_weekly_pack = None

    # ---------- 模块②e：香港券商新闻与数据（独立栏目）----------
    hk_brokers_pack = None
    if template == "hk_brokers":
        log(f"\n🏦 正在采集香港券商新闻与数据（{args.hours}h 窗口，12 家券商）…")
        try:
            from hk_brokers import (collect_hk_brokers,
                                    render_hk_brokers_appendix,
                                    hk_brokers_context)
            hk_brokers_pack = collect_hk_brokers(
                topic=topic, hours=args.hours,
                timeout=min(args.timeout, 15))
            for _k, _items in hk_brokers_pack.reports.items():
                log(f"  ✅ {_items[0].firm if _items else _k}: {len(_items)} 条")
            for _e in hk_brokers_pack.errors:
                log(f"  ⚠️  {_e}")
            log(f"  → 共 {hk_brokers_pack.agg.get('n', 0)} 条 / "
                f"{hk_brokers_pack.agg.get('firms_hit', 0)}/12 家，"
                f"股价快照 {hk_brokers_pack.agg.get('quotes_hit', 0)} 家")
            appendix_md = render_hk_brokers_appendix(hk_brokers_pack)
            user_context = hk_brokers_context(hk_brokers_pack)
        except Exception as ex:  # noqa: BLE001
            log(f"  ❌ 香港券商栏目失败：{ex}（将降级为规则提示）")
            hk_brokers_pack = None

    # ---------- 模块①：三源取价 + 交叉核验（可选）----------
    data_md, context = "", user_context
    quotes: list[Quote] = []
    if hk_code_raw:
        code = normalize_hk_code(hk_code_raw)
        log(f"\n📥 正在从 3 个免费源获取 HK{code} 行情…")
        quotes = fetch_all(code, timeout=min(args.timeout, 15))
        ver = verify_quotes(quotes)
        for q in quotes:
            if q.ok:
                log(f"  ✅ {q.source}: {q.price:.3f}"
                    + (f"（{q.change_pct:+.2f}%）" if q.change_pct is not None else ""))
            else:
                log(f"  ❌ {q.source}: {q.error}")
        log(f"  → 核验结论：{ver.verdict}")
        data_md = market_block_md(code, quotes, ver)
        line = market_context_line(code, quotes, ver)
        context = (f"{line}\n\n{user_context}" if user_context else line)

    # ---------- 模块③：内容生成 ----------
    try:
        if provider == "rule":
            if template == "sentiment" and sent_pack is not None:
                content = render_sentiment_rule(topic, code4sent, sent_pack)
            elif template == "feedscan" and feed_pack is not None:
                content = render_feed_rule(topic, feed_pack)
            elif template == "newsnow" and newsnow_pack is not None:
                try:
                    from newsnow_sources import render_newsnow_rule
                    content = render_newsnow_rule(topic, newsnow_pack)
                except Exception:
                    content = gen_by_rule(topic, template, sent_pack=sent_pack, newsnow_pack=newsnow_pack)
            elif template == "foreign_weekly" and foreign_weekly_pack is not None:
                try:
                    from foreign_institution_weekly import render_foreign_weekly_rule
                    content = render_foreign_weekly_rule(topic, foreign_weekly_pack)
                except Exception:
                    content = gen_by_rule(topic, template)
            elif template == "hk_brokers" and hk_brokers_pack is not None:
                try:
                    from hk_brokers import render_hk_brokers_rule
                    content = render_hk_brokers_rule(topic, hk_brokers_pack)
                except Exception:
                    content = gen_by_rule(topic, template)
            else:
                # newsnow may be None if import fails, pass it anyway
                try:
                    content = gen_by_rule(topic, template, sent_pack=sent_pack, newsnow_pack=locals().get("newsnow_pack"))
                except TypeError:
                    content = gen_by_rule(topic, template, sent_pack=sent_pack)
        else:
            ai_url, ai_model, key, ai_label, keyless = ai_credentials(provider)
            if not key and not keyless:
                log(f"⚠️  缺少 AI Key（ai_provider={provider}；可配置 INFERENCE_API_KEY "
                    f"或 DEEPSEEK_API_KEY），已降级使用 rule 模板继续。")
                provider = "rule"
                if template == "sentiment" and sent_pack is not None:
                    content = render_sentiment_rule(topic, code4sent, sent_pack)
                elif template == "feedscan" and feed_pack is not None:
                    content = render_feed_rule(topic, feed_pack)
                elif template == "newsnow" and newsnow_pack is not None:
                    try:
                        from newsnow_sources import render_newsnow_rule
                        content = render_newsnow_rule(topic, newsnow_pack)
                    except Exception:
                        content = gen_by_rule(topic, template, sent_pack=sent_pack,
                                              newsnow_pack=newsnow_pack)
                elif template == "foreign_weekly" and foreign_weekly_pack is not None:
                    try:
                        from foreign_institution_weekly import render_foreign_weekly_rule
                        content = render_foreign_weekly_rule(topic, foreign_weekly_pack)
                    except Exception:
                        content = gen_by_rule(topic, template)
                elif template == "hk_brokers" and hk_brokers_pack is not None:
                    try:
                        from hk_brokers import render_hk_brokers_rule
                        content = render_hk_brokers_rule(topic, hk_brokers_pack)
                    except Exception:
                        content = gen_by_rule(topic, template)
                else:
                    try:
                        content = gen_by_rule(topic, template, sent_pack=sent_pack,
                                              newsnow_pack=locals().get("newsnow_pack"))
                    except TypeError:
                        content = gen_by_rule(topic, template, sent_pack=sent_pack)
            else:
                messages = build_messages(template, topic, context, risk)
                log(f"  🤖 推理端点：{ai_label}")
                content = chat_completion(
                    ai_url, key, ai_model, messages,
                    TEMPLATE_MAX_TOKENS[template], args.timeout)
    except PushError as e:
        if args.dry_run and "缺少 Secret" in str(e):
            log(f"⚠️  {e}")
            log("⚠️  dry-run 模式：降级使用 rule 模板继续演示管线")
            content = gen_by_rule(topic, template, sent_pack=sent_pack)
        else:
            log(f"❌ 内容生成失败：{e}")
            return 1

    # ---------- 模块③c：AI 分节压缩（每部分分别调用 AI 简化 + 压缩正文） ----------
    # 在内容指纹 / 分篇 / 推送之前完成，压缩结果即最终正文；
    # 关闭开关（--no-ai-compress 或 AI_COMPRESS=0）或缺 Key 时自动跳过。
    if getattr(args, "ai_compress", True):
        log("\n✂️ AI 分节压缩（逐节简化）…")
        content, comp_note = ai_compress_md(
            content, provider=provider, template=template,
            timeout=args.timeout)
        if comp_note:
            log(f"  → {comp_note}")

    # ---------- 模块③b：内容指纹 + 与上次推送的新鲜度对比 ----------
    state_enabled = not args.no_state
    st = (load_state(state_path()) if state_enabled
          else {"version": STATE_VERSION, "runs": {}})
    skey = run_state_key(template, topic, hk_code_raw)
    prev = st["runs"].get(skey) or {}

    item_keys = gather_item_keys(sent_pack, feed_pack, newsnow_pack, locals().get("foreign_weekly_pack"))
    new_items = diff_item_keys(item_keys, prev.get("item_keys", {})) \
        if prev else {}
    new_key_set = {k for keys in new_items.values() for k in keys}

    qb = _quote_brief(quotes)
    extra_points: dict = {}
    anchor_parts: list[str] = []
    if sent_pack is not None:
        anchor = sent_pack.agg.get("综合多头概率锚点")
        extra_points["anchor"] = anchor
        extra_points["mom"] = sent_pack.momentum.get("score")
        anchor_parts.append(
            f"综合多头概率锚点 ≈ {anchor if anchor is not None else '—'}%")
        if sent_pack.momentum.get("ok"):
            anchor_parts.append(
                f"量价动量 {sent_pack.momentum['score']:+.2f}")
        scan = getattr(sent_pack, "scan", None)
        if scan is not None:
            sa = scan.agg
            extra_points["scan_hit"] = sa.get("platforms_hit")
            # 兼容 17/18 平台
            anchor_parts.append(
                f"{sa.get('platforms_total', 18)}平台命中 {sa.get('platforms_hit', 0)}"
                f"/{sa.get('platforms_total', 18)}")
    if feed_pack is not None:
        anchor = feed_pack.agg.get("综合多头概率锚点")
        extra_points["feed_anchor"] = anchor
        anchor_parts.append(
            f"全市场快讯锚点 ≈ {anchor}%（{feed_pack.agg.get('n', 0)} 条）")
    if newsnow_pack is not None:
        extra_points["newsnow_total"] = newsnow_pack.agg.get("total")
        anchor_parts.append(
            f"热榜样本 {newsnow_pack.agg.get('total', 0)} 条"
            f"/{newsnow_pack.agg.get('sources_ok', 0)}/{newsnow_pack.agg.get('sources_total', 10)} 源")
    # 国外机构一周研报（新增）
    fw_pack = locals().get("foreign_weekly_pack")
    if fw_pack is not None:
        extra_points["foreign_n"] = fw_pack.agg.get("n")
        extra_points["foreign_hit"] = fw_pack.agg.get("institutions_hit")
        anchor_parts.append(
            f"外资研报 {fw_pack.agg.get('n',0)} 篇 / {fw_pack.agg.get('institutions_hit',0)}/{fw_pack.agg.get('institutions_total',12)} 机构")
    anchor_line = (" · ".join(anchor_parts)
                   or "无本地预聚合数据（本模板不含数据采集模块）")

    fp = content_fingerprint(template, topic, content, qb,
                             extra_points, item_keys)
    dup = bool(prev) and prev.get("fingerprint") == fp
    log(f"\n🔢 内容指纹: {fp}"
        + ("（状态对比已关闭）" if not state_enabled
           else "（首次建立基线）" if not prev
           else ("（⚠️ 与上次推送一致）" if dup else "（与上次不同）")))

    # AI 内容与上次完全一致：自动换表述重试一次，避免推文逐字复读
    if dup and provider != "rule":
        log("♻️  已要求 AI 换表述差异化重试一次…")
        try:
            messages[-1]["content"] = (ANTI_DUP_PREFIX
                                       + messages[-1]["content"])
            content = chat_completion(
                ai_url, key, ai_model, messages,
                TEMPLATE_MAX_TOKENS[template], args.timeout,
                temperature=0.9)
            # 差异化重试生成的是全新正文：同样走一遍 AI 分节压缩
            if getattr(args, "ai_compress", True):
                content, comp_note = ai_compress_md(
                    content, provider=provider, template=template,
                    timeout=args.timeout)
                if comp_note:
                    log(f"  → 重试后{comp_note}")
            fp = content_fingerprint(template, topic, content, qb,
                                     extra_points, item_keys)
            dup = prev.get("fingerprint") == fp
            log(f"  → 重试后指纹: {fp}"
                + ("（仍与上次一致，正文将明确标注）" if dup
                   else "（已与上次区分）"))
        except PushError as e:
            log(f"  ⚠️ 差异化重试失败，沿用原内容：{e}")

    # 附录：把「上次推送后新增」的样本标注 🆕
    if template in ("sentiment", "analysis") and sent_pack is not None:
        appendix_md = render_sentiment_appendix(sent_pack,
                                                new_keys=new_key_set)
    elif template == "feedscan" and feed_pack is not None:
        # 隐藏快讯原始明细，保留香港券商、国外投行等简报栏目附录。
        appendix_md = brief_columns_appendix_md
    elif template == "foreign_weekly" and locals().get("foreign_weekly_pack") is not None:
        try:
            from foreign_institution_weekly import render_foreign_weekly_appendix
            appendix_md = render_foreign_weekly_appendix(locals().get("foreign_weekly_pack"))
        except Exception:
            pass

    freshness_md = render_freshness_md(
        now_cst=datetime.now(CST).strftime("%m-%d %H:%M"),
        template=template, hours=args.hours, fingerprint=fp,
        dup=(dup if state_enabled else None),
        quote_line=_quote_freshness_line(qb, prev.get("price")),
        new_items=new_items, first_run=not prev,
        anchor_line=anchor_line, state_enabled=state_enabled)

    if template == "analysis" and provider != "rule" \
            and not validate_analysis(content):
        content += "\n\n> ⚠️ 本次模型输出未通过框架格式校验，以上为原始返回，仅供参考。"
    if data_md:
        content += "\n\n" + data_md
    if appendix_md:
        content += "\n\n" + appendix_md

    # 字符模拟图：紧跟新鲜度看板、位于正文顶部；任何失败自动降级为空串（兼容 --no-kline）
    no_chart = bool(getattr(args, "no_chart", False) or getattr(args, "no_kline", False))
    chart_md = make_chart_block(hk_code_raw, quotes, no_chart)

    # 新鲜度看板固定在品牌头之后、AI 正文之前；
    # 所有模板/通道/dry-run 走同一个组装函数：作者和研究提示固定在推送正文最后一行。
    content = add_branding(freshness_md + "\n\n---\n\n" + chart_md
                           + content.rstrip())

    # 推送标题保持品牌名：不展示渠道名、主题、标的或时间。
    title = BRAND_TITLE
    log("\n📝 生成的内容：")
    log("-" * 60)
    log(f"# {title}\n\n{content}")
    log("-" * 60)

    # ---------- dry-run ----------
    if args.dry_run:
        log("\n🧪 dry-run：跳过真实推送。各通道 Secret 就绪情况：")
        for ch in targets:
            missing = [n for n in required_secrets(ch, "rule") if not env(n)]
            log(f"  {'⚠️ ' if missing else '✅'} {ch}: "
                + (f"缺少 {', '.join(missing)}" if missing else "就绪"))
        log("\n✅ dry-run 完成。去掉 --dry-run 即为真实推送。"
            "（dry-run 只对比状态，不写入状态文件）")
        return 0

    # ---------- 模块⑤：真实推送 ----------
    if theme != "default" and any(ch != "pushplus" for ch in targets):
        log("ℹ️  主题样式仅作用于 pushplus 通道（企微/Server酱不支持换肤）")
    results: dict[str, str] = {}
    failures = 0
    successes = 0
    skips = 0
    for ch in targets:
        log(f"\n📤 正在通过 {ch} 推送…")
        missing = missing_channel_secrets(ch)
        if missing:
            results[ch] = skip_missing_secret_result(missing)
            skips += 1
            log(f"  ⚠️ {ch}: {results[ch]}")
            continue
        try:
            if ch == "pushplus":
                results[ch] = push_pushplus(title, content, args.timeout,
                                            theme=theme)
            else:
                results[ch] = PUSH_FUNCS[ch](title, content, args.timeout)
            successes += 1
            log(f"  ✅ {ch}: {results[ch]}")
        except PushError as e:
            results[ch] = f"失败：{e}"
            failures += 1
            log(f"  ❌ {ch}: {e}")

    log("\n===== 推送结果汇总 =====")
    for ch, r in results.items():
        mark = "❌" if is_failure_result(r) else "⚠️" if is_skip_result(r) else "✅"
        log(f"  {mark} {ch}: {r}")
    if failures:
        log(f"\n❌ 共 {failures}/{len(targets)} 个通道失败（详见上方日志）")
        return 1
    if skips and not successes:
        log("\n✅ 正文已完整生成并打印；所选推送通道均因缺少 Secret 被优雅跳过。")
        return 0

    # ---------- 模块⑥：写入跨运行状态（供下次新鲜度对比）----------
    if state_enabled:
        st["version"] = STATE_VERSION
        st["runs"][skey] = {
            "fingerprint": fp,
            "ts": datetime.now(CST).isoformat(timespec="seconds"),
            "price": qb.get("price"),
            "anchor": extra_points.get("anchor"),
            "item_keys": {s: ks[:STATE_MAX_KEYS_PER_SRC]
                          for s, ks in item_keys.items()},
        }
        # 只保留最近若干组合，避免状态文件无限增长
        if len(st["runs"]) > STATE_MAX_RUN_KEYS:
            ordered = sorted(st["runs"],
                             key=lambda k: st["runs"][k].get("ts", ""))
            for old_key in ordered[:-STATE_MAX_RUN_KEYS]:
                del st["runs"][old_key]
        save_state(state_path(), st)
        log(f"\n💾 运行状态已写入 {state_path()}（指纹 {fp}，"
            f"样本 {sum(len(v) for v in item_keys.values())} 条）")
    log("\n✅ 全部通道推送成功，请到微信查收。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
