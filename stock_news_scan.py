#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_news_scan.py — 量价舆情动量 · 十七平台股票扫描

输入股票代码（含名称/别名），在最近 156 小时（默认，可配）窗口内检索
十七个平台，找出与该股票相关的新闻，以及它所属/被提及的有关板块。

十七个平台 = 财经快讯 7 源 + 社媒热榜 10 源（2026-08-13 由 7 源扩充）：
 - 财经 7：Google新闻(RSS检索) / 财联社电报 / 华尔街见闻快讯 /
   格隆汇事件 / 金十数据 / MKTNews快讯 / 雪球热门股票
 - 社媒 10：知乎热榜 / 微博实时热搜 / 抖音热搜 / 虎扑热搜 /
   AI hot / 联合早报 / 香港01 / 今日头条热榜 / 百度实时热点 / B站热榜
   （复用 newsnow_sources.py，逐源健康度见 source_check_db.py 校验库）

窗口规则：
 - 带可靠时间戳的条目：严格按 156h（默认）过滤；
 - 热榜类条目（雪球/社媒）本身是实时快照：不参与时间过滤，标记「实时」。

板块识别：
 - 预设板块：内置个股档案 STOCK_PROFILES（代码→名称/别名/板块），
   覆盖港股/A股与热门美股（NVDA/AAPL/TSLA/MSFT/GOOGL/AMZN/META/BABA，
   美股行情见 us_quote.py 四源交叉验证）；
 - 自定义板块：--sectors 追加关键词；
 - 动态板块：从命中标题中按位提取「XX板块」，长命中被同频短命中吸收。

仅依赖标准库；命中按「直接相关（代码/名称/别名）」与「板块相关」分组，
逐条本地情绪打标，任何单源失败只进数据缺口、不中断整体扫描。
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UTC = timezone.utc
DEFAULT_WINDOW_HOURS = 156          # 默认窗口：最近 156 小时
SOCIAL_REALTIME = "实时"            # 热榜类条目的时效标记


class ScanError(RuntimeError):
    """十七平台扫描专用异常。"""


# ---------------- 通用请求 ----------------

def http_request(url: str, headers: dict | None = None, timeout: int = 15,
                 encoding: str = "utf-8", retries: int = 2) -> tuple[int, str]:
    """带指数退避重试，避免瞬时 502/503 导致数据丢失。"""
    import time
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "Mozilla/5.0 (stock-scan/1.0; 14-platforms)")
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, resp.read().decode(encoding, "replace")
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise ScanError(f"HTTP {e.code}") from e
        except ScanError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise ScanError(f"网络失败 {e}") from e
    raise ScanError(f"网络失败 {last_exc}") from last_exc


# ---------------- 轻量情绪词典（与主脚本口径一致，独立实现避免交叉依赖） ----------------

POS_WORDS = ["利好", "上涨", "大涨", "增长", "中标", "突破", "创新高", "创纪录",
             "回购", "预增", "超预期", "盈利", "签约", "订单", "扩产", "获批",
             "分红", "涨停", "回升", "反弹", "走强", "拉升"]
NEG_WORDS = ["利空", "下跌", "大跌", "亏损", "减持", "违约", "下调", "预警",
             "预亏", "低于预期", "处罚", "诉讼", "削减", "延期", "停产",
             "跌停", "跳水", "承压"]


def score_text(text: str) -> tuple[float, str]:
    """词典法情绪打分。返回 (score∈[-1,1], 标签)。"""
    pos = sum(text.count(w) for w in POS_WORDS)
    neg = sum(text.count(w) for w in NEG_WORDS)
    total = pos + neg
    score = 0.0 if total == 0 else max(-1.0, min(1.0, (pos - neg) / total))
    label = "利好" if score > 0.12 else ("利空" if score < -0.12 else "中性")
    return round(score, 2), label


# ---------------- 时间解析与窗口 ----------------

CN_TZ = timezone(timedelta(hours=8), "CST")


