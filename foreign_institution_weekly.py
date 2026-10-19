#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
foreign_institution_weekly.py — 国外专业投资机构一周研报栏目

新增栏目：聚合 Goldman Sachs / Morgan Stanley / JPMorgan / BofA / Citi / UBS / 
Deutsche Bank / Barclays / HSBC / Morningstar / Bernstein 等国外顶级投行/研究机构
在最近一周（默认 168h）的研报、观点、评级调整与策略周报。

设计：
- 纯标准库 + Google News RSS（主）/ Bing News RSS（备）真实新闻检索，
  deepseek_search 生成式搜索仅末位兜底（stock_news_scan 提供 RSS 解析）
- 任何单机构失败只记数据缺口，不中断整体
- 支持离线样本自检（不触网）与在线真抓取（真实检索无需 API Key）
- 产出 Markdown 栏目内容，供 pushplus_deepseek / server_dashboard / stock_report 调用

用法：
    python foreign_institution_weekly.py --selftest
    python foreign_institution_weekly.py --hours 168 --topic "全球市场" --json
    python foreign_institution_weekly.py --hours 168 --topic "AI 算力" --dry-run
    python pushplus_deepseek.py --template foreign_weekly --topic "全球市场一周研报" --hours 168 --dry-run

模板名：foreign_weekly（已注册到 pushplus_deepseek.TEMPLATES）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Dict

UTC = timezone.utc
CST = timezone(timedelta(hours=8), "CST")
DEFAULT_HOURS = 168  # 一周

# ---------------- 国外机构清单 ----------------

INSTITUTIONS: List[Dict[str, any]] = [
    {
        "key": "goldman",
        "name": "高盛 Goldman Sachs",
        "aliases": ["Goldman Sachs", "Goldman", "高盛"],
        "focus": ["美股策略", "宏观", "大宗", "AI"],
        "search_terms": ["Goldman Sachs research weekly", "Goldman Sachs outlook"],
        "color": "#1f4bff",
    },
    {
        "key": "morgan_stanley",
        "name": "摩根士丹利 Morgan Stanley",
        "aliases": ["Morgan Stanley", "大摩"],
        "focus": ["全球策略", "科技", "中国股票"],
        "search_terms": ["Morgan Stanley research weekly", "Morgan Stanley strategy"],
        "color": "#0a5a3a",
    },
    {
        "key": "jpmorgan",
        "name": "摩根大通 JPMorgan",
        "aliases": ["JPMorgan", "J.P. Morgan", "小摩"],
        "focus": ["宏观", "利率", "新兴市场"],
        "search_terms": ["JPMorgan research weekly", "JPMorgan outlook"],
        "color": "#000000",
    },
    {
        "key": "bofa",
        "name": "美银 BofA Securities",
        "aliases": ["Bank of America", "BofA", "美银"],
        "focus": ["资金流", "量化", "信用"],
        "search_terms": ["BofA Global Research weekly", "Bank of America strategy"],
        "color": "#012169",
    },
    {
        "key": "citi",
        "name": "花旗 Citi Research",
        "aliases": ["Citi", "Citigroup", "花旗"],
        "focus": ["全球宏观", "外汇", "大宗"],
        "search_terms": ["Citi Research weekly", "Citi Global Outlook"],
        "color": "#003b70",
    },
    {
        "key": "ubs",
        "name": "瑞银 UBS",
        "aliases": ["UBS", "瑞银"],
        "focus": ["全球配置", "中国", "ESG"],
        "search_terms": ["UBS research weekly", "UBS Global Outlook"],
        "color": "#e60000",
    },
    {
        "key": "deutsche",
        "name": "德银 Deutsche Bank",
        "aliases": ["Deutsche Bank", "德意志银行"],
        "focus": ["欧股", "宏观", "信用"],
        "search_terms": ["Deutsche Bank research weekly"],
        "color": "#0018a8",
    },
    {
        "key": "barclays",
        "name": "巴克莱 Barclays",
        "aliases": ["Barclays", "巴克莱"],
        "focus": ["英股", "宏观", "策略"],
        "search_terms": ["Barclays research weekly"],
        "color": "#00aeef",
    },
    {
        "key": "hsbc",
        "name": "汇丰 HSBC Global Research",
        "aliases": ["HSBC", "汇丰"],
        "focus": ["亚洲", "中国", "利率"],
        "search_terms": ["HSBC Global Research weekly"],
        "color": "#db0011",
    },
    {
        "key": "morningstar",
        "name": "晨星 Morningstar",
        "aliases": ["Morningstar", "晨星"],
        "focus": ["估值", "基金", "ETF"],
        "search_terms": ["Morningstar weekly outlook", "Morningstar research"],
        "color": "#ff6600",
    },
    {
        "key": "bernstein",
        "name": "伯恩斯坦 Bernstein",
        "aliases": ["Bernstein", "伯恩斯坦", "Sanford Bernstein"],
        "focus": ["科技", "中概", "半导体"],
        "search_terms": ["Bernstein research weekly", "Bernstein China"],
        "color": "#6a0dad",
    },
    {
        "key": "nomura",
        "name": "野村 Nomura",
        "aliases": ["Nomura", "野村"],
        "focus": ["日本", "亚洲", "中国"],
        "search_terms": ["Nomura research weekly"],
        "color": "#d50000",
    },
]

