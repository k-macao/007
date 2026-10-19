#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hk_brokers.py — 香港券商新闻与数据栏目

新增栏目：聚合混合 12 家香港主要券商（中资驻港大行 + 本地/互联网券商）
的最新新闻与研报动态（研报标题、评级/目标价调整、公司新闻），并附
上市券商的股价快照数据。

设计（与 foreign_institution_weekly 同一模式）：
- 纯标准库 + Google News RSS（主）/ Bing News RSS（备）真实新闻检索，
  deepseek_search 生成式搜索仅末位兜底；股价快照复用
  hk_quote.fetch_quote（港股/美股统一入口，失败只缺该项）
- 任何单家失败只记数据缺口，不中断整体；采集不到就如实报缺口，
  不用演示数据冒充真实推送（演示包仅用于离线自检 / HK_BROKERS_OFFLINE=1）
- 产出 Markdown 栏目内容，供 pushplus_deepseek 模板与每日简报章节调用

用法：
    python hk_brokers.py --selftest
    python hk_brokers.py --hours 48 --topic "港股市场"
    python pushplus_deepseek.py --template hk_brokers --topic "香港券商动态" --hours 48 --dry-run

模板名：hk_brokers（注册于 pushplus_deepseek.TEMPLATES）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List

UTC = timezone.utc
CST = timezone(timedelta(hours=8), "CST")
DEFAULT_HOURS = 48

# ---------------- 香港券商清单（混合 12 家）----------------
# listing_code 为抓股价用的代码；中信/华泰/招商的驻港子公司本身未单独上市，
# 用其母公司 H 股代码并在 label 注明；建银国际/中银国际为银行系非上市子公司，无股价。

FIRMS: List[Dict[str, any]] = [
    {"key": "cicc", "name": "中金公司", "aliases": ["中金公司", "中金", "CICC"],
     "listing_code": "03908", "listing_label": "3908.HK 中金公司",
     "focus": ["港股策略", "中概", "IPO"]},
    {"key": "citic", "name": "中信证券国际",
     "aliases": ["中信证券国际", "中信证券", "CITIC Securities"],
     "listing_code": "06030", "listing_label": "6030.HK 中信证券（母公司）",
     "focus": ["港股研究", "跨境", "固收"]},
    {"key": "huatai", "name": "华泰金控",
     "aliases": ["华泰金控", "华泰证券", "华泰国际", "Huatai"],
     "listing_code": "06886", "listing_label": "6886.HK 华泰证券（母公司）",
     "focus": ["港股策略", "海外研究"]},
    {"key": "gtja", "name": "国泰君安国际",
     "aliases": ["国泰君安国际", "国泰君安", "Guotai Junan"],
     "listing_code": "01788", "listing_label": "1788.HK 国泰君安国际",
     "focus": ["港股零售", "财富管理"]},
    {"key": "cms", "name": "招商证券国际",
     "aliases": ["招商证券国际", "招商证券", "招证国际"],
     "listing_code": "06099", "listing_label": "6099.HK 招商证券（母公司）",
     "focus": ["港股研究", "机构业务"]},
    {"key": "haitong", "name": "海通国际",
     "aliases": ["海通国际", "海通证券", "Haitong"],
     "listing_code": "06837", "listing_label": "6837.HK 海通证券（母公司；665.HK 已退市）",
     "focus": ["投行", "海外研究"]},
    {"key": "bocom", "name": "交银国际",
     "aliases": ["交银国际", "BOCOM International"],
     "listing_code": "03329", "listing_label": "3329.HK 交银国际",
     "focus": ["银行系研究", "固收"]},
    {"key": "ccbi", "name": "建银国际",
     "aliases": ["建银国际", "CCB International"],
     "listing_code": "", "listing_label": "非上市（建行子公司）",
     "focus": ["投行", "直投"]},
    {"key": "boci", "name": "中银国际", "aliases": ["中银国际", "BOCI"],
     "listing_code": "", "listing_label": "非上市（中行子公司）",
     "focus": ["宏观", "港股研究"]},
    {"key": "futu", "name": "富途证券",
     "aliases": ["富途证券", "富途", "富途牛牛", "Futu"],
     "listing_code": "FUTU", "listing_label": "FUTU (NASDAQ) 富途控股",
     "focus": ["互联网券商", "零售经纪"]},
    {"key": "tiger", "name": "老虎证券",
     "aliases": ["老虎证券", "Tiger Brokers", "UP Fintech"],
     "listing_code": "TIGR", "listing_label": "TIGR (NASDAQ) 老虎证券",
     "focus": ["互联网券商", "全球经纪"]},
    {"key": "bright", "name": "耀才证券",
     "aliases": ["耀才证券", "耀才", "Bright Smart"],
     "listing_code": "01428", "listing_label": "1428.HK 耀才证券金融",
     "focus": ["本地零售", "孖展"]},
]