def parse_any_time(v) -> datetime | None:
    """兼容 epoch(s/ms)、ISO、RFC822、'YYYY-MM-DD HH:MM[:SS]'。无法解析返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:          # 毫秒
            ts /= 1000.0
        if ts < 1e9:           # 明显不是 epoch
            return None
        return datetime.fromtimestamp(ts, UTC)
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10}(\d{3})?", s):
        return parse_any_time(float(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # 无时区的快讯时间戳均为站内本地时间（中文源 = 东八区）
        return dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)
    except (TypeError, ValueError):
        pass
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*"
                  r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        return datetime(int(y), int(mo), int(d), int(hh), int(mm),
                        int(ss or 0), tzinfo=CN_TZ)
    return None


def _aware(ts: datetime) -> datetime:
    """无时区时间一律按东八区解释（中文财经源惯例）。"""
    return ts if ts.tzinfo else ts.replace(tzinfo=CN_TZ)


def in_window(ts: datetime | None, hours: int, now: datetime) -> bool:
    return ts is not None and (now - _aware(ts)).total_seconds() <= hours * 3600


# ---------------- 个股档案：代码 → 名称/别名/板块 ----------------

STOCK_PROFILES: dict[str, dict] = {
    "00700": {
        "name": "腾讯控股",
        "aliases": ["腾讯", "Tencent"],
        "sectors": ["互联网", "游戏", "AI", "云计算", "社交"],
    },
    "09988": {
        "name": "阿里巴巴",
        "aliases": ["阿里", "Alibaba"],
        "sectors": ["电商", "云计算", "互联网", "AI"],
    },
    "600519": {
        "name": "贵州茅台",
        "aliases": ["茅台", "Moutai"],
        "sectors": ["白酒", "食品饮料", "消费"],
    },
    "300750": {
        "name": "宁德时代",
        "aliases": ["宁王", "CATL"],
        "sectors": ["锂电池", "储能", "新能源车", "动力电池"],
    },
    # ---- 境外（美股）档案：行情经 us_quote 四源交叉验证 ----
    "NVDA": {
        "name": "英伟达",
        "aliases": ["NVIDIA", "黄仁勋", "Nvidia"],
        "sectors": ["芯片", "算力", "AI", "半导体"],
    },
    "AAPL": {
        "name": "苹果",
        "aliases": ["Apple", "iPhone", "蒂姆库克"],
        "sectors": ["消费电子", "科技", "硬件"],
    },
    "TSLA": {
        "name": "特斯拉",
        "aliases": ["Tesla", "马斯克", "Musk"],
        "sectors": ["新能源车", "自动驾驶", "AI", "电池"],
    },
    "MSFT": {
        "name": "微软",
        "aliases": ["Microsoft", "纳德拉"],
        "sectors": ["云计算", "AI", "软件"],
    },
    "GOOGL": {
        "name": "谷歌",
        "aliases": ["Google", "Alphabet", "皮查伊"],
        "sectors": ["互联网", "AI", "广告", "云计算"],
    },
    "AMZN": {
        "name": "亚马逊",
        "aliases": ["Amazon", "AWS", "贝索斯"],
        "sectors": ["电商", "云计算", "物流"],
    },
    "META": {
        "name": "Meta",
        "aliases": ["Meta", "Facebook", "扎克伯格"],
        "sectors": ["社交", "AI", "广告", "元宇宙"],
    },
    "BABA": {
        "name": "阿里巴巴(美股)",
        "aliases": ["阿里", "Alibaba"],
        "sectors": ["电商", "云计算", "互联网", "AI"],
    },
}

# 按位前瞻提取「XX板块」：'港股电商板块' 同时产出 '港股电商板块' 与 '电商板块'
_SECTOR_RE = re.compile(r"(?=([一-龥A-Za-z]{2,8}板块))")


@dataclass
class StockQuery:
    """一次扫描的检索条件：代码变体 + 名称/别名 + 板块关键词。"""
    code: str = ""
    name: str = ""
    direct_words: list[str] = field(default_factory=list)   # 代码变体 + 名称 + 别名
    sectors: list[str] = field(default_factory=list)        # 预设 + 自定义板块


def normalize_code(raw: str) -> str:
    """纯数字港股代码补零到 5 位；其他原样返回。"""
    s = (raw or "").strip().upper().replace(".HK", "")
    return s.zfill(5) if s.isdigit() and len(s) <= 5 else s


def build_stock_query(code: str = "", name: str = "",
                      extra_sectors: list[str] | None = None) -> StockQuery:
    """组装检索词：代码变体 + 名称/别名（查档案补全）+ 板块关键词。"""
    code = normalize_code(code)
    prof = STOCK_PROFILES.get(code, {})
    name = name.strip() or prof.get("name", "")
    direct: list[str] = []
    if code:
        stripped = code.lstrip("0") or code
        direct.append(code)
        if code.isdigit():                      # ".HK" 变体仅适用港股数字代码
            if stripped != code:
                direct.append(f"{stripped}.HK")
            direct.append(f"{code}.HK")
    if name:
        direct.append(name)
    for a in prof.get("aliases", []):
        if a not in direct:
            direct.append(a)
    sectors = list(dict.fromkeys(
        prof.get("sectors", []) + [s.strip() for s in (extra_sectors or []) if s.strip()]))
    return StockQuery(code=code, name=name, direct_words=direct, sectors=sectors)


# ---------------- 数据结构 ----------------

@dataclass
class ScanItem:
    platform: str
    title: str
    url: str = ""
    via: str = "直接"                 # 直接（代码/名称/别名）| 板块
    matched: list[str] = field(default_factory=list)
    age_h: float | None = None        # None = 实时/时间未知
    score: float = 0.0
    label: str = "中性"


@dataclass
class ScanPack:
    hours: int
    code: str = ""
    name: str = ""
    direct: dict[str, list[ScanItem]] = field(default_factory=dict)
    sector_hits: dict[str, list[ScanItem]] = field(default_factory=dict)
    sectors: list[str] = field(default_factory=list)
    dyn_sectors: list[tuple[str, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    platforms_total: int = 18  # 财经 7+DeepSeek 1 + 社媒 10 = 18
    agg: dict = field(default_factory=dict)


def _mk_scan_item(platform: str, title: str, url: str, ts: datetime | None,
                  now: datetime, via: str, matched: list[str]) -> ScanItem:
    title = re.sub(r"\s+", " ", title).strip()
    score, label = score_text(title)
    age = round((now - _aware(ts)).total_seconds() / 3600, 1) if ts else None
    return ScanItem(platform=platform, title=title, url=url, via=via,
                    matched=matched, age_h=age, score=score, label=label)


def classify_items(platform: str, raw: list[dict], q: StockQuery,
                   hours: int, now: datetime,
                   time_filtered: bool = True) -> tuple[list[ScanItem], list[ScanItem]]:
    """把某平台的原始条目分为「直接相关」与「板块相关」。

    raw: [{"title","url","ts"}]；time_filtered=False 用于热榜实时快照。
    """
    direct_out: list[ScanItem] = []
    sector_out: list[ScanItem] = []
    seen: set[str] = set()
    for r in raw:
        title = (r.get("title") or "").strip()
        if len(title) < 4 or title in seen:
            continue
        ts = r.get("ts")
        if time_filtered and ts is not None and not in_window(ts, hours, now):
            continue
        low = title.lower()
        hits = [w for w in q.direct_words if w and w.lower() in low]
        if hits:
            direct_out.append(_mk_scan_item(platform, title, r.get("url", ""),
                                            ts, now, "直接", hits))
            seen.add(title)
            continue
        sec_hits = [s for s in q.sectors if s and s in title]
        if sec_hits:
            sector_out.append(_mk_scan_item(platform, title, r.get("url", ""),
                                            ts, now, "板块", sec_hits))
            seen.add(title)
    return direct_out, sector_out


def extract_dyn_sectors(direct: dict[str, list[ScanItem]],
                        sector_hits: dict[str, list[ScanItem]],
                        ) -> list[tuple[str, int]]:
    """从全部命中标题提取「XX板块」并计数。

    子串对（Y 是 X 的尾部子串，如 '能源板块'⊂'新能源板块'）按频次归属：
    - 同频：Y 只伴随 X 出现 → Y 是伪影，保留较长的规范名（新能源板块）；
    - Y 更高频：X 只存在于 Y 的语境里 → 保留短名（港股电商板块→电商板块）。
    """
    counter: dict[str, int] = {}
    for group in (direct, sector_hits):
        for items in group.values():
            for i in items:
                for m in _SECTOR_RE.findall(i.title):
                    counter[m] = counter.get(m, 0) + 1
    drop: set[str] = set()
    for x in counter:
        for y in counter:
            if len(y) < len(x) and y in x and x not in drop and y not in drop:
                if counter[x] == counter[y]:
                    drop.add(y)      # Y 总是伴随 X 出现 → Y 为子串伪影
                elif counter[y] > counter[x]:
                    drop.add(x)      # Y 独立出现更多 → X 并入 Y
    for k in drop:
        counter.pop(k, None)
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


# ---------------- 财经快讯 7 源：通用 JSON 提取 ----------------

TITLE_KEYS = ["title", "content", "digest", "summary", "brief", "name",
              "description", "content_text", "subtitle"]
TIME_KEYS = ["ctime", "display_time", "time", "pub_date", "published_at",
             "ltime", "showtime", "created_at", "date", "add_time",
             "update_time", "publish_time"]
LIST_KEYS = ["data", "list", "items", "result", "news", "lives", "flash_list"]


def _json_find_list(payload):
    """在常见键下定位新闻列表（支持双层 data 嵌套）。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in LIST_KEYS:
            v = payload.get(k)
            if isinstance(v, list) and v:
                return v
            if isinstance(v, dict):
                for sk in LIST_KEYS:
                    sv = v.get(sk)
                    if isinstance(sv, list) and sv:
                        return sv
    return None