INSTITUTION_MAP = {it["key"]: it for it in INSTITUTIONS}

# ---------------- 数据结构 ----------------

@dataclass
class ReportItem:
    institution: str
    institution_key: str
    title: str
    url: str = ""
    summary: str = ""
    source: str = ""
    published: datetime | None = None
    rating: str = ""  # 增持/中性/减持 等
    sentiment: str = "中性"  # 利好/利空/中性
    score: float = 0.0

@dataclass
class ForeignWeeklyPack:
    hours: int
    topic: str = ""
    reports: Dict[str, List[ReportItem]] = field(default_factory=dict)  # key -> items
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
    s = str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=CST)
    except Exception:
        return None

def _score_text(text: str) -> tuple[float, str]:
    pos_words = ["上调", "增持", "买入", "利好", "超配", "看好", "上行", "增长", "乐观", "upgrade", "overweight", "buy", "bullish", "outperform"]
    neg_words = ["下调", "减持", "卖出", "利空", "低配", "看空", "下行", "风险", "悲观", "downgrade", "underweight", "sell", "bearish", "underperform"]
    t = text.lower()
    pos = sum(t.count(w.lower()) for w in pos_words)
    neg = sum(t.count(w.lower()) for w in neg_words)
    total = pos + neg
    score = 0.0 if total == 0 else max(-1.0, min(1.0, (pos - neg) / total))
    label = "利好" if score > 0.15 else ("利空" if score < -0.15 else "中性")
    return round(score, 2), label

def _mk_item(inst_key: str, title: str, url: str, summary: str, ts, source: str = "") -> ReportItem:
    inst = INSTITUTION_MAP.get(inst_key, {})
    name = inst.get("name", inst_key)
    score, label = _score_text(f"{title} {summary}")
    # 尝试提取评级
    rating = ""
    m = re.search(r"(增持|买入|中性|减持|卖出|上调|下调|overweight|underweight|buy|sell|neutral|outperform)", title + " " + summary, re.I)
    if m:
        rating = m.group(1)
    return ReportItem(
        institution=name,
        institution_key=inst_key,
        title=title.strip(),
        url=url.strip(),
        summary=summary.strip(),
        source=source or name,
        published=ts,
        rating=rating,
        sentiment=label,
        score=score,
    )

# ---------------- 离线样本 ----------------

SAMPLE_RAW = [
    {
        "institution_key": "goldman",
        "title": "Goldman Sachs：上调英伟达目标价至 $220，维持买入评级，AI算力需求超预期",
        "url": "https://example.com/goldman-nvda",
        "summary": "高盛认为AI基础设施支出将持续，数据中心需求强劲",
        "source": "Goldman Sachs Research",
        "date": "2026-10-15T10:00:00+08:00",
    },
    {
        "institution_key": "morgan_stanley",
        "title": "Morgan Stanley：全球策略周报 - 美股科技股估值回调提供入场机会，维持标普500增持",
        "url": "https://example.com/ms-weekly",
        "summary": "大摩指出科技股回调后风险回报改善，关注Q4财报季",
        "source": "Morgan Stanley",
        "date": "2026-10-14T09:00:00+08:00",
    },
    {
        "institution_key": "jpmorgan",
        "title": "JPMorgan：下调特斯拉评级至中性，电动车竞争加剧",
        "url": "https://example.com/jpm-tsla",
        "summary": "小摩认为价格战将压缩毛利率",
        "source": "JPMorgan",
        "date": "2026-10-13T14:00:00+08:00",
    },
    {
        "institution_key": "bofa",
        "title": "BofA：资金流周报 - 全球股票基金净流入 $120亿，科技领跑",
        "url": "https://example.com/bofa-flow",
        "summary": "美银数据显示科技与通信服务获最多资金",
        "source": "BofA Global Research",
        "date": "2026-10-12T16:00:00+08:00",
    },
    {
        "institution_key": "ubs",
        "title": "UBS：上调中国互联网板块至增持，阿里巴巴、腾讯为首选",
        "url": "https://example.com/ubs-china",
        "summary": "瑞银看好平台经济监管常态化与AI变现",
        "source": "UBS",
        "date": "2026-10-11T11:00:00+08:00",
    },
]

