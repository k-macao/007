#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
source_names.py — 全仓库统一的「来源泛化名」映射与脱敏层（零第三方依赖）

设计目的：
  所有面向读者展示的推送正文、Markdown 表格、附录明细、AI 上下文提示词、
  数据新鲜度看板、字符走势图页眉、错误/数据缺口提示以及服务器大屏，
  一律展示**语义清晰且互不冲突的泛化来源名**（如「财经电报源」「宏观快讯源」
  「实时快讯主源」「公开新闻检索」「问答社区热榜」「实时行情主源」等），
  不再外露具体第三方网站、App、接口域名或模型供应商名称。

核心保证：
  1. 同表唯一性：全市场快讯 13 源、十八平台扫描 18 源、全网热榜 10 源、
     港股三源核验、美股四源核验在各自字典/表格中泛化名 **100% 一一对应不重名**，
     绝不因重名覆盖字典键或改变源计数。
  2. 标题脱敏：自动剥离电报/快讯开头的「财联社X月X日电，」「格隆汇X月X日电｜」
     等渠道导语前缀，同时完整保留正文中的公司/标的名称（如腾讯控股、阿里巴巴）。
  3. 异常脱敏：网络报错、HTTP 状态异常写入「数据缺口」前统一剥离原始 URL、
     接口域名、环境变量名与渠道品牌名。

用法：
  python3 source_names.py --selftest
  python3 source_names.py --list
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# ================================================================ 1. 核心泛化映射表

# 全市场快讯 13 源（pushplus_deepseek.FEED_SPECS + AI 智能检索）
FEED_SOURCE_MAP: dict[str, str] = {
    "MKTNews 快讯": "海外快讯源",
    "华尔街见闻 快讯": "宏观快讯源",
    "华尔街见闻 最新": "宏观资讯流",
    "华尔街见闻 最热": "宏观热读榜",
    "财联社 电报": "财经电报源",
    "财联社 深度": "财经深度源",
    "财联社 热门": "财经热文榜",
    "雪球 热门股票": "投资社区热榜",
    "格隆汇 事件": "港美股快讯源",
    "法布财经 快讯": "外汇商品快讯",
    "法布财经 头条": "市场头条源",
    "金十数据": "实时快讯主源",
    "DeepSeek 搜索": "AI 智能检索",
}

# 全网社媒与资讯热榜 10 源（newsnow_sources.FETCHERS）
HOTLIST_KEY_MAP: dict[str, str] = {
    "zhihu": "问答社区热榜",
    "douyin": "短视频热搜榜",
    "weibo": "社交实时热搜",
    "hupu": "论坛热议榜",
    "aihot": "科技热点榜",
    "zaobao": "亚太资讯源",
    "hk01": "港闻资讯源",
    "toutiao": "综合资讯热榜",
    "baidu": "搜索实时热点",
    "bilibili": "视频社区热榜",
}

# 行情与日线数据源（hk_quote / us_quote / pushplus_deepseek / stock_report）
QUOTE_SOURCE_MAP: dict[str, str] = {
    "腾讯财经": "实时行情主源",
    "东方财富": "实时行情备源",
    "东财": "实时行情备源",
    "Yahoo财经": "国际行情源",
    "Yahoo Finance": "国际行情源",
    "腾讯财经(美股)": "美股行情主源",
    "腾讯美股": "美股行情主源",
    "东方财富(美股)": "美股行情备源",
    "东财美股": "美股行情备源",
    "Stooq(延迟)": "延迟核验源",
    "Stooq": "延迟核验源",
    "tencent": "实时行情主源",
    "eastmoney": "实时行情备源",
    "yahoo": "国际行情源",
    "stooq": "延迟核验源",
    "us_tencent": "美股行情主源",
    "us_eastmoney": "美股行情备源",
    "us_yahoo": "国际行情核验源",
    "us_stooq": "延迟核验源",
    "YAHOO": "日线行情主源",
    "EASTMONEY": "日线行情备源",
}