FIRM_MAP = {f["key"]: f for f in FIRMS}

# ---------------- 数据结构 ----------------


@dataclass
class BrokerItem:
    firm: str
    firm_key: str
    title: str
    url: str = ""
    summary: str = ""
    source: str = ""
    published: datetime | None = None
    rating: str = ""
    sentiment: str = "中性"
    score: float = 0.0


@dataclass
class HkBrokersPack:
    hours: int
    topic: str = ""
    reports: Dict[str, List[BrokerItem]] = field(default_factory=dict)
    # firm_key -> {"label","code","price","change_pct"}，抓不到时无该键
    quotes: Dict[str, Dict] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    agg: Dict = field(default_factory=dict)

# ---------------- 工具 ----------------


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(v) -> datetime | None:
    if v is None:
        return None
    try:
        from deepseek_search import parse_any_time
        return parse_any_time(v)
    except ImportError:
        pass
    if isinstance(v, datetime):
        return v
    try:
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=CST)
    except Exception:
        return None


def _score_text(text: str) -> tuple[float, str]:
    pos_words = ["上调", "增持", "买入", "利好", "超配", "看好", "上行",
                 "增长", "新高", "跑赢", "首选", "upgrade", "overweight",
                 "buy", "bullish", "outperform"]
    neg_words = ["下调", "减持", "卖出", "利空", "低配", "看空", "下行",
                 "跑输", "亏损", "罚", "downgrade", "underweight", "sell",
                 "bearish", "underperform"]
    t = text.lower()
    pos = sum(t.count(w.lower()) for w in pos_words)
    neg = sum(t.count(w.lower()) for w in neg_words)
    total = pos + neg
    score = 0.0 if total == 0 else max(-1.0, min(1.0, (pos - neg) / total))
    label = "利好" if score > 0.15 else ("利空" if score < -0.15 else "中性")
    return round(score, 2), label


def _mk_item(firm_key: str, title: str, url: str, summary: str, ts,
             source: str = "") -> BrokerItem:
    firm = FIRM_MAP.get(firm_key, {})
    name = firm.get("name", firm_key)
    score, label = _score_text(f"{title} {summary}")
    rating = ""
    m = re.search(r"(增持|买入|中性|减持|卖出|超配|低配|跑赢大市|跑输大市|"
                  r"overweight|underweight|buy|sell|neutral|outperform)",
                  title + " " + summary, re.I)
    if m:
        rating = m.group(1)
    return BrokerItem(
        firm=name, firm_key=firm_key, title=title.strip(),
        url=(url or "").strip(), summary=(summary or "").strip(),
        source=source or name, published=ts, rating=rating,
        sentiment=label, score=score)


def _fmt_quote(q: Dict) -> str:
    price = q.get("price")
    pct = q.get("change_pct")
    if price is None:
        return ""
    ptxt = f"{price:.3f}".rstrip("0").rstrip(".")
    return f"{ptxt}({pct:+.2f}%)" if isinstance(pct, (int, float)) else ptxt

# ---------------- 离线样本（仅自检/离线模式） ----------------