def demo_pack(now: datetime | None = None) -> ForeignWeeklyPack:
    now = now or _now()
    pack = ForeignWeeklyPack(hours=DEFAULT_HOURS, topic="全球市场")
    for raw in SAMPLE_RAW:
        ts = _parse_time(raw["date"])
        item = _mk_item(raw["institution_key"], raw["title"], raw["url"], raw["summary"], ts, raw["source"])
        pack.reports.setdefault(raw["institution_key"], []).append(item)
    # 聚合
    all_items = [i for v in pack.reports.values() for i in v]
    pos = sum(1 for i in all_items if i.sentiment == "利好")
    neg = sum(1 for i in all_items if i.sentiment == "利空")
    mean = round(sum(i.score for i in all_items) / len(all_items), 2) if all_items else 0.0
    pack.agg = {
        "n": len(all_items),
        "pos": pos,
        "neg": neg,
        "neu": len(all_items) - pos - neg,
        "mean": mean,
        "institutions_hit": len(pack.reports),
        "institutions_total": len(INSTITUTIONS),
    }
    return pack

# ---------------- 在线采集 ----------------

def _fetch_via_deepseek(query: str, inst_key: str, hours: int, timeout: int) -> List[ReportItem]:
    try:
        import deepseek_search
    except ImportError as e:
        raise RuntimeError(f"deepseek_search 不可用: {e}") from e
    rows = deepseek_search.fetch_deepseek_search(query, timeout=timeout, hours=hours)
    items: List[ReportItem] = []
    for r in rows:
        title = r.get("title", "")
        if len(title) < 6:
            continue
        # 仅保留与机构相关的
        low = title.lower()
        inst = INSTITUTION_MAP.get(inst_key, {})
        aliases = [a.lower() for a in inst.get("aliases", [])]
        if not any(alias in low or alias in r.get("summary","").lower() for alias in aliases):
            # 若查询已带机构名，允许不二次过滤；否则过滤
            if inst_key not in query.lower() and inst["name"].split()[0].lower() not in low:
                # 宽松：只要标题含“研报/评级/目标价”等也保留
                if not re.search(r"研报|评级|目标价|research|rating|target|outlook|strategy", title, re.I):
                    continue
        ts = r.get("ts")
        items.append(_mk_item(inst_key, title, r.get("url",""), r.get("summary",""), ts, r.get("source","")))
        if len(items) >= 5:
            break
    return items

def _inst_queries(inst_key: str) -> List[str]:
    """短查询：机构中文简称 + 1~3 个关键词。切忌把整句 topic 与英文长串
    混塞进查询——Google News 全词 AND 匹配会把召回压到 0（2026-10
    线上实测：'Goldman Sachs 全球市场 research report weekly rating
    target' 0 命中，'高盛 评级 目标价' 34 条）。topic 只作栏目口径。"""
    inst = INSTITUTION_MAP.get(inst_key, {})
    zh = (inst.get("name") or inst_key).split()[0]
    return [f"{zh} 研报", f"{zh} 评级 目标价"]


def _rows_to_items(rows, inst_key: str, hours: int, now: datetime,
                   source: str) -> List[ReportItem]:
    """RSS 行 -> ReportItem：时效过滤 + 标题含机构别名者优先
    （全无别名命中时按查询相关性顺序兜底接受）。"""
    cutoff = now - timedelta(hours=hours)
    inst = INSTITUTION_MAP.get(inst_key, {})
    aliases = [a.lower() for a in inst.get("aliases", [])]
    zh = (inst.get("name") or "").split()
    if zh:
        aliases.append(zh[0].lower())  # 如「摩根士丹利」未列入别名也要认
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
    return [_mk_item(inst_key, t, u, "", ts, source) for t, u, ts in picked]