# 其余常见渠道别名与历史写法统一归并表
EXTRA_SOURCE_ALIASES: dict[str, str] = {
    # 搜索与音视频渠道
    "Google新闻": "公开新闻检索",
    "Google 新闻": "公开新闻检索",
    "Google News": "公开新闻检索",
    "Google": "公开新闻检索",
    "google": "公开新闻检索",
    "Bing资讯": "备援新闻检索",
    "Bing新闻": "备援新闻检索",
    "Bing News": "备援新闻检索",
    "Bing": "备援新闻检索",
    "bing": "备援新闻检索",
    "YouTube·investtalk": "海外财经频道",
    "YouTube@investtalk": "海外财经频道",
    "YouTube": "海外财经频道",
    "youtube": "海外财经频道",
    "investtalk": "海外财经频道",
    "DeepSeek AI 搜索": "AI 智能检索",
    "DeepSeek AI": "AI 智能检索",
    "DeepSeek": "AI 智能检索",
    "deepseek": "AI 智能检索",
    # 财经快讯别名
    "MKTNews快讯": "海外快讯源",
    "MKTNews": "海外快讯源",
    "mktnews": "海外快讯源",
    "华尔街见闻快讯": "宏观快讯源",
    "华尔街见闻": "宏观快讯源",
    "wscn": "宏观快讯源",
    "财联社电报": "财经电报源",
    "财联社": "财经电报源",
    "cls": "财经电报源",
    "雪球热门股票": "投资社区热榜",
    "雪球热帖": "投资社区热榜",
    "雪球": "投资社区热榜",
    "xueqiu": "投资社区热榜",
    "格隆汇事件": "港美股快讯源",
    "格隆汇": "港美股快讯源",
    "gelonghui": "港美股快讯源",
    "法布财经": "外汇商品快讯",
    "FastBull": "外汇商品快讯",
    "fastbull": "外汇商品快讯",
    "金十": "实时快讯主源",
    "jin10": "实时快讯主源",
    "东方财富快讯": "财经资讯源",
    "新浪7x24": "门户快讯源",
    "新浪财经": "门户快讯源",
    "同花顺7x24": "终端快讯源",
    "同花顺": "终端快讯源",
    # 热榜与社媒别名（含大屏演示卡片旧写法）
    "知乎热榜": "问答社区热榜",
    "知乎热议": "问答社区热榜",
    "知乎": "问答社区热榜",
    "抖音热搜": "短视频热搜榜",
    "抖音热榜": "短视频热搜榜",
    "抖音": "短视频热搜榜",
    "微博实时热搜": "社交实时热搜",
    "微博财经": "社交实时热搜",
    "微博": "社交实时热搜",
    "虎扑热搜": "论坛热议榜",
    "虎扑步行街": "论坛热议榜",
    "虎扑": "论坛热议榜",
    "AI hot": "科技热点榜",
    "AI hot 追踪": "科技热点榜",
    "联合早报": "亚太资讯源",
    "香港01": "港闻资讯源",
    "今日头条热榜": "综合资讯热榜",
    "今日头条": "综合资讯热榜",
    "百度实时热点": "搜索实时热点",
    "百度热搜": "搜索实时热点",
    "百度": "搜索实时热点",
    "B站热榜": "视频社区热榜",
    "B站": "视频社区热榜",
    "哔哩哔哩": "视频社区热榜",
    # 第三方媒体出处泛化
    "香港经济日报": "财经媒体报道",
    "经济通": "财经媒体报道",
    "阿思达克": "财经媒体报道",
    "阿思达克财经网": "财经媒体报道",
    "财华社": "财经媒体报道",
    "信报": "财经媒体报道",
    "明报": "财经媒体报道",
    "南华早报": "财经媒体报道",
    "第一财经": "财经媒体报道",
    "证券时报": "财经媒体报道",
    "中国证券报": "财经媒体报道",
    "上海证券报": "财经媒体报道",
    "21世纪经济报道": "财经媒体报道",
    "每日经济新闻": "财经媒体报道",
    "界面新闻": "财经媒体报道",
    "澎湃新闻": "财经媒体报道",
    "财新": "财经媒体报道",
    "36氪": "财经媒体报道",
    "富途资讯": "机构市场资讯",
    "老虎资讯": "机构市场资讯",
    "本地媒体": "财经媒体报道",
    "新闻": "公开新闻检索",
    "Reuters": "国际财经媒体",
    "路透": "国际财经媒体",
    "Bloomberg": "国际财经媒体",
    "彭博": "国际财经媒体",
    "WSJ": "国际财经媒体",
    "华尔街日报": "国际财经媒体",
    "FT": "国际财经媒体",
    "金融时报": "国际财经媒体",
    "CNBC": "国际财经媒体",
}