SAMPLE_RAW = [
    {"firm_key": "cicc",
     "title": "中金：维持腾讯控股跑赢大市评级，上调目标价至 720 港元",
     "url": "https://example.com/cicc-tencent",
     "summary": "中金看好 AI 资本开支与游戏流水韧性",
     "source": "中金公司研究", "date": "2026-10-18T09:30:00+08:00"},
    {"firm_key": "citic",
     "title": "中信证券国际：港股策略周报，南向资金回流配置高息股",
     "url": "https://example.com/citic-weekly",
     "summary": "建议增配银行与公用事业等防御板块",
     "source": "中信证券国际", "date": "2026-10-17T15:00:00+08:00"},
    {"firm_key": "gtja",
     "title": "国泰君安国际下调某内房股评级至中性，销售复苏慢于预期",
     "url": "https://example.com/gtja-property",
     "summary": "下调盈利预测与目标价",
     "source": "国泰君安国际证券", "date": "2026-10-17T11:00:00+08:00"},
    {"firm_key": "futu",
     "title": "富途第三季总营收同比增长，全球开户数创新高",
     "url": "https://example.com/futu-q3",
     "summary": "管理层称海外市场扩张顺利",
     "source": "富途资讯", "date": "2026-10-18T20:00:00+08:00"},
    {"firm_key": "bright",
     "title": "耀才证券：港股新股热潮带动孖展借贷需求，IPO 融资额度充足",
     "url": "https://example.com/bright-ipo",
     "summary": "行政总裁接受本地媒体访问谈 IPO 市场",
     "source": "本地媒体", "date": "2026-10-16T14:00:00+08:00"},
    {"firm_key": "boci",
     "title": "中银国际：美联储年内或再降息，维持港股增持观点",
     "url": "https://example.com/boci-fed",
     "summary": "预计流动性环境利好高息资产",
     "source": "中银国际研究", "date": "2026-10-15T10:00:00+08:00"},
]

SAMPLE_QUOTES = {
    "cicc": {"label": "3908.HK 中金公司", "code": "03908",
             "price": 21.5, "change_pct": 1.89},
    "futu": {"label": "FUTU (NASDAQ) 富途控股", "code": "FUTU",
             "price": 152.3, "change_pct": -0.62},
}


def _finalize_agg(pack: HkBrokersPack) -> HkBrokersPack:
    all_items = [i for v in pack.reports.values() for i in v]
    pos = sum(1 for i in all_items if i.sentiment == "利好")
    neg = sum(1 for i in all_items if i.sentiment == "利空")
    mean = round(sum(i.score for i in all_items) / len(all_items), 2) \
        if all_items else 0.0
    pack.agg = {
        "n": len(all_items), "pos": pos, "neg": neg,
        "neu": len(all_items) - pos - neg, "mean": mean,
        "firms_hit": len(pack.reports), "firms_total": len(FIRMS),
        "quotes_hit": len(pack.quotes),
    }
    return pack


def demo_pack(now: datetime | None = None) -> HkBrokersPack:
    pack = HkBrokersPack(hours=DEFAULT_HOURS, topic="港股市场")
    for raw in SAMPLE_RAW:
        item = _mk_item(raw["firm_key"], raw["title"], raw["url"],
                        raw["summary"], _parse_time(raw["date"]),
                        raw["source"])
        pack.reports.setdefault(raw["firm_key"], []).append(item)
    pack.quotes = dict(SAMPLE_QUOTES)
    return _finalize_agg(pack)

# ---------------- 在线采集 ----------------


def _fetch_via_deepseek(query: str, firm_key: str, hours: int,
                        timeout: int) -> List[BrokerItem]:
    try:
        import deepseek_search
    except ImportError as e:
        raise RuntimeError(f"deepseek_search 不可用: {e}") from e
    rows = deepseek_search.fetch_deepseek_search(query, timeout=timeout,
                                                 hours=hours)
    firm = FIRM_MAP.get(firm_key, {})
    aliases = [a.lower() for a in firm.get("aliases", [])]
    items: List[BrokerItem] = []
    for r in rows:
        title = r.get("title", "")
        if len(title) < 6:
            continue
        hay = (title + " " + r.get("summary", "")).lower()
        if aliases and not any(a in hay for a in aliases):
            continue  # 与该券商无关的结果不计入该家
        items.append(_mk_item(firm_key, title, r.get("url", ""),
                              r.get("summary", ""), r.get("ts"),
                              r.get("source", "")))
        if len(items) >= 5:
            break
    return items