def _fetch_via_google(inst_key: str, hours: int, timeout: int, now: datetime) -> List[ReportItem]:
    """Google News RSS（真实新闻检索，主链路）。"""
    try:
        from stock_news_scan import parse_google_rss, http_request, GOOGLE_RSS
        import urllib.parse
        for query in _inst_queries(inst_key):
            q = urllib.parse.quote_plus(f"{query} when:7d")
            status, body = http_request(GOOGLE_RSS.format(q=q), timeout=timeout)
            if status != 200:
                raise RuntimeError(f"Google HTTP {status}")
            items = _rows_to_items(parse_google_rss(body), inst_key, hours,
                                   now, "Google新闻")
            if items:
                return items
        return []
    except Exception as e:
        raise RuntimeError(f"Google 搜索失败: {e}") from e


def _fetch_via_bing(inst_key: str, hours: int, timeout: int, now: datetime) -> List[ReportItem]:
    """Bing News RSS（备援真实新闻检索）。"""
    try:
        from stock_news_scan import http_request
        import urllib.parse
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        zh = (INSTITUTION_MAP.get(inst_key, {}).get("name")
              or inst_key).split()[0]
        q = urllib.parse.quote_plus(f"{zh} 研报")
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
        return _rows_to_items(rows, inst_key, hours, now, "Bing资讯")
    except Exception as e:
        raise RuntimeError(f"Bing 搜索失败: {e}") from e

def collect_foreign_weekly(topic: str = "全球市场", hours: int = DEFAULT_HOURS,
                           timeout: int = 12, only: List[str] | None = None,
                           use_deepseek: bool = True) -> ForeignWeeklyPack:
    """
    聚合国外机构一周研报。
    - topic: 关注主题，如 "AI 算力"、"港股互联网"、"全球市场"
    - hours: 默认 168h 一周
    - only: 仅采集指定机构 key，如 ["goldman","morgan_stanley"]
    - use_deepseek: 是否允许 DeepSeek 生成式搜索作末位兜底（需 Key）；
      主链路为 Google/Bing 真实新闻检索，不受此开关影响
    """
    now = _now()
    pack = ForeignWeeklyPack(hours=hours, topic=topic)

    target_keys = only or [it["key"] for it in INSTITUTIONS]
    has_ds_key_global = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    # 主链路为真实新闻检索（Google News RSS -> Bing RSS 短查询）；
    # DeepSeek 生成式搜索仅作末位兜底（结果为模型生成、无真实检索）。
    rss_timeout = min(timeout, 10)

    dead_streak = 0
    for inst_key in target_keys:
        if inst_key not in INSTITUTION_MAP:
            pack.errors.append(f"{inst_key}：未知机构")
            continue
        inst = INSTITUTION_MAP[inst_key]
        items: List[ReportItem] = []
        errs: List[str] = []
        transport_dead = True  # 两路 RSS 都抛错才算网络源不可达
        for fetcher in (_fetch_via_google, _fetch_via_bing):
            try:
                items = fetcher(inst_key, hours, rss_timeout, now)
                transport_dead = False
                if items:
                    break
            except Exception as e:
                errs.append(str(e)[:80])
        if not items and use_deepseek and has_ds_key_global:
            try:
                q = f"{inst['name']} 研报 评级 目标价"
                items = _fetch_via_deepseek(q, inst_key, hours, timeout)
            except Exception as e:
                errs.append(str(e)[:80])

        if items:
            seen = set()
            uniq = []
            for it in items:
                if it.title not in seen:
                    uniq.append(it)
                    seen.add(it.title)
            pack.reports[inst_key] = uniq[:5]
            dead_streak = 0
        else:
            detail = "；".join(errs) if errs else "一周内无命中"
            pack.errors.append(f"{inst['name']}：{detail}")
            # 开局连续 3 家两路 RSS 全抛错：判定网络源不可达，快速止损
            dead_streak = dead_streak + 1 if transport_dead else 0
            if transport_dead and not pack.reports and dead_streak >= 3:
                pack.errors.append("新闻检索源连续不可达，已快速止损")
                break

    all_items = [i for v in pack.reports.values() for i in v]
    pos = sum(1 for i in all_items if i.sentiment == "利好")
    neg = sum(1 for i in all_items if i.sentiment == "利空")
    mean = round(sum(i.score for i in all_items) / len(all_items), 2) if all_items else 0.0
    pack.agg = {
        "n": len(all_items),
        "pos": pos,
        "neg": neg,
        "neu": len(all_items) - pos - neg,
        "mean": mean,
        "institutions_hit": len(pack.reports),
        "institutions_total": len(INSTITUTIONS),
    }
    return pack