ALL_SOURCE_MAP: dict[str, str] = {}
for _d in (FEED_SOURCE_MAP, HOTLIST_KEY_MAP, QUOTE_SOURCE_MAP, EXTRA_SOURCE_ALIASES):
    ALL_SOURCE_MAP.update(_d)


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip()).lower()


_NORM_LOOKUP: dict[str, str] = {_norm_key(k): v for k, v in ALL_SOURCE_MAP.items()}

# 已属于合法泛化名的集合（幂等保护）
CANONICAL_GENERALIZED_NAMES: frozenset[str] = frozenset(ALL_SOURCE_MAP.values()) | {
    "机构研报摘要",
    "财经媒体报道",
    "国际财经媒体",
    "公开市场资讯",
    "双路实时行情源",
    "境外四路行情源",
    "日线行情源",
    "静态快照 (STATIC DEMO)",
    "演示",
    "行情",
    "未获取到",
}


# ================================================================ 2. 泛化函数

def generalize_source(raw: str | None, default: str = "公开资讯源") -> str:
    """将任意原始渠道/数据源名称转为泛化名（幂等，保留文件缓存或标的名括号后缀）。"""
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    if s in CANONICAL_GENERALIZED_NAMES:
        return s

    # 保留文件缓存后缀，如 "腾讯财经 (文件缓存)" / "东方财富 (文件缓存-过期)"
    cache_suffix = ""
    m_cache = re.search(r"(\s*\(文件缓存[^)]*\))\s*$", s)
    if m_cache:
        cache_suffix = m_cache.group(1)
        s = s[:m_cache.start()].strip()

    # 保留新鲜度看板分组前缀，如 "扫·雪球 热门股票" / "外研·goldman"
    if s.startswith("扫·"):
        return f"扫·{generalize_source(s[2:], default=default)}{cache_suffix}"
    if s.startswith("外研·"):
        return f"{s}{cache_suffix}"

    # 直接命中或归一化命中
    hit = ALL_SOURCE_MAP.get(s) or _NORM_LOOKUP.get(_norm_key(s))
    if hit:
        return f"{hit}{cache_suffix}"

    # 形如 "Yahoo财经（阿里巴巴）" 的核验表行：保留括号内证券名，仅泛化前置数据源名
    m_paren = re.match(r"^([^（(]+?)([（(][^）)]+[）)])$", s)
    if m_paren:
        head, tail = m_paren.group(1).strip(), m_paren.group(2)
        head_hit = ALL_SOURCE_MAP.get(head) or _NORM_LOOKUP.get(_norm_key(head))
        if head_hit:
            return f"{head_hit}{tail}{cache_suffix}"

    # Google·香港经济日报 / Bing·xxx / YouTube·xxx 等前缀形式
    low = s.lower()
    if low.startswith(("google·", "google-", "google ")):
        return f"公开新闻检索{cache_suffix}"
    if low.startswith(("bing·", "bing-", "bing ")):
        return f"备援新闻检索{cache_suffix}"
    if low.startswith(("youtube", "yt·", "yt@")):
        return f"海外财经频道{cache_suffix}"
    if "deepseek" in low:
        return f"AI 智能检索{cache_suffix}"
    if "stooq" in low:
        return f"延迟核验源{cache_suffix}"
    if "yahoo" in low:
        return f"国际行情源{cache_suffix}"
    if "东方财富" in s or "东财" in s or "eastmoney" in low:
        return f"实时行情备源{cache_suffix}"
    if "腾讯" in s or "tencent" in low:
        return f"实时行情主源{cache_suffix}"

    # 其它部分匹配
    for kw, target in (
        ("财联社", "财经电报源"),
        ("华尔街见闻", "宏观快讯源"),
        ("格隆汇", "港美股快讯源"),
        ("金十", "实时快讯主源"),
        ("雪球", "投资社区热榜"),
        ("mktnews", "海外快讯源"),
        ("法布", "外汇商品快讯"),
        ("fastbull", "外汇商品快讯"),
        ("知乎", "问答社区热榜"),
        ("抖音", "短视频热搜榜"),
        ("微博", "社交实时热搜"),
        ("虎扑", "论坛热议榜"),
        ("ai hot", "科技热点榜"),
        ("联合早报", "亚太资讯源"),
        ("香港01", "港闻资讯源"),
        ("今日头条", "综合资讯热榜"),
        ("头条", "综合资讯热榜"),
        ("百度", "搜索实时热点"),
        ("b站", "视频社区热榜"),
        ("bilibili", "视频社区热榜"),
    ):
        if kw in low or kw in s:
            return f"{target}{cache_suffix}"

    return f"{s}{cache_suffix}"