def extract_flash_rows(payload, limit: int = 30) -> list[dict]:
    """通用快讯提取：[{title,url,ts}]，不做窗口过滤（由 classify 统一处理）。"""
    rows = _json_find_list(payload) or []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        scopes = [row] + ([row["data"]] if isinstance(row.get("data"), dict) else [])
        title = ""
        for sc in scopes:
            for k in TITLE_KEYS:
                v = sc.get(k)
                if isinstance(v, str) and len(v.strip()) >= 4:
                    title = re.sub(r"<[^>]+>", "", v).strip()
                    break
            if title:
                break
        if not title:
            continue
        ts = None
        for sc in scopes:
            for k in TIME_KEYS:
                if k in sc:
                    ts = parse_any_time(sc[k])
                    if ts:
                        break
            if ts:
                break
        url = ""
        for k in ("url", "share_url", "uri", "link"):
            if isinstance(row.get(k), str) and row[k].startswith("http"):
                url = row[k]
                break
        out.append({"title": title, "url": url, "ts": ts})
        if len(out) >= limit:
            break
    return out


# ---- 源1：Google 新闻（官方 RSS，按标的直接检索）----

GOOGLE_RSS = ("https://news.google.com/rss/search?"
              "q={q}&hl=zh-HK&gl=HK&ceid=HK:zh-Hant")


def parse_google_rss(text: str) -> list[dict]:
    out: list[dict] = []
    for it in ET.fromstring(text).iter("item"):
        title = it.findtext("title", "") or ""
        if " - " in title:                       # Google 格式: "标题 - 来源"
            title = title.rsplit(" - ", 1)[0]
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate", "") or "")
        except (TypeError, ValueError):
            ts = None
        out.append({"title": title, "url": it.findtext("link", "") or "",
                    "ts": ts})
    return out


def fetch_google(query: str, timeout: int) -> list[dict]:
    # when:7d 覆盖 156h（≈6.5 天）；本地再严过滤
    q = urllib.parse.quote_plus(f"{query} when:7d")
    status, body = http_request(GOOGLE_RSS.format(q=q), timeout=timeout)
    if status != 200:
        raise ScanError(f"Google HTTP {status}")
    rows = parse_google_rss(body)
    if not rows:
        raise ScanError("RSS 无条目")
    return rows


# ---- 源2：财联社 电报 ----

CLS_URL = ("https://www.cls.cn/nodeapi/telegraphList"
           "?app=CailianpressWeb&os=web&sv=8.4.6&rn=30")


def fetch_cls(timeout: int) -> list[dict]:
    status, body = http_request(CLS_URL, timeout=timeout,
                                headers={"Referer": "https://www.cls.cn/telegraph"})
    if status != 200:
        raise ScanError(f"财联社 HTTP {status}")
    rows = extract_flash_rows(json.loads(body))
    if not rows:
        raise ScanError("财联社解析空")
    return rows


# ---- 源3：华尔街见闻 快讯 ----

WSCN_URL = ("https://api-ddc-wscn.awtmt.com/market/lives"
            "?channel=global-channel&limit=30")


def fetch_wscn(timeout: int) -> list[dict]:
    status, body = http_request(
        WSCN_URL, timeout=timeout,
        headers={"Referer": "https://wallstreetcn.com/", "Accept": "application/json"})
    if status != 200:
        raise ScanError(f"华尔街见闻 HTTP {status}")
    rows = extract_flash_rows(json.loads(body))
    if not rows:
        raise ScanError("华尔街见闻解析空")
    return rows


# ---- 源4：格隆汇 事件 ----

GLH_URL = "https://www.gelonghui.com/api/fastnews/v2/getFastNewsList?limit=30"