# ---------------- 渲染 ----------------

def render_foreign_weekly_rule(topic: str, pack: ForeignWeeklyPack) -> str:
    a = pack.agg
    lines = [
        f"**{topic} · 国外专业投资机构一周研报（{pack.hours}h，{a.get('institutions_hit',0)}/{a.get('institutions_total',len(INSTITUTIONS))} 机构命中）**（本地聚合）",
        "",
        "| 机构 | 评级/方向 | 标题 | 来源 | 时间 |",
        "|---|---|---|---|---|",
    ]
    for inst_key in [it["key"] for it in INSTITUTIONS]:
        items = pack.reports.get(inst_key)
        if not items:
            continue
        for it in items[:3]:
            title = it.title[:36] + ("…" if len(it.title) > 36 else "")
            title = title.replace("|", "/")
            rating = it.rating or it.sentiment
            ts = it.published.strftime("%m-%d") if it.published else "本周"
            lines.append(f"| {it.institution} | {rating} | {title} | {it.source} | {ts} |")
    if len(lines) <= 4:
        lines.append("| — | — | 一周内未命中（见数据缺口） | — | — |")

    # 聚合
    stance = "偏多" if a.get("mean",0) > 0.15 else ("偏空" if a.get("mean",0) < -0.15 else "中性")
    lines += [
        "",
        f"- **汇总**：{a.get('n',0)} 篇研报 / {a.get('institutions_hit',0)} 机构，利好 {a.get('pos',0)} / 利空 {a.get('neg',0)} / 中性 {a.get('neu',0)}，均值 {a.get('mean',0):+.2f} → **{stance}**",
        f"- **覆盖机构**：{', '.join([INSTITUTION_MAP[k]['name'] for k in pack.reports.keys()]) or '无'}",
        "> 数据来源：DeepSeek AI 搜索（主）+ Google新闻 RSS（备），按机构别名与评级关键词过滤，逐机构隔离",
        "> ⚠️ 非投资建议，仅供参考，研报原文以机构官方披露为准。",
    ]
    if pack.errors:
        lines.append("")
        lines.append("**数据缺口**")
        for e in pack.errors[:8]:
            lines.append(f"- ⚠️ {e}")
    return "\n".join(lines)

def foreign_weekly_context(pack: ForeignWeeklyPack) -> str:
    a = pack.agg
    lines = [
        f"【国外机构一周研报：{pack.topic}，窗口 {pack.hours}h，命中 {a.get('institutions_hit',0)}/{a.get('institutions_total',len(INSTITUTIONS))} 机构，共 {a.get('n',0)} 篇】",
        f"情绪 利好{a.get('pos',0)}/利空{a.get('neg',0)}/中性{a.get('neu',0)}，均值 {a.get('mean',0):+.2f}",
    ]
    for inst_key, items in pack.reports.items():
        if not items:
            continue
        lines.append(f"▼ {INSTITUTION_MAP[inst_key]['name']}（{len(items)}篇）：")
        for it in items[:3]:
            lines.append(f"  - [{it.sentiment}{it.score:+.2f}] {it.title}（{it.rating or '—'} · {it.source}）")
    if pack.errors:
        lines.append("▼ 数据缺口：" + "；".join(pack.errors[:5]))
    return "\n".join(lines)

def render_foreign_weekly_appendix(pack: ForeignWeeklyPack,
                                   max_per_inst: int | None = None) -> str:
    lines = ["---", f"🏛 **国外机构一周研报明细（{pack.agg.get('n',0)}篇，{pack.hours}h）**"]
    for inst_key in [it["key"] for it in INSTITUTIONS]:
        items = pack.reports.get(inst_key)
        if not items:
            continue
        show = items[:max_per_inst] if max_per_inst else items
        lines.append(f"\n**{INSTITUTION_MAP[inst_key]['name']}（{len(items)}篇）**")
        for idx, it in enumerate(show, 1):
            ts = it.published.strftime("%Y-%m-%d") if it.published else "本周"
            lines.append(f"{idx}. [{it.sentiment} {it.score:+.2f}] {it.title} — {it.source} · {ts} {it.rating}")
            if it.url:
                lines.append(f"   {it.url}")
            if it.summary:
                lines.append(f"   > {it.summary[:80]}")
    if pack.errors:
        lines.append("\n**数据缺口**")
        for e in pack.errors:
            lines.append(f"- ⚠️ {e}")
    return "\n".join(lines)