def generalize_broker_source(raw: str | None, default: str = "机构研报摘要") -> str:
    """券商 / 外资投行栏目的「来源」列泛化：
    - 检索渠道（Google/Bing/DeepSeek 等）→ 公开新闻检索 / 备援新闻检索 / AI 智能检索
    - 研报出处或机构名重复充当出处 → 机构研报摘要
    """
    if not raw or not str(raw).strip():
        return default
    s = str(raw).strip()
    mapped = generalize_source(s, default="")
    if mapped and mapped != s:
        return mapped
    if s in CANONICAL_GENERALIZED_NAMES:
        return s
    return default


# ================================================================ 3. 快讯导语前缀与报错脱敏

# 常见快讯电头：「财联社10月22日电，」「【标题】财联社讯，」「格隆汇10月22日电｜」等
_WIRE_PREFIX_RE = re.compile(
    r"^(?:【([^】]{2,40})】\s*)?"
    r"(?:财联社|格隆汇|华尔街见闻|法布财经|金十数据|金十快讯|新浪财经|同花顺)"
    r"(?:\s*\d{1,2}月\d{1,2}日|\s*\d{1,2}日)?"
    r"(?:电报|快讯|电|讯|消息|整理)?"
    r"[，,｜|：:\s]+"
)

_WIRE_TAG_RE = re.compile(r"【(?:金十数据|财联社|格隆汇|华尔街见闻|法布财经)([^】]*)】")


def clean_wire_prefix(title: str) -> str:
    """剥离快讯标题开头的电报社/平台署名导语，保留核心新闻事实与公司名。"""
    if not title:
        return ""
    t = title.strip()
    t = _WIRE_TAG_RE.sub(lambda m: f"【{m.group(1).strip('·-—:： ')}】" if m.group(1).strip("·-—:： ") else "", t).strip()
    m = _WIRE_PREFIX_RE.match(t)
    if m:
        head_tag = f"【{m.group(1)}】" if m.group(1) else ""
        rest = t[m.end():].strip()
        if rest:
            t = f"{head_tag}{rest}"
    return t


_URL_RE = re.compile(r"https?://[^\s)）:：\"'<>]+")
_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|tv|hk|sg|us|co)\b", re.I
)