def fetch_gelonghui(timeout: int) -> list[dict]:
    status, body = http_request(GLH_URL, timeout=timeout,
                                headers={"Referer": "https://www.gelonghui.com/live"})
    if status != 200:
        raise ScanError(f"格隆汇 HTTP {status}")
    rows = extract_flash_rows(json.loads(body))
    if not rows:
        raise ScanError("格隆汇解析空")
    return rows


# ---- 源5：金十数据（JS 壳包裹 JSON）----

JIN10_URL = "https://www.jin10.com/flash_newest.js"


def strip_js_shell(body: str) -> str:
    """剥掉 'var flash_newest = ...;' 外壳，取出内层 JSON。"""
    start = body.find("{")
    alt = body.find("[")
    if start < 0 or (0 <= alt < start):
        start = alt
    end_obj = body.rfind("}")
    end_arr = body.rfind("]")
    end = max(end_obj, end_arr)
    if start < 0 or end <= start:
        raise ScanError("金十 JS 壳结构异常")
    return body[start:end + 1]


def fetch_jin10(timeout: int) -> list[dict]:
    status, body = http_request(
        JIN10_URL, timeout=timeout,
        headers={"Referer": "https://www.jin10.com/", "x-app-id": "rU6QIu7JHe2gOUTe"})
    if status != 200:
        raise ScanError(f"金十 HTTP {status}")
    rows = extract_flash_rows(json.loads(strip_js_shell(body)))
    if not rows:
        raise ScanError("金十解析空")
    return rows


# ---- 源6：MKTNews 快讯 ----

MKTNEWS_URL = "https://api.mktnews.net/api/flash?limit=30"


def fetch_mktnews(timeout: int) -> list[dict]:
    status, body = http_request(MKTNEWS_URL, timeout=timeout)
    if status != 200:
        raise ScanError(f"MKTNews HTTP {status}")
    rows = extract_flash_rows(json.loads(body))
    if not rows:
        raise ScanError("MKTNews 解析空")
    return rows


# ---- 源7：雪球 热门股票（代码/名称直接命中，涨跌幅映射情绪）----

XUEQIU_HOT_URL = ("https://stock.xueqiu.com/v5/stock/hot_stock/list.json"
                  "?size=20&_type=10&type=10")


def parse_xueqiu_hot(text: str, q: StockQuery, now: datetime) -> list[ScanItem]:
    """雪球热度榜：只保留命中代码/名称/别名的行，涨跌幅直接映射情绪分。"""
    rows = _json_find_list(json.loads(text)) or []
    out: list[ScanItem] = []
    for row in rows:
        name, code = str(row.get("name", "")), str(row.get("code", ""))
        if not name:
            continue
        low = f"{name} {code}".lower()
        hits = [w for w in q.direct_words if w and w.lower() in low]
        if not hits:
            continue
        pct = row.get("percent")
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
        score = max(-1.0, min(1.0, (pct or 0) / 5))
        label = "利好" if score > 0.12 else ("利空" if score < -0.12 else "中性")
        inc = row.get("increment") or row.get("follow_increment")
        title = f"{name} {code}" + (f" 涨跌{pct:+.2f}%" if pct is not None else "")
        if inc:
            title += f"，关注增量 {inc}"
        out.append(ScanItem(platform="雪球 热门股票", title=title, via="直接",
                            matched=hits, age_h=None,  # 实时热榜
                            score=round(score, 2), label=label))
    return out


def fetch_xueqiu(q: StockQuery, timeout: int, now: datetime) -> list[ScanItem]:
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (stock-scan/1.0)")]
    try:
        opener.open("https://xueqiu.com/hq", timeout=timeout).read()  # Cookie 预热
        body = opener.open(XUEQIU_HOT_URL, timeout=timeout).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ScanError(f"雪球 HTTP {e.code}") from e
    except Exception as e:
        raise ScanError(f"雪球网络失败 {e}") from e
    items = parse_xueqiu_hot(body, q, now)
    if not items:
        raise ScanError("热度榜未命中该标的（或接口结构已变）")
    return items


# ---------------- DeepSeek 搜索（AI 搜索引擎，新增） ----------------

def fetch_deepseek_search_api(query: str, timeout: int, hours: int = DEFAULT_WINDOW_HOURS) -> list[dict]:
    """DeepSeek API 作为搜索引擎，返回 [{title,url,ts}]，供财经快讯链路复用"""
    try:
        import deepseek_search
    except ImportError as e:
        raise ScanError(f"deepseek_search 模块不可用: {e}") from e
    # deepseek_search 自带 hours 窗口
    return deepseek_search.fetch_deepseek_search(query, timeout=timeout, hours=hours)


def fetch_deepseek(query: str, timeout: int) -> list[dict]:
    # 兼容旧签名，默认 48h，与 sentiment 因子一致
    return fetch_deepseek_search_api(query, timeout, hours=48)


# ---------------- 扫描主流程 ----------------

FINANCE_FETCHERS = {                     # 财经 7 源中的 6 个 JSON/RSS 源 + DeepSeek AI 搜索
    "Google新闻": fetch_google,          # 签名 (query, timeout)，其余为 (timeout)；DeepSeek 同为 query
    "财联社 电报": fetch_cls,
    "华尔街见闻 快讯": fetch_wscn,
    "格隆汇 事件": fetch_gelonghui,
    "金十数据": fetch_jin10,
    "MKTNews 快讯": fetch_mktnews,
    "DeepSeek 搜索": fetch_deepseek,     # 新增：DeepSeek API 作为智能搜索引擎
}


def _needs(query: StockQuery) -> str:
    return query.name or query.code