def _firm_queries(firm_key: str) -> List[str]:
    """短查询：券商全名 + 1~3 个财经关键词。切忌把整句 topic 塞进查询——
    Google News 按全词 AND 匹配，词越多召回越趋近于 0（2026-10 线上
    实测教训：'中金 港股市场 研报 评级 目标价 新闻' 0 命中，
    '中金公司 研报' 28 条）。topic 只作栏目口径，不入检索式。"""
    name = FIRM_MAP.get(firm_key, {}).get("name", firm_key)
    return [f"{name} 研报", f"{name} 评级 目标价"]


def _rows_to_items(rows, firm_key: str, hours: int, now: datetime,
                   source: str) -> List[BrokerItem]:
    """RSS 行 -> BrokerItem：时效过滤 + 标题含券商别名者优先
    （全无别名命中时按查询相关性顺序兜底接受）。"""
    cutoff = now - timedelta(hours=hours)
    aliases = [a.lower()
               for a in FIRM_MAP.get(firm_key, {}).get("aliases", [])]
    fresh = []
    for r in rows:
        ts = r.get("ts")
        if ts and ts < cutoff:
            continue
        title = (r.get("title") or "").strip()
        if len(title) < 6:
            continue
        fresh.append((title, r.get("url", ""), ts))
    hit = [t for t in fresh if any(a in t[0].lower() for a in aliases)]
    picked = (hit or fresh)[:5]
    return [_mk_item(firm_key, t, u, "", ts, source) for t, u, ts in picked]


def _fetch_via_google(firm_key: str, hours: int,
                      timeout: int, now: datetime) -> List[BrokerItem]:
    """Google News RSS（真实新闻检索，主链路）。"""
    try:
        from stock_news_scan import parse_google_rss, http_request, GOOGLE_RSS
        import urllib.parse
        for query in _firm_queries(firm_key):
            q = urllib.parse.quote_plus(f"{query} when:7d")
            status, body = http_request(GOOGLE_RSS.format(q=q),
                                         timeout=timeout)
            if status != 200:
                raise RuntimeError(f"Google HTTP {status}")
            items = _rows_to_items(parse_google_rss(body), firm_key, hours,
                                   now, "Google新闻")
            if items:
                return items
        return []
    except Exception as e:
        raise RuntimeError(f"Google 搜索失败: {e}") from e


def _fetch_via_bing(firm_key: str, hours: int,
                    timeout: int, now: datetime) -> List[BrokerItem]:
    """Bing News RSS（备援真实新闻检索）。"""
    try:
        from stock_news_scan import http_request
        import urllib.parse
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        name = FIRM_MAP.get(firm_key, {}).get("name", firm_key)
        q = urllib.parse.quote_plus(f"{name} 研报")
        url = f"https://www.bing.com/news/search?q={q}&format=rss"
        status, body = http_request(url, timeout=timeout)
        if status != 200:
            raise RuntimeError(f"Bing HTTP {status}")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            root = ET.fromstring(body.encode("utf-8", "replace"))
        rows = []
        for it in root.iter("item"):
            try:
                ts = parsedate_to_datetime(it.findtext("pubDate", "") or "")
            except (TypeError, ValueError):
                ts = None
            rows.append({"title": it.findtext("title", "") or "",
                         "url": it.findtext("link", "") or "", "ts": ts})
        return _rows_to_items(rows, firm_key, hours, now, "Bing资讯")
    except Exception as e:
        raise RuntimeError(f"Bing 搜索失败: {e}") from e