_ERROR_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("DEEPSEEK_API_KEY", "检索密钥"),
    ("OPENAI_API_KEY", "模型密钥"),
    ("HUGGINGFACEHUB_API_TOKEN", "模型令牌"),
    ("HUGGINGFACE_API_KEY", "模型令牌"),
    ("HF_TOKEN", "模型令牌"),
    ("DeepSeek AI 搜索", "AI 智能检索"),
    ("DeepSeek 搜索", "AI 智能检索"),
    ("DeepSeek API", "AI 推理接口"),
    ("DeepSeek", "AI 智能检索"),
    ("Google 新闻", "公开新闻检索"),
    ("Google新闻", "公开新闻检索"),
    ("Google", "公开新闻检索"),
    ("Bing资讯", "备援新闻检索"),
    ("Bing新闻", "备援新闻检索"),
    ("Bing", "备援新闻检索"),
    ("YouTube·investtalk", "海外财经频道"),
    ("YouTube@investtalk", "海外财经频道"),
    ("YouTube", "海外财经频道"),
    ("investtalk", "海外财经频道"),
    ("MKTNews 快讯", "海外快讯源"),
    ("MKTNews", "海外快讯源"),
    ("华尔街见闻 快讯", "宏观快讯源"),
    ("华尔街见闻 最新", "宏观资讯流"),
    ("华尔街见闻 最热", "宏观热读榜"),
    ("华尔街见闻", "宏观快讯源"),
    ("财联社 电报", "财经电报源"),
    ("财联社 深度", "财经深度源"),
    ("财联社 热门", "财经热文榜"),
    ("财联社", "财经电报源"),
    ("雪球 热门股票", "投资社区热榜"),
    ("雪球反爬升级", "访问受限"),
    ("雪球", "投资社区热榜"),
    ("格隆汇 事件", "港美股快讯源"),
    ("格隆汇", "港美股快讯源"),
    ("法布财经 快讯", "外汇商品快讯"),
    ("法布财经 头条", "市场头条源"),
    ("法布财经", "外汇商品快讯"),
    ("FastBull", "外汇商品快讯"),
    ("金十数据", "实时快讯主源"),
    ("金十", "实时快讯主源"),
    ("知乎热榜", "问答社区热榜"),
    ("知乎", "问答社区热榜"),
    ("抖音热搜", "短视频热搜榜"),
    ("抖音", "短视频热搜榜"),
    ("微博实时热搜", "社交实时热搜"),
    ("微博", "社交实时热搜"),
    ("虎扑热搜", "论坛热议榜"),
    ("虎扑", "论坛热议榜"),
    ("AI hot", "科技热点榜"),
    ("联合早报", "亚太资讯源"),
    ("香港01", "港闻资讯源"),
    ("今日头条热榜", "综合资讯热榜"),
    ("今日头条", "综合资讯热榜"),
    ("百度实时热点", "搜索实时热点"),
    ("百度", "搜索实时热点"),
    ("B站热榜", "视频社区热榜"),
    ("B站", "视频社区热榜"),
    ("腾讯财经(美股)", "美股行情主源"),
    ("东方财富(美股)", "美股行情备源"),
    ("腾讯财经", "实时行情主源"),
    ("东方财富", "实时行情备源"),
    ("东财", "实时行情备源"),
    ("Yahoo Finance", "国际行情源"),
    ("Yahoo财经", "国际行情源"),
    ("Yahoo", "国际行情源"),
    ("Stooq(延迟)", "延迟核验源"),
    ("Stooq", "延迟核验源"),
)


def sanitize_error(err: object) -> str:
    """清洗报错或数据缺口文本：去 URL、去域名、将具体平台名替换为泛化名。"""
    s = str(err or "").strip()
    if not s:
        return "请求失败"
    s = _URL_RE.sub("", s)
    s = _DOMAIN_RE.sub("", s)
    for raw, repl in _ERROR_REPLACEMENTS:
        if raw in s:
            s = s.replace(raw, repl)
    s = re.sub(r"\(\s*\)|（\s*）", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" :：-")
    return s or "请求失败"


# ================================================================ 4. 品牌外露审计（供自检与单测断言）

FORBIDDEN_SOURCE_BRANDS: tuple[str, ...] = (
    "MKTNews",
    "华尔街见闻",
    "财联社",
    "雪球",
    "格隆汇",
    "法布财经",
    "FastBull",
    "金十",
    "DeepSeek",
    "deepseek",
    "Google新闻",
    "Google 新闻",
    "Google·",
    "Bing资讯",
    "Bing新闻",
    "Bing RSS",
    "YouTube",
    "investtalk",
    "知乎",
    "抖音",
    "微博",
    "虎扑",
    "AI hot",
    "联合早报",
    "香港01",
    "今日头条",
    "百度实时热点",
    "百度热搜",
    "B站",
    "哔哩",
    "NewsNow",
    "newsnow",
    "ourongxing",
    "腾讯财经",
    "腾讯美股",
    "东方财富",
    "东财",
    "Yahoo",
    "YAHOO",
    "EASTMONEY",
    "Stooq",
    "新浪7x24",
    "新浪财经",
    "同花顺",
    "Hugging Face",
    "FinBERT",
    "Anthropic",
)


def find_leaked_brands(text: str) -> list[str]:
    """检测渲染结果中是否残留任何未泛化的具体数据源品牌词。返回命中列表（空=零泄漏）。"""
    if not text:
        return []
    return [b for b in FORBIDDEN_SOURCE_BRANDS if b in text]