def scan_stock(code: str = "", name: str = "", hours: int = DEFAULT_WINDOW_HOURS,
               timeout: int = 12, extra_sectors: list[str] | None = None,
               now: datetime | None = None) -> ScanPack:
    """输入股票代码/名称，扫描十七平台最近 hours 小时内的相关新闻与有关板块。

    任何单源失败只记录数据缺口、不中断；返回值可直接交给
    render_scan_md / scan_context 渲染。
    """
    now = now or datetime.now(UTC)
    q = build_stock_query(code, name, extra_sectors)
    pack = ScanPack(hours=hours, code=q.code, name=q.name, sectors=q.sectors)

    # ---- 财经 7+1 源（Google/DeepSeek 需要 query，其余仅 timeout；DeepSeek 带 hours）----
    for platform, fn in FINANCE_FETCHERS.items():
        try:
            if platform in ("Google新闻", "DeepSeek 搜索"):
                if platform == "DeepSeek 搜索":
                    raw = fetch_deepseek_search_api(_needs(q), timeout, hours=hours)
                else:
                    raw = fn(_needs(q), timeout)
            else:
                raw = fn(timeout)
            d_items, s_items = classify_items(platform, raw, q, hours, now)
            if platform in ("Google新闻", "DeepSeek 搜索"):
                # 检索本身即相关：窗口内未命中代码字面的条目仍计直接相关
                known = {i.title for i in d_items} | {i.title for i in s_items}
                for r in raw:
                    ts = r.get("ts")
                    if ts is None or not in_window(ts, hours, now):
                        continue
                    t = re.sub(r"\s+", " ", r["title"]).strip()
                    if t and t not in known:
                        d_items.append(_mk_scan_item(platform, t, r.get("url", ""),
                                                     ts, now, "直接", [_needs(q)]))
                        known.add(t)
            if d_items:
                pack.direct[platform] = d_items[:10]
            if s_items:
                pack.sector_hits[platform] = s_items[:10]
        except Exception as e:                       # noqa: BLE001 —— 逐源隔离
            pack.errors.append(f"{platform}：{str(e)[:120]}")

    # ---- 雪球（专用结构，只保留命中行）----
    if q.direct_words:
        try:
            items = fetch_xueqiu(q, timeout, now)
            pack.direct["雪球 热门股票"] = items[:10]
        except Exception as e:                       # noqa: BLE001
            pack.errors.append(f"雪球 热门股票：{str(e)[:120]}")

    # ---- 社媒热榜 10 源（复用 newsnow_sources，实时快照不过滤时间）----
    try:
        from newsnow_sources import FETCHERS as SOCIAL_FETCHERS, TARGET_SOURCES
        for key in TARGET_SOURCES:
            src_name, fn = SOCIAL_FETCHERS[key]
            try:
                hot = fn(timeout=timeout)
                raw = [{"title": f"{h.title} {h.extra_info}".strip(),
                        "url": h.url, "ts": None} for h in hot]
                d_items, s_items = classify_items(src_name, raw, q, hours,
                                                  now, time_filtered=False)
                if d_items:
                    pack.direct[src_name] = d_items[:10]
                if s_items:
                    pack.sector_hits[src_name] = s_items[:10]
            except Exception as e:                   # noqa: BLE001
                pack.errors.append(f"{src_name}：{str(e)[:120]}")
    except ImportError as e:
        pack.errors.append(f"社媒10源不可用（缺 newsnow_sources.py）：{e}")

    # ---- 动态板块 + 聚合 ----
    pack.dyn_sectors = extract_dyn_sectors(pack.direct, pack.sector_hits)
    direct_items = [i for v in pack.direct.values() for i in v]
    sector_items = [i for v in pack.sector_hits.values() for i in v]
    all_items = direct_items + sector_items
    pos = sum(1 for i in all_items if i.label == "利好")
    neg = sum(1 for i in all_items if i.label == "利空")
    mean = round(sum(i.score for i in all_items) / len(all_items), 2) \
        if all_items else 0.0
    pack.agg = {
        "n_direct": len(direct_items), "n_sector": len(sector_items),
        "n": len(all_items), "pos": pos, "neg": neg,
        "neu": len(all_items) - pos - neg, "mean": mean,
        "platforms_hit": len(set(pack.direct) | set(pack.sector_hits)),
        "platforms_total": pack.platforms_total,
    }
    return pack


# ---------------- 渲染 ----------------

def _fmt_age(age: float | None) -> str:
    return f"{age:.0f}h前" if age is not None else SOCIAL_REALTIME


def _related_sector_lines(pack: ScanPack) -> list[str]:
    """预设板块（含动态计数）+ 新闻动态提及板块的展示行。"""
    dyn = dict(pack.dyn_sectors)
    related: list[str] = []
    for s in pack.sectors:
        c = sum(cnt for k, cnt in pack.dyn_sectors
                if k == s + "板块" or s in k or k in s)
        related.append(f"{s}（预设{f'＋{c}条提及' if c else ''}）")
    for s, c in pack.dyn_sectors[:8]:
        if any(s == ps + "板块" or ps in s for ps in pack.sectors):
            continue            # 已并入预设展示
        related.append(f"{s}（新闻提及 {c} 条）")
        dyn.pop(s, None)
    return related