def _collect_quotes(target_keys: List[str]) -> Dict[str, Dict]:
    """上市券商股价快照（hk_quote 统一入口）；全部失败返回空 dict。"""
    try:
        from hk_quote import fetch_quote
    except ImportError:
        return {}
    quotes: Dict[str, Dict] = {}
    for key in target_keys:
        firm = FIRM_MAP.get(key, {})
        code = firm.get("listing_code") or ""
        if not code:
            continue
        try:
            q = fetch_quote(code)
        except Exception:  # noqa: BLE001 —— 单家隔离
            q = None
        if q and q.get("price") is not None:
            quotes[key] = {"label": firm.get("listing_label", code),
                           "code": code, "price": q.get("price"),
                           "change_pct": q.get("change_pct")}
    return quotes


def collect_hk_brokers(topic: str = "港股市场", hours: int = DEFAULT_HOURS,
                       timeout: int = 12, only: List[str] | None = None,
                       use_deepseek: bool = True,
                       with_quotes: bool = True) -> HkBrokersPack:
    """聚合香港券商新闻与研报动态 + 上市券商股价快照（逐家隔离）。"""
    now = _now()
    pack = HkBrokersPack(hours=hours, topic=topic)
    target_keys = only or [f["key"] for f in FIRMS]
    has_ds = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    # 主链路为真实新闻检索（Google News RSS -> Bing RSS 短查询）；
    # DeepSeek 生成式搜索仅作末位兜底（其结果为模型生成、无真实检索）。
    rss_timeout = min(timeout, 10)  # RSS 单次 1~3s，12 家逐家限时防拖长

    dead_streak = 0
    for key in target_keys:
        firm = FIRM_MAP.get(key)
        if not firm:
            pack.errors.append(f"{key}：未知券商")
            continue
        items: List[BrokerItem] = []
        errs: List[str] = []
        transport_dead = True  # 两路 RSS 都抛错才算网络源不可达
        for fetcher in (_fetch_via_google, _fetch_via_bing):
            try:
                items = fetcher(key, hours, rss_timeout, now)
                transport_dead = False
                if items:
                    break
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:80])
        if not items and use_deepseek and has_ds:
            try:
                items = _fetch_via_deepseek(
                    f"{firm['name']} 研报 评级 目标价", key, hours, timeout)
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:80])
        if items:
            seen, uniq = set(), []
            for it in items:
                if it.title not in seen:
                    uniq.append(it)
                    seen.add(it.title)
            pack.reports[key] = uniq[:5]
            dead_streak = 0
        else:
            detail = "；".join(errs) if errs else f"{hours}h 内无命中"
            pack.errors.append(f"{firm['name']}：{detail}")
            # 开局连续 3 家两路 RSS 全抛错：判定网络源不可达，快速止损
            dead_streak = dead_streak + 1 if transport_dead else 0
            if transport_dead and not pack.reports and dead_streak >= 3:
                pack.errors.append("新闻检索源连续不可达，已快速止损")
                break

    if with_quotes:
        pack.quotes = _collect_quotes(target_keys)
        if not pack.quotes:
            pack.errors.append("券商股价快照：本次未获取到（行情源失败或离线）")
    return _finalize_agg(pack)

# ---------------- 渲染 ----------------


def _quote_strip(pack: HkBrokersPack) -> str:
    if not pack.quotes:
        return ""
    parts = []
    for key in [f["key"] for f in FIRMS]:
        q = pack.quotes.get(key)
        if q:
            parts.append(f"{q['label'].split()[0]} {_fmt_quote(q)}")
    return "｜".join(parts)