# ---------------- 自检 ----------------

def selftest() -> int:
    fails = 0
    def check(name, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("🏛 国外专业投资机构一周研报（离线自检，不触网）")
    check("机构清单 12 家", len(INSTITUTIONS) >= 12 and "goldman" in INSTITUTION_MAP)
    check("机构含别名与焦点", all("aliases" in it and "focus" in it for it in INSTITUTIONS))

    pack = demo_pack()
    check("演示包 5 篇", pack.agg["n"] == 5 and pack.agg["institutions_hit"] == 5)
    check("情绪打标 利好/利空", pack.agg["pos"] >= 2 and pack.agg["neg"] >= 1)

    md = render_foreign_weekly_rule("全球市场", pack)
    check("渲染含表格与机构", "| 机构 |" in md and "Goldman" in md and "一周研报" in md)

    ctx = foreign_weekly_context(pack)
    check("上下文含机构与情绪", "Goldman" in ctx and "利好" in ctx)

    appx = render_foreign_weekly_appendix(pack)
    check("附录含明细与链接", "https://example.com" in appx and "一周研报明细" in appx)

    # 评分映射
    item = _mk_item("goldman", "Goldman Sachs 上调评级至买入", "", "看好AI", None)
    check("评级提取与利好", item.rating != "" and item.sentiment == "利好")

    check("检索式为短查询（中文简称+关键词，不含 topic）",
          _inst_queries("goldman") == ["高盛 研报", "高盛 评级 目标价"])
    now = _now()
    rows = [
        {"title": "高盛：维持港交所买入评级", "url": "u1",
         "ts": now - timedelta(hours=3)},
        {"title": "高盛：一周前的旧研报", "url": "u2",
         "ts": now - timedelta(days=9)},
        {"title": "外资大行观点汇总", "url": "u3",
         "ts": now - timedelta(hours=1)},
    ]
    got = _rows_to_items(rows, "goldman", 168, now, "Google新闻")
    check("RSS 行过滤过期+别名优先",
          len(got) == 1 and got[0].url == "u1"
          and got[0].source == "Google新闻")

    print(f"\n{'✅ 国外机构一周研报自检通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0

# ---------------- CLI ----------------

def main(argv: List[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="国外专业投资机构一周研报栏目")
    p.add_argument("--topic", default="全球市场", help="关注主题，如 AI 算力 / 港股互联网")
    p.add_argument("--hours", type=int, default=DEFAULT_HOURS, help="时间窗口小时数，默认 168 一周")
    p.add_argument("--only", default="", help="仅指定机构 key，逗号分隔，如 goldman,morgan_stanley")
    p.add_argument("--timeout", type=int, default=12)
    p.add_argument("--no-deepseek", action="store_true", help="禁用 DeepSeek，仅用 Google")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--selftest", action="store_true", help="离线自检")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if args.selftest:
        return selftest()

    only = [s.strip() for s in args.only.replace("，",",").split(",") if s.strip()] or None

    # 离线演示模式：无网络时使用样本
    if os.environ.get("FOREIGN_WEEKLY_OFFLINE") == "1":
        pack = demo_pack()
        pack.topic = args.topic
        pack.hours = args.hours
    else:
        try:
            pack = collect_foreign_weekly(
                topic=args.topic,
                hours=args.hours,
                timeout=args.timeout,
                only=only,
                use_deepseek=not args.no_deepseek,
            )
        except Exception as e:
            print(f"❌ 采集失败: {e}", file=sys.stderr)
            # 降级演示
            print("⚠️ 降级使用离线演示包", file=sys.stderr)
            pack = demo_pack()
            pack.topic = args.topic
            pack.hours = args.hours
            pack.errors.append(f"采集失败降级：{e}")

    if args.json:
        print(json.dumps({
            "topic": pack.topic,
            "hours": pack.hours,
            "agg": pack.agg,
            "reports": {
                k: [{"title": i.title, "url": i.url, "summary": i.summary,
                     "source": i.source, "sentiment": i.sentiment,
                     "score": i.score, "rating": i.rating,
                     "published": i.published.isoformat() if i.published else None}
                    for i in v]
                for k, v in pack.reports.items()
            },
            "errors": pack.errors,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_foreign_weekly_rule(pack.topic, pack))
        print("\n" + render_foreign_weekly_appendix(pack))
    return 0

if __name__ == "__main__":
    sys.exit(main())