def render_scan_md(pack: ScanPack) -> str:
    """多平台扫描的 Markdown 附录（直接相关 + 板块相关 + 有关板块 + 缺口），当前 18 平台含 DeepSeek 搜索。"""
    a = pack.agg
    subject = f"HK{pack.code} {pack.name}".strip() or "标的"
    total = a.get('platforms_total', pack.platforms_total)
    # 兼容旧称：18 平台为十八平台，17 为十七
    label = f"{total}平台" if total != 18 else "十八平台"
    # 若为 17 保持旧文案兼容
    if total == 17:
        label = "十七平台"
    lines = [
        "---",
        f"🛰 **{subject} · {pack.hours}h {label}扫描**"
        f"（直接 {a.get('n_direct', 0)} 条 / 板块相关 {a.get('n_sector', 0)} 条，"
        f"命中 {a.get('platforms_hit', 0)}/{total} 平台）",
        "",
        f"- **情绪概览**：利好 {a.get('pos', 0)} / 利空 {a.get('neg', 0)}"
        f" / 中性 {a.get('neu', 0)}，动量均值 {a.get('mean', 0):+.2f}",
    ]
    if pack.direct:
        lines.append("\n▼ **直接相关新闻**（代码/名称/别名命中）")
        n = 0
        for platform, items in pack.direct.items():
            for i in items[:6]:
                n += 1
                t = i.title if len(i.title) <= 46 else i.title[:43] + "…"
                hit = "/".join(i.matched[:2])
                lines.append(f"{n}. [{i.label} {i.score:+.2f}] {t}"
                             f" — {platform} · {_fmt_age(i.age_h)} · 命中「{hit}」")
    else:
        lines.append("\n▼ **直接相关新闻**：窗口内未命中（见数据缺口）")
    if pack.sector_hits:
        sec_names = "、".join(pack.sectors[:6]) or "—"
        lines.append(f"\n▼ **板块相关快讯**（板块关键词命中：{sec_names}）")
        n = 0
        for platform, items in pack.sector_hits.items():
            for i in items[:4]:
                n += 1
                t = i.title if len(i.title) <= 46 else i.title[:43] + "…"
                lines.append(f"{n}. [{i.label} {i.score:+.2f}] {t}"
                             f" — {platform} · {_fmt_age(i.age_h)}")
    related = _related_sector_lines(pack)
    lines.append(f"\n▼ **有关板块**：{'、'.join(related) if related else '未识别'}")
    if pack.errors:
        lines.append("\n▼ **数据缺口**")
        lines += [f"- ⚠️ {e}" for e in pack.errors]
    return "\n".join(lines)


def scan_context(pack: ScanPack) -> str:
    """给 AI 的紧凑上下文（本地已分组打标，AI 只做综合），当前 18 平台含 DeepSeek。"""
    a = pack.agg
    total = a.get('platforms_total', pack.platforms_total)
    label = f"{total}平台" if total not in (17, 18) else ("十七平台" if total == 17 else "十八平台")
    lines = [
        f"【量价舆情动量·{label}扫描：{pack.code or '—'}/{pack.name or '—'}，"
        f"窗口 {pack.hours}h】",
        f"直接相关 {a.get('n_direct', 0)} 条 / 板块相关 {a.get('n_sector', 0)} 条，"
        f"命中 {a.get('platforms_hit', 0)}/{total} 平台；"
        f"情绪 利好{a.get('pos', 0)}/利空{a.get('neg', 0)}/中性{a.get('neu', 0)}，"
        f"动量均值 {a.get('mean', 0):+.2f}",
    ]
    for group_name, group in (("直接相关", pack.direct),
                              ("板块相关", pack.sector_hits)):
        if not group:
            continue
        lines.append(f"▼ {group_name}：")
        for platform, items in group.items():
            for i in items[:5]:
                lines.append(f"  - [{i.label}{i.score:+.2f}] {i.title}"
                             f"（{platform}·{_fmt_age(i.age_h)}）")
    if pack.dyn_sectors:
        lines.append("▼ 有关板块（新闻提及）："
                     + "、".join(f"{s}×{c}" for s, c in pack.dyn_sectors[:8]))
    if pack.errors:
        lines.append("▼ 数据缺口：" + "；".join(pack.errors[:8]))
    return "\n".join(lines)


# ---------------- 离线样例与自检 ----------------

CLS_SAMPLE = json.dumps({"data": [
    {"title": "阿里巴巴Q3营收超预期，云计算业务增长提速", "ctime": 9999},
    {"title": "电商板块午后异动拉升，多只概念股涨停", "ctime": 9999},
    {"title": "某公司债券违约被立案调查", "ctime": 9999},
]})

WSCN_SAMPLE = json.dumps({"data": {"lives": [
    {"content_text": "AI应用板块集体走强，云计算领涨", "display_time": 9999},
    {"title": "隔夜美股三大指数收涨", "display_time": 9999},
]}})

JIN10_TPL = ('var flash_newest = {"data": ['
             '{"data": {"content": "阿里巴巴股价大涨 6%，南向资金加仓",'
             ' "time": "{t1}"}},'
             '{"data": {"content": "国际油价小幅下跌", "time": "{t2}"}}'
             ']};')

GLH_SAMPLE = json.dumps({"result": [
    {"title": "港股电商板块走强，阿里巴巴上涨", "time": "{t1}"},
    {"title": "恒生指数低开高走", "time": "{t2}"},
]})

MKT_SAMPLE = json.dumps({"data": {"list": [
    {"title": "AI算力需求爆发 云计算板块受益", "published_at": 9999},
    {"title": "欧元区 PMI 低于预期", "published_at": 9999},
]}})