def render_hk_brokers_rule(topic: str, pack: HkBrokersPack) -> str:
    a = pack.agg
    lines = [
        f"**{topic} · 香港券商新闻与数据（{pack.hours}h，"
        f"{a.get('firms_hit', 0)}/{a.get('firms_total', len(FIRMS))} 家命中）**（本地聚合）",
        "",
    ]
    strip = _quote_strip(pack)
    if strip:
        lines += [f"- **上市券商股价（抓取时点）**：{strip}", ""]
    lines += ["| 券商 | 方向/评级 | 最新动态 | 时间 |",
              "|---|---|---|---|"]
    for firm in FIRMS:
        items = pack.reports.get(firm["key"])
        if not items:
            continue
        for it in items[:3]:
            title = it.title[:40] + ("…" if len(it.title) > 40 else "")
            title = title.replace("|", "/")
            ts = it.published.strftime("%m-%d") if it.published else "近期"
            lines.append(f"| {it.firm} | {it.rating or it.sentiment} | "
                         f"{title} | {ts} |")
    if a.get("n", 0) == 0:
        lines.append("| — | — | 窗口内未命中（见数据缺口） | — |")
    stance = "偏多" if a.get("mean", 0) > 0.15 else (
        "偏空" if a.get("mean", 0) < -0.15 else "中性")
    lines += [
        "",
        f"- **汇总**：{a.get('n', 0)} 条动态 / {a.get('firms_hit', 0)} 家，"
        f"利好 {a.get('pos', 0)} / 利空 {a.get('neg', 0)} / 中性 {a.get('neu', 0)}，"
        f"均值 {a.get('mean', 0):+.2f} → **{stance}**；股价快照 {a.get('quotes_hit', 0)} 家",
        "> 数据来源：DeepSeek AI 搜索（主）+ Google 新闻 RSS（备）逐家检索，股价为免费行情源抓取时点值",
        "> ⚠️ 非投资建议，仅供参考，研报原文以各券商官方披露为准。",
    ]
    if pack.errors:
        lines.append("")
        lines.append("**数据缺口**")
        lines += [f"- ⚠️ {e}" for e in pack.errors[:8]]
    return "\n".join(lines)


def hk_brokers_context(pack: HkBrokersPack, max_per_firm: int = 3) -> str:
    a = pack.agg
    lines = [
        f"【香港券商新闻与数据：{pack.topic}，窗口 {pack.hours}h，命中 "
        f"{a.get('firms_hit', 0)}/{a.get('firms_total', len(FIRMS))} 家，共 {a.get('n', 0)} 条】",
        f"情绪 利好{a.get('pos', 0)}/利空{a.get('neg', 0)}/中性{a.get('neu', 0)}，均值 {a.get('mean', 0):+.2f}",
    ]
    strip = _quote_strip(pack)
    if strip:
        lines.append(f"上市券商股价（抓取时点）：{strip}")
    for firm in FIRMS:
        items = pack.reports.get(firm["key"])
        if not items:
            continue
        lines.append(f"▼ {firm['name']}（{len(items)}条）：")
        for it in items[:max_per_firm]:
            lines.append(f"  - [{it.sentiment}{it.score:+.2f}] {it.title}"
                         f"（{it.rating or '—'} · {it.source}）")
    if pack.errors:
        lines.append("▼ 数据缺口：" + "；".join(pack.errors[:5]))
    return "\n".join(lines)


def render_hk_brokers_appendix(pack: HkBrokersPack,
                               max_per_firm: int | None = None) -> str:
    lines = ["---", f"🏦 **香港券商新闻与数据（{pack.agg.get('n', 0)}条，{pack.hours}h）**"]
    strip = _quote_strip(pack)
    if strip:
        lines.append(f"\n上市券商股价（抓取时点）：{strip}")
    for firm in FIRMS:
        items = pack.reports.get(firm["key"])
        if not items:
            continue
        show = items[:max_per_firm] if max_per_firm else items
        qtxt = ""
        q = pack.quotes.get(firm["key"])
        if q:
            qtxt = f"，{q['label']} {_fmt_quote(q)}"
        lines.append(f"\n**{firm['name']}（{len(items)}条{qtxt}）**")
        for idx, it in enumerate(show, 1):
            ts = it.published.strftime("%Y-%m-%d") if it.published else "近期"
            lines.append(f"{idx}. [{it.sentiment} {it.score:+.2f}] {it.title}"
                         f" — {it.source} · {ts} {it.rating}".rstrip())
            if it.url:
                lines.append(f"   {it.url}")
            if it.summary:
                lines.append(f"   > {it.summary[:80]}")
    if pack.errors:
        lines.append("\n**数据缺口**")
        lines += [f"- ⚠️ {e}" for e in pack.errors]
    return "\n".join(lines)

# ---------------- 自检 ----------------