# 兼容别名（供测试与外部调用统一引用）
SOURCE_NAME_MAP = ALL_SOURCE_MAP
find_brand_leaks = find_leaked_brands


def generalize_markdown_sources(md: str) -> str:
    """将 Markdown 表格单元格中的原始数据源名替换为泛化名，同时保留上市公司/标的名称。"""
    if not md:
        return ""
    out_lines: list[str] = []
    for line in md.splitlines():
        if "|" in line and not re.match(r"^\s*\|[\s:\-|]+\|\s*$", line):
            parts = line.split("|")
            for idx in range(1, len(parts) - 1):
                cell = parts[idx].strip()
                if cell in ALL_SOURCE_MAP or cell.startswith(("Google·", "Google ·")):
                    pad_l = parts[idx][:len(parts[idx]) - len(parts[idx].lstrip())]
                    pad_r = parts[idx][len(parts[idx].rstrip()):]
                    parts[idx] = f"{pad_l}{generalize_source(cell)}{pad_r}"
            line = "|".join(parts)
        out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if md.endswith("\n") else "")


# ================================================================ 5. 自检与 CLI

def selftest() -> int:
    fails = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal fails
        suffix = f" ← {detail}" if (not cond and detail) else ""
        print(f"  {'✅' if cond else '❌'} {name}{suffix}")
        if not cond:
            fails += 1

    print("🏷️  来源泛化名映射层（离线自检）")
    check("全市场快讯 13 源泛化名互不重复",
          len(set(FEED_SOURCE_MAP.values())) == len(FEED_SOURCE_MAP) == 13)
    check("全网热榜 10 源泛化名互不重复",
          len(set(HOTLIST_KEY_MAP.values())) == len(HOTLIST_KEY_MAP) == 10)
    scan_18 = {
        generalize_source("Google新闻"),
        generalize_source("财联社 电报"),
        generalize_source("华尔街见闻 快讯"),
        generalize_source("格隆汇 事件"),
        generalize_source("金十数据"),
        generalize_source("MKTNews 快讯"),
        generalize_source("DeepSeek 搜索"),
        generalize_source("雪球 热门股票"),
        *HOTLIST_KEY_MAP.values(),
    }
    check("十八平台扫描 18 源泛化名互不重复", len(scan_18) == 18, str(len(scan_18)))
    check("全部泛化名自身零品牌残留",
          all(not find_leaked_brands(v) for v in ALL_SOURCE_MAP.values()))
    check("Google·子来源泛化为公开新闻检索",
          generalize_source("Google·香港经济日报") == "公开新闻检索")
    check("核验表保留标的名且泛化数据源",
          generalize_source("腾讯财经（腾讯控股）") == "实时行情主源（腾讯控股）")
    check("文件缓存后缀保留",
          generalize_source("东方财富 (文件缓存)") == "实时行情备源 (文件缓存)")
    check("电报导语前缀剥离且保留公司名",
          clean_wire_prefix("财联社10月22日电，腾讯控股今日回购股份")
          == "腾讯控股今日回购股份")
    check("带书名号导语前缀剥离",
          clean_wire_prefix("【央行操作】格隆汇10月22日电｜逆回购净投放")
          == "【央行操作】逆回购净投放")
    err_clean = sanitize_error(
        "网络请求失败 https://www.cls.cn/v1/roll: 财联社 HTTP 502 (DEEPSEEK_API_KEY)")
    check("报错信息去 URL/域名/品牌名/密钥名",
          not find_leaked_brands(err_clean) and "cls.cn" not in err_clean
          and "DEEPSEEK_API_KEY" not in err_clean, err_clean)

    print(f"\n{'✅ 来源泛化名自检通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="全仓库统一来源泛化名映射与脱敏")
    p.add_argument("--selftest", action="store_true", help="运行离线自检")
    p.add_argument("--list", action="store_true", help="输出泛化映射 JSON")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()
    if args.list:
        print(json.dumps({
            "feed_sources": FEED_SOURCE_MAP,
            "hotlist_sources": HOTLIST_KEY_MAP,
            "quote_sources": QUOTE_SOURCE_MAP,
        }, ensure_ascii=False, indent=2))
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