GOOGLE_SAMPLE_SCAN = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>阿里巴巴 09988.HK 回购股份公告 - 经济通</title>
<link>https://example.com/r1</link><pubDate>{d1}</pubDate></item>
<item><title>云业务增长提速 电商板块受益 - 财华社</title>
<link>https://example.com/r2</link><pubDate>{d2}</pubDate></item>
</channel></rss>"""

XUEQIU_SAMPLE = json.dumps({"data": {"items": [
    {"name": "阿里巴巴", "code": "HK09988", "percent": 2.35, "increment": 5312},
    {"name": "贵州茅台", "code": "SH600519", "percent": -0.8},
]}})

SOCIAL_SAMPLE_RAW = [
    {"title": "如何看待阿里巴巴新财报？ 1234万热度",
     "url": "https://z.hu/1", "ts": None},
    # 干扰项：完全不含代码/名称/板块词（注意别让标题撞上板块词如「电商」）
    {"title": "娱乐圈新片定档暑期", "url": "https://z.hu/2", "ts": None},
]


def demo_scan_pack(now: datetime | None = None) -> ScanPack:
    """用离线样例构造的演示 Pack（供渲染联调/自检复用，不触网）。

    与 scan_stock 相同的分组/窗口/兜底逻辑，平台内可用样例占满：
    直接相关 ≥5 条（财联社/金十/格隆汇/Google/雪球/知乎），
    板块相关 ≥2 条（财联社/华尔街见闻/格隆汇/MKTNews/Google）。
    """
    now = now or datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    q = build_stock_query("09988", "阿里巴巴")
    hours = DEFAULT_WINDOW_HOURS

    def _shift(h: float, fmt: str) -> str:
        return (now - timedelta(hours=h)).strftime(fmt)

    def _rfc(h: float) -> str:
        from email.utils import format_datetime
        return format_datetime(now - timedelta(hours=h), usegmt=True)

    parsed: dict[str, list[dict]] = {
        "财联社 电报": extract_flash_rows(json.loads(
            CLS_SAMPLE.replace("9999", str(int(now.timestamp() - 10 * 3600))))),
        "华尔街见闻 快讯": extract_flash_rows(json.loads(
            WSCN_SAMPLE.replace("9999", str(int(now.timestamp() - 20 * 3600))))),
        "金十数据": extract_flash_rows(json.loads(strip_js_shell(
            JIN10_TPL.replace("{t1}", _shift(30, "%Y-%m-%d %H:%M:%S"))
                     .replace("{t2}", _shift(31, "%Y-%m-%d %H:%M:%S"))))),
        "格隆汇 事件": extract_flash_rows(json.loads(
            GLH_SAMPLE.replace("{t1}", _shift(40, "%Y-%m-%d %H:%M:%S"))
                      .replace("{t2}", _shift(41, "%Y-%m-%d %H:%M:%S")))),
        "MKTNews 快讯": extract_flash_rows(json.loads(
            MKT_SAMPLE.replace("9999", str(int(now.timestamp() - 50 * 3600))))),
        "Google新闻": parse_google_rss(
            GOOGLE_SAMPLE_SCAN.replace("{d1}", _rfc(60)).replace("{d2}", _rfc(61))),
    }
    pack = ScanPack(hours=hours, code=q.code, name=q.name, sectors=q.sectors)
    for platform, raw in parsed.items():
        d_items, s_items = classify_items(platform, raw, q, hours, now)
        if platform == "Google新闻":   # 与线上 scan_stock 一致的检索词兜底
            known = {i.title for i in d_items} | {i.title for i in s_items}
            for r in raw:
                ts = r.get("ts")
                if ts is None or not in_window(ts, hours, now):
                    continue
                t = re.sub(r"\s+", " ", r["title"]).strip()
                if t and t not in known:
                    d_items.append(_mk_scan_item(platform, t, r.get("url", ""),
                                                 ts, now, "直接", [q.name]))
                    known.add(t)
        if d_items:
            pack.direct[platform] = d_items[:10]
        if s_items:
            pack.sector_hits[platform] = s_items[:10]
    pack.direct["雪球 热门股票"] = parse_xueqiu_hot(XUEQIU_SAMPLE, q, now)
    d_items, s_items = classify_items("知乎热榜", SOCIAL_SAMPLE_RAW, q, hours,
                                      now, time_filtered=False)
    if d_items:
        pack.direct["知乎热榜"] = d_items
    if s_items:
        pack.sector_hits["知乎热榜"] = s_items

    pack.dyn_sectors = extract_dyn_sectors(pack.direct, pack.sector_hits)
    direct_items = [i for v in pack.direct.values() for i in v]
    sector_items = [i for v in pack.sector_hits.values() for i in v]
    all_items = direct_items + sector_items
    pos = sum(1 for i in all_items if i.label == "利好")
    neg = sum(1 for i in all_items if i.label == "利空")
    pack.agg = {
        "n_direct": len(direct_items), "n_sector": len(sector_items),
        "n": len(all_items), "pos": pos, "neg": neg,
        "neu": len(all_items) - pos - neg,
        "mean": round(sum(i.score for i in all_items) / len(all_items), 2)
        if all_items else 0.0,
        "platforms_hit": len(set(pack.direct) | set(pack.sector_hits)),
        "platforms_total": 18,  # 含 DeepSeek 搜索
    }
    return pack


def selftest_scan() -> int:
    """离线自检：解析器 + 156h 窗口 + 代码/板块匹配 + 动态板块 + 渲染，当前 18 平台含 DeepSeek。"""
    fails = 0

    def check(name: str, cond: bool):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("🛰 十八平台股票扫描（离线自检，不触网，含 DeepSeek 搜索）")
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    ts10h = int(now.timestamp() - 10 * 3600)
    ts160h = int(now.timestamp() - 160 * 3600)

    check("代码补零 normalize 9988→09988", normalize_code("9988") == "09988")
    q = build_stock_query("09988", "阿里巴巴")
    check("直接词含代码变体+名称+别名",
          "09988" in q.direct_words and "9988.HK" in q.direct_words
          and "阿里巴巴" in q.direct_words and "Alibaba" in q.direct_words)
    check("预设板块含电商/云计算",
          "电商" in q.sectors and "云计算" in q.sectors)
    q2 = build_stock_query("09988", "", extra_sectors=["数字经济"])
    check("无名时档案补全名称+追加自定义板块",
          q2.name == "阿里巴巴" and "数字经济" in q2.sectors)
    q3 = build_stock_query("nvda", "")
    check("美股档案补全：中文名+英文别名+算力板块",
          q3.code == "NVDA" and q3.name == "英伟达"
          and "NVIDIA" in q3.direct_words and "算力" in q3.sectors
          and not any(w.endswith(".HK") for w in q3.direct_words))

    check("epoch 秒/毫秒均解析",
          parse_any_time(ts10h) is not None
          and parse_any_time(ts10h * 1000) is not None)
    check("中文时间串解析", parse_any_time("2026-08-06 10:00:00") is not None)
    check("156h 内保留",
          in_window(datetime.fromtimestamp(ts10h, UTC), 156, now))
    check("156h 外丢弃(160h)",
          not in_window(datetime.fromtimestamp(ts160h, UTC), 156, now))

    rows = extract_flash_rows(json.loads(CLS_SAMPLE.replace("9999", str(ts10h))))
    check("财联社解析 3 条带时间", len(rows) == 3 and rows[0]["ts"] is not None)
    jin10_filled = JIN10_TPL.replace("{t1}", "2026-08-06 10:00:00") \
                            .replace("{t2}", "2026-08-06 09:00:00")
    rows_j = extract_flash_rows(json.loads(strip_js_shell(jin10_filled)))
    check("金十剥JS壳解析 2 条", len(rows_j) == 2 and rows_j[0]["ts"] is not None)
    rows_x = _json_find_list(json.loads(XUEQIU_SAMPLE))
    check("雪球双层 data 定位列表", isinstance(rows_x, list) and len(rows_x) == 2)

    from email.utils import format_datetime
    g = GOOGLE_SAMPLE_SCAN.replace(
        "{d1}", format_datetime(now - timedelta(hours=148), usegmt=True)) \
        .replace("{d2}", format_datetime(now - timedelta(hours=149), usegmt=True))
    rows_g = parse_google_rss(g)
    check("Google RSS 解析 2 条且去来源尾缀",
          len(rows_g) == 2 and "经济通" not in rows_g[0]["title"])

    d_items, s_items = classify_items("财联社 电报", rows, q, 156, now)
    check("直接命中：阿里巴巴+超预期", len(d_items) == 1
          and any("阿里巴巴" in i.matched for i in d_items))
    check("板块命中：电商板块归入板块相关", len(s_items) == 1
          and "电商" in s_items[0].matched)
    check("无关条目被过滤",
          all("违约" not in i.title for i in d_items + s_items))
    rows_old = extract_flash_rows(json.loads(CLS_SAMPLE.replace("9999", str(ts160h))))
    d_old, s_old = classify_items("财联社 电报", rows_old, q, 156, now)
    check("160h 前新闻被 156h 窗口剔除", not d_old and not s_old)
    d_soc, s_soc = classify_items("知乎热榜", SOCIAL_SAMPLE_RAW, q, 156,
                                  now, time_filtered=False)
    check("社媒热榜实时快照命中直接相关", len(d_soc) == 1
          and d_soc[0].age_h is None and not s_soc)

    xq_items = parse_xueqiu_hot(XUEQIU_SAMPLE, q, now)
    check("雪球按名称命中且涨幅映射利好", len(xq_items) == 1
          and xq_items[0].label == "利好" and xq_items[0].score > 0)
    check("雪球未命中行被剔除", all("茅台" not in i.title for i in xq_items))
    check("雪球空查询不命中", not parse_xueqiu_hot(
        XUEQIU_SAMPLE, StockQuery(), now))

    s1 = extract_dyn_sectors(
        {"P": [_mk_scan_item("P", "港股电商板块走强 电商板块受益", "", None,
                             now, "板块", ["电商"])]}, {})
    check("动态板块：低频长名被高频短名吸收(电商板块)",
          [s for s, _ in s1] == ["电商板块"])
    s2 = extract_dyn_sectors(
        {"P": [_mk_scan_item("P", "AI应用板块集体走强", "", None,
                             now, "板块", ["AI"])]}, {})
    check("动态板块：同频子串伪影被吸收(AI应用板块)",
          [s for s, _ in s2] == ["AI应用板块"])

    pack = demo_scan_pack(now)
    a = pack.agg
    check("聚合：直接相关 ≥5 条", a["n_direct"] >= 5)
    check("聚合：板块相关 ≥2 条", a["n_sector"] >= 2)
    check("聚合：命中平台 3<n≤18", 3 < a["platforms_hit"] <= 18)
    md = render_scan_md(pack)
    check("渲染：标题含平台扫描/156h", ("平台扫描" in md) and "156h" in md)
    check("渲染：含直接分组与有关板块",
          "直接相关新闻" in md and "有关板块" in md)
    check("渲染：动态板块提取「电商板块」",
          any(s == "电商板块" for s, _ in pack.dyn_sectors))
    check("渲染：预设板块行带提及计数", "电商（预设" in md)
    ctx = scan_context(pack)
    check("AI 上下文含代码+板块行", "09988" in ctx and "有关板块" in ctx)

    print(f"\n{'✅ 十八平台扫描自检通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


# ---------------- CLI ----------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="量价舆情动量·十七平台股票扫描：输入股票代码，"
                    "检索最近 156 小时内的相关新闻与有关板块")
    p.add_argument("--code", default="", help="股票代码（如 09988 / 9988.HK / 600519）")
    p.add_argument("--name", default="", help="股票名称（留空时查内置档案补全）")
    p.add_argument("--hours", type=int, default=DEFAULT_WINDOW_HOURS,
                   help=f"检索窗口小时数（默认 {DEFAULT_WINDOW_HOURS}）")
    p.add_argument("--sectors", default="",
                   help="追加板块关键词，逗号分隔（如：电商,云计算）")
    p.add_argument("--timeout", type=int, default=12)
    p.add_argument("--selftest", action="store_true", help="离线自检后退出")
    p.add_argument("--json", action="store_true", help="以 JSON 输出聚合结果")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import sys
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest_scan()
    extra = [s.strip() for s in re.split(r"[,，]", args.sectors) if s.strip()]
    pack = scan_stock(code=args.code, name=args.name, hours=args.hours,
                      timeout=args.timeout, extra_sectors=extra)
    if args.json:
        print(json.dumps({
            "code": pack.code, "name": pack.name, "hours": pack.hours,
            "agg": pack.agg, "sectors": pack.sectors,
            "dyn_sectors": pack.dyn_sectors,
            "direct": {k: [i.title for i in v] for k, v in pack.direct.items()},
            "sector_hits": {k: [i.title for i in v]
                            for k, v in pack.sector_hits.items()},
            "errors": pack.errors,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_scan_md(pack))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