def selftest() -> int:
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("🏦 香港券商新闻与数据（离线自检，不触网）")
    check("券商清单 12 家", len(FIRMS) == 12 and "cicc" in FIRM_MAP
          and "futu" in FIRM_MAP and "bright" in FIRM_MAP)
    check("券商含别名与上市代码字段",
          all("aliases" in f and "listing_code" in f for f in FIRMS))
    listed = [f for f in FIRMS if f["listing_code"]]
    check("有股价代码的券商 10 家", len(listed) == 10)

    pack = demo_pack()
    check("演示包 6 条/6 家", pack.agg["n"] == 6 and pack.agg["firms_hit"] == 6)
    check("情绪打标 利好/利空", pack.agg["pos"] >= 2 and pack.agg["neg"] >= 1)
    check("演示股价快照 2 家", pack.agg["quotes_hit"] == 2
          and "3908" in _quote_strip(pack))

    md = render_hk_brokers_rule("香港券商动态", pack)
    check("渲染含表格/股价条/机构",
          "| 券商 |" in md and "上市券商股价" in md and "富途" in md)
    ctx = hk_brokers_context(pack)
    check("上下文含机构与情绪", "中金公司" in ctx and "利好" in ctx)
    appx = render_hk_brokers_appendix(pack)
    check("附录含明细与链接", "https://example.com" in appx
          and "香港券商新闻与数据" in appx)
    appx2 = render_hk_brokers_appendix(pack, max_per_firm=1)
    check("附录 max_per_firm 生效", appx2.count("https://example.com") <= 6)

    item = _mk_item("cicc", "中金维持跑赢大市并上调目标价", "", "看好", None)
    check("评级提取与利好", item.rating != "" and item.sentiment == "利好")

    check("检索式为短查询（全名+关键词，不含 topic）",
          _firm_queries("cicc") == ["中金公司 研报", "中金公司 评级 目标价"])
    now = _now()
    rows = [
        {"title": "中金公司：维持跑赢行业评级", "url": "u1",
         "ts": now - timedelta(hours=2)},
        {"title": "中金公司：一个月前的旧闻", "url": "u2",
         "ts": now - timedelta(days=30)},
        {"title": "券商研报掘金汇总", "url": "u3",
         "ts": now - timedelta(hours=1)},
    ]
    got = _rows_to_items(rows, "cicc", 48, now, "Google新闻")
    check("RSS 行过滤过期+别名优先",
          len(got) == 1 and got[0].url == "u1"
          and got[0].source == "Google新闻")

    print(f"\n{'✅ 香港券商栏目自检通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0

# ---------------- CLI ----------------


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="香港券商新闻与数据栏目")
    p.add_argument("--topic", default="港股市场", help="检索主题，如 港股市场 / IPO")
    p.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    p.add_argument("--only", default="", help="仅指定券商 key，逗号分隔，如 cicc,futu")
    p.add_argument("--timeout", type=int, default=12)
    p.add_argument("--no-deepseek", action="store_true")
    p.add_argument("--no-quotes", action="store_true", help="不抓股价快照")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true", help="离线自检")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if args.selftest:
        return selftest()

    only = [s.strip() for s in args.only.replace("，", ",").split(",")
            if s.strip()] or None
    if os.environ.get("HK_BROKERS_OFFLINE") == "1":
        pack = demo_pack()
        pack.topic, pack.hours = args.topic, args.hours
    else:
        pack = collect_hk_brokers(
            topic=args.topic, hours=args.hours, timeout=args.timeout,
            only=only, use_deepseek=not args.no_deepseek,
            with_quotes=not args.no_quotes)

    if args.json:
        print(json.dumps({
            "topic": pack.topic, "hours": pack.hours, "agg": pack.agg,
            "quotes": pack.quotes,
            "reports": {
                k: [{"title": i.title, "url": i.url, "summary": i.summary,
                     "source": i.source, "sentiment": i.sentiment,
                     "score": i.score, "rating": i.rating,
                     "published": i.published.isoformat() if i.published else None}
                    for i in v]
                for k, v in pack.reports.items()},
            "errors": pack.errors,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_hk_brokers_rule(args.topic, pack))
        print("\n" + render_hk_brokers_appendix(pack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
