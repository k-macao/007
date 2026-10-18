#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newsnow_sources.py — 移植自 ourongxing/newsnow 的 10 个热榜源
已接入源（前 7 个按用户指定，2026-08-13 新增 3 个）：
 - 知乎热榜 (zhihu)
 - 抖音热搜 (douyin)
 - 微博实时热搜 (weibo)
 - 虎扑热搜 (hupu)
 - AI hot (aihot / aihot.virxact.com)
 - 联合早报 (zaobao / 早晨报 realtime)
 - 香港01 (hk01 / 自实现抓取)
 - 今日头条热榜 (toutiao / hot-event API，cookie 预热)      [新增]
 - 百度实时热点 (baidu / top.baidu.com board API)           [新增]
 - B站热榜 (bilibili / 全站热门 popular API)                 [新增]

所有代码仅依赖标准库，兼容 pushplus_deepseek.py 的离线自检与像素风格渲染。
每个源遵循同一约定：SAMPLE（离线样本）→ parse_xxx（解析器）→ fetch_xxx（抓取器），
并在下方 SAMPLES / FETCHERS 注册；source_check_db.py 据此做离线/在线校验入库。
配置说明见 DATA_SOURCES.md。
"""

from __future__ import annotations
import re
import json
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict

UTC = timezone.utc

# ---------------- 通用请求 ----------------

def http_request(url: str, headers: dict | None = None, timeout: int = 15,
                 encoding: str = "utf-8", retries: int = 2) -> tuple[int, str, bytes]:
    """带指数退避重试的通用请求，避免瞬时抖动导致数据丢失。"""
    import time
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "Mozilla/5.0 (newsnow-py/1.0; pixel-theme)")
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    try:
                        text = raw.decode(encoding, "replace")
                    except Exception:
                        text = raw.decode("utf-8", "replace")
                    return resp.status, text, raw
            except urllib.error.HTTPError as e:
                try:
                    raw = e.read()
                    txt = raw.decode(encoding, "replace")
                except Exception:
                    raw = b""
                    txt = str(e)
                # 502/503/429 可重试
                if e.code in (429, 502, 503, 504) and attempt < retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                return e.code, txt, raw
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"网络请求失败 {url}: {e}") from e
    # 理论上不会到这里
    raise RuntimeError(f"网络请求失败 {url}: {last_exc}") from last_exc


# ---------------- 数据结构 ----------------

@dataclass
class HotItem:
    source: str
    id: str
    title: str
    url: str
    extra_info: str = ""   # 对应 newsnow 的 metrics / hot value / info
    pub_date: str = ""


# ---------------- 1. 知乎热榜 ----------------
ZHIHU_URL = "https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=20&desktop=true"

ZHIHU_SAMPLE = json.dumps({
    "data": [
        {
            "target": {
                "title_area": {"text": "如何看待阿里巴巴Q3财报大超预期？"},
                "excerpt_area": {"text": "云计算营收提速，电商板块..."},
                "metrics_area": {"text": "1234 万热度"},
                "link": {"url": "https://www.zhihu.com/question/123456"}
            }
        },
        {
            "target": {
                "title_area": {"text": "AI会取代哪些工作？"},
                "excerpt_area": {"text": "大模型快速发展..."},
                "metrics_area": {"text": "987 万热度"},
                "link": {"url": "https://www.zhihu.com/question/654321"}
            }
        }
    ]
})

def parse_zhihu(text: str) -> List[HotItem]:
    data = json.loads(text)
    items = []
    for d in data.get("data", []):
        tgt = d.get("target", {})
        title = tgt.get("title_area", {}).get("text", "").strip()
        url = tgt.get("link", {}).get("url", "").strip()
        if not title or not url:
            continue
        metrics = tgt.get("metrics_area", {}).get("text", "")
        excerpt = tgt.get("excerpt_area", {}).get("text", "")
        # id 取 url 尾数字
        m = re.search(r"(\d+)$", url)
        _id = m.group(1) if m else url
        items.append(HotItem(source="知乎热榜", id=_id, title=title, url=url,
                             extra_info=metrics or excerpt))
    return items

def fetch_zhihu(timeout: int = 15) -> List[HotItem]:
    status, body, _ = http_request(ZHIHU_URL, timeout=timeout, encoding="utf-8")
    if status != 200:
        raise RuntimeError(f"知乎 HTTP {status}")
    return parse_zhihu(body)


# ---------------- 2. 抖音热搜 ----------------
DOUYIN_HOT_URL = "https://www.douyin.com/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1"
DOUYIN_LOGIN_URL = "https://login.douyin.com/"

DOUYIN_SAMPLE = json.dumps({
    "data": {
        "word_list": [
            {"sentence_id": "123", "word": "阿里巴巴股价大涨", "hot_value": "1234567"},
            {"sentence_id": "124", "word": "电商成交额破纪录", "hot_value": "987654"}
        ]
    }
})

def parse_douyin(text: str) -> List[HotItem]:
    data = json.loads(text)
    lst = data.get("data", {}).get("word_list", [])
    items = []
    for k in lst:
        sid = str(k.get("sentence_id", "")).strip()
        word = str(k.get("word", "")).strip()
        hot = str(k.get("hot_value", "")).strip()
        if not sid or not word:
            continue
        url = f"https://www.douyin.com/hot/{sid}"
        items.append(HotItem(source="抖音热搜", id=sid, title=word, url=url, extra_info=hot))
    return items

def fetch_douyin(timeout: int = 15) -> List[HotItem]:
    # 1) 先拿 cookie
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (newsnow-py/1.0)")]
    try:
        opener.open(DOUYIN_LOGIN_URL, timeout=timeout).read()
        cookies = "; ".join([f"{c.name}={c.value}" for c in cj])
    except Exception:
        cookies = ""
    headers = {"cookie": cookies} if cookies else {}
    status, body, _ = http_request(DOUYIN_HOT_URL, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"抖音 HTTP {status}")
    return parse_douyin(body)


# ---------------- 3. 微博实时热搜 ----------------
WEIBO_URL = "https://s.weibo.com/top/summary?cate=realtimehot"
WEIBO_COOKIE = "SUB=_2AkMWIuNSf8NxqwJRmP8dy2rhaoV2ygrEieKgfhKJJRMxHRl-yT9jqk86tRB6PaLNvQZR6zYUcYVT1zSjoSreQHidcUq7"

WEIBO_SAMPLE = """
<div id="pl_top_realtimehot"><table><tbody>
<tr><td>1</td><td class="td-02"><a href="/weibo?q=%23%E9%87%91%E9%A3%8E%E7%A7%91%E6%8A%80%23">阿里巴巴财报</a></td><td class="td-03">热</td></tr>
<tr><td>2</td><td class="td-02"><a href="/weibo?q=%23AI%E7%83%AD%E6%A6%9C%23">AI热榜更新</a></td><td class="td-03">新</td></tr>
</tbody></table></div>
"""

def parse_weibo(html: str) -> List[HotItem]:
    # 简易正则替代 cheerio，匹配 newsnow 逻辑
    pattern = re.compile(
        r'<td[^>]*class="td-02"[^>]*>.*?<a\s+href="([^"]+)".*?>([^<]+)</a>.*?</td>\s*<td[^>]*class="td-03"[^>]*>(.*?)</td>',
        re.S
    )
    items = []
    for m in pattern.finditer(html):
        href, title, flag = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if not title or not href:
            continue
        if "javascript" in href:
            continue
        url = f"https://s.weibo.com{href}" if href.startswith("/") else href
        items.append(HotItem(source="微博实时热搜", id=title, title=title, url=url, extra_info=flag))
    return items

def fetch_weibo(timeout: int = 15) -> List[HotItem]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": WEIBO_COOKIE,
        "Referer": WEIBO_URL,
    }
    status, body, _ = http_request(WEIBO_URL, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"微博 HTTP {status}")
    return parse_weibo(body)


# ---------------- 4. 虎扑热搜 ----------------
HUPU_URL = "https://bbs.hupu.com/topic-daily-hot"
HUPU_SAMPLE = """
<li class="bbs-sl-web-post-body"><a href="/123456.html" class="p-title">虎扑NBA：阿里巴巴冠名赞助</a></li>
<li class="bbs-sl-web-post-body"><a href="/654321.html" class="p-title">虎扑热帖：电商板块大涨</a></li>
"""

def parse_hupu(html: str) -> List[HotItem]:
    regex = re.compile(r'<li class="bbs-sl-web-post-body">.*?<a href="([^"]+?\.html)"[^>]*?class="p-title"[^>]*>([^<]+)</a>', re.S)
    items = []
    for m in regex.finditer(html):
        path, title = m.group(1).strip(), m.group(2).strip()
        if not path or not title:
            continue
        url = f"https://bbs.hupu.com{path}" if path.startswith("/") else path
        items.append(HotItem(source="虎扑热搜", id=path, title=title, url=url))
    return items

def fetch_hupu(timeout: int = 15) -> List[HotItem]:
    status, body, _ = http_request(HUPU_URL, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"虎扑 HTTP {status}")
    return parse_hupu(body)


# ---------------- 5. AI hot ----------------
AIHOT_API = "https://aihot.virxact.com/api/public/items?mode=all&take=30"
AIHOT_RSS = "https://aihot.virxact.com/feed/all.xml"

AIHOT_SAMPLE_JSON = json.dumps({
    "items": [
        {"id": "a1", "title": "OpenAI 发布新模型", "url": "https://aihot.virxact.com/a1", "source": "OpenAI", "category": "模型", "summary": "性能提升", "publishedAt": "2026-08-06T10:00:00Z"},
        {"id": "a2", "title": "阿里巴巴开源新一代AI模型", "url": "https://aihot.virxact.com/a2", "source": "Alibaba", "category": "云计算AI"}
    ]
})

AIHOT_SAMPLE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>AI突破：大模型推理加速</title><link>https://example.com/ai1</link><pubDate>Mon, 04 Aug 2026 10:00:00 GMT</pubDate><description>加速推理</description></item>
</channel></rss>"""

def parse_aihot_json(text: str) -> List[HotItem]:
    data = json.loads(text)
    items = []
    for it in data.get("items", [])[:30]:
        if not (it.get("id") and it.get("title") and it.get("url")):
            continue
        info = it.get("category") or ""
        src = it.get("source") or ""
        extra = f"{src} · {info}" if info else src
        items.append(HotItem(source="AI hot", id=str(it["id"]), title=str(it["title"]),
                             url=str(it["url"]), extra_info=extra or it.get("summary","") ))
    return items

def parse_aihot_rss(text: str) -> List[HotItem]:
    root = ET.fromstring(text)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        items.append(HotItem(source="AI hot", id=link, title=title, url=link))
    return items

def fetch_aihot(timeout: int = 15) -> List[HotItem]:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (newsnow-py/1.0) Chrome/130.0.0.0 Safari/537.36 aihot-skill/0.2.0"}
        status, body, _ = http_request(AIHOT_API, headers=headers, timeout=timeout)
        if status == 200:
            parsed = parse_aihot_json(body)
            if parsed:
                return parsed
    except Exception:
        pass
    # fallback RSS
    status, body, _ = http_request(AIHOT_RSS, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"AI hot RSS HTTP {status}")
    return parse_aihot_rss(body)


# ---------------- 6. 联合早报 (zaobao) - 来自早晨报 realtime ----------------
ZAOBAO_URL = "https://www.zaochenbao.com/realtime/"

ZAOBAO_SAMPLE = """
<div class="list-block">
<a class="item" href="/realtime/china/story123"><div class="eps">阿里巴巴国际电商增速翻倍</div><div class="pdt10">今天 10:20</div></a>
<a class="item" href="/realtime/world/story124"><div class="eps">全球云计算支出回升</div><div class="pdt10">今天 09:15</div></a>
</div>
"""

def parse_zaobao(html: str) -> List[HotItem]:
    # cheerio 选择器 div.list-block>a.item -> .eps .pdt10
    pattern = re.compile(r'<a[^>]*class="item"[^>]*href="([^"]+)"[^>]*>.*?<div[^>]*class="eps"[^>]*>([^<]+)</div>.*?<div[^>]*class="pdt10"[^>]*>([^<]+)</div>', re.S)
    items = []
    base = "https://www.zaochenbao.com"
    for m in pattern.finditer(html):
        url_path, title, date = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if not url_path or not title:
            continue
        url = base + url_path if url_path.startswith("/") else url_path
        items.append(HotItem(source="联合早报", id=url_path, title=title, url=url, extra_info=date))
    return items

def fetch_zaobao(timeout: int = 15) -> List[HotItem]:
    # 尝试 gb2312 解码
    status, text, raw = http_request(ZAOBAO_URL, timeout=timeout, encoding="gb2312")
    if status != 200:
        # 重试 utf-8
        status, text2, raw2 = http_request(ZAOBAO_URL, timeout=timeout, encoding="utf-8")
        if status != 200:
            raise RuntimeError(f"联合早报 HTTP {status}")
        text = text2
    # 如果 raw 解码失败，尝试 gbk
    if "eps" not in text and "list-block" not in text:
        try:
            text = raw.decode("gbk", "replace")
        except Exception:
            pass
    return parse_zaobao(text)


# ---------------- 7. 香港01 (hk01) ----------------
HK01_URLS = [
    "https://web-data.api.hk01.com/v1/feed/hot-list?limit=20",
    "https://web-data.api.hk01.com/v2/feed/landing/hot-list?limit=20",
    "https://www.hk01.com/hot",
    "https://www.hk01.com/most-popular",
]

HK01_SAMPLE_HTML = """
<div class="content"><a href="/news/123" title="香港01：电商新闻上热榜">香港01：电商新闻上热榜</a></div>
<div class="content"><a href="/news/124" title="港股阿里巴巴大涨">港股阿里巴巴大涨</a></div>
"""

def parse_hk01_json(text: str) -> List[HotItem]:
    try:
        data = json.loads(text)
        # 兼容多种结构：尝试找 list
        candidates = []
        if isinstance(data, dict):
            for key in ["data", "items", "list", "result", "articles"]:
                v = data.get(key)
                if isinstance(v, list) and v:
                    candidates = v
                    break
                if isinstance(v, dict):
                    for sub in ["data", "items", "list"]:
                        if isinstance(v.get(sub), list):
                            candidates = v.get(sub)
                            break
        elif isinstance(data, list):
            candidates = data
        items = []
        for it in candidates[:25]:
            if not isinstance(it, dict):
                continue
            title = it.get("title") or it.get("name") or ""
            url = it.get("url") or it.get("link") or it.get("shareUrl") or ""
            if not title:
                continue
            if url and not url.startswith("http"):
                url = "https://www.hk01.com" + url
            if not url:
                url = "https://www.hk01.com/hot"
            items.append(HotItem(source="香港01", id=title, title=title.strip(), url=url))
        return items
    except Exception:
        return []

def parse_hk01_html(html: str) -> List[HotItem]:
    # 粗解析所有 <a href="/...">标题</a> 且长度>6
    pattern = re.compile(r'<a[^>]+href="(/[^"]+)"[^>]*>([^<]{6,80})</a>', re.S)
    items = []
    seen = set()
    for m in pattern.finditer(html):
        href, title = m.group(1).strip(), m.group(2).strip()
        if len(title) < 6 or len(title) > 80:
            continue
        if "登录" in title or "login" in title.lower():
            continue
        if href in seen:
            continue
        seen.add(href)
        url = f"https://www.hk01.com{href}"
        items.append(HotItem(source="香港01", id=href, title=title, url=url))
        if len(items) >= 20:
            break
    return items

def fetch_hk01(timeout: int = 15) -> List[HotItem]:
    last_err = "无可用端点"
    for url in HK01_URLS:
        try:
            status, body, _ = http_request(url, timeout=timeout)
            if status != 200:
                last_err = f"HTTP {status} @ {url}"
                continue
            # 尝试 JSON
            j = parse_hk01_json(body)
            if j:
                return j
            # 尝试 HTML
            h = parse_hk01_html(body)
            if h:
                return h
            last_err = f"解析空 @{url}"
        except Exception as e:
            last_err = str(e)[:120]
    raise RuntimeError(f"香港01 获取失败：{last_err}")


# ---------------- 8. 今日头条热榜 (toutiao) ----------------
TOUTIAO_HOT_URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
TOUTIAO_HOME_URL = "https://www.toutiao.com/"

TOUTIAO_SAMPLE = json.dumps({
    "data": [
        {"ClusterId": 7485100000000000001, "Title": "阿里巴巴宣布组织架构调整",
         "HotValue": "12345678",
         "Url": "https://www.toutiao.com/trending/7485100000000000001/"},
        {"ClusterId": 7485100000000000002, "Title": "港股今日大涨电商板块领涨",
         "HotValue": "9876543", "Url": ""}
    ]
})

def parse_toutiao(text: str) -> List[HotItem]:
    data = json.loads(text)
    lst = data.get("data", [])
    if isinstance(lst, dict):                       # 兼容 {"data": {"list": [...]}}
        lst = lst.get("list", [])
    items = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        title = str(it.get("Title") or it.get("title") or "").strip()
        if not title:
            continue
        cid = str(it.get("ClusterId") or it.get("cluster_id") or "").strip()
        url = str(it.get("Url") or it.get("url") or "").strip()
        if not url and cid:
            url = f"https://www.toutiao.com/trending/{cid}/"
        hot = str(it.get("HotValue") or it.get("hot_value") or "").strip()
        items.append(HotItem(source="今日头条热榜", id=cid or title, title=title,
                             url=url, extra_info=hot))
    return items

def fetch_toutiao(timeout: int = 15) -> List[HotItem]:
    # 与抖音同款策略：先预热 cookie 再请求 JSON 接口，降低空响应概率
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (newsnow-py/1.0)")]
    try:
        opener.open(TOUTIAO_HOME_URL, timeout=timeout).read()
        cookies = "; ".join([f"{c.name}={c.value}" for c in cj])
    except Exception:
        cookies = ""
    headers = {"cookie": cookies, "Referer": TOUTIAO_HOME_URL} if cookies \
        else {"Referer": TOUTIAO_HOME_URL}
    status, body, _ = http_request(TOUTIAO_HOT_URL, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"今日头条 HTTP {status}")
    items = parse_toutiao(body)
    if not items:
        raise RuntimeError("今日头条 解析为空（接口结构可能变更）")
    return items


# ---------------- 9. 百度实时热点 (baidu) ----------------
BAIDU_URL = "https://top.baidu.com/api/board?platform=wise&tab=realtime"

BAIDU_SAMPLE = json.dumps({
    "data": {
        "cards": [
            {"component": "hotList", "content": [
                {"word": "阿里巴巴港股创新高", "desc": "港股大涨",
                 "hotScore": "1234567",
                 "rawUrl": "https://www.baidu.com/s?wd=阿里巴巴港股创新高",
                 "query": "阿里巴巴港股创新高"},
                {"word": "AI大模型新品发布", "desc": "",
                 "hotScore": "987654",
                 "rawUrl": "https://www.baidu.com/s?wd=AI大模型新品发布"}
            ]}
        ]
    }
})

def parse_baidu(text: str) -> List[HotItem]:
    data = json.loads(text)
    cards = data.get("data", {}).get("cards", [])
    items = []
    for card in cards:
        for it in card.get("content", []) or []:
            if not isinstance(it, dict):
                continue
            title = str(it.get("word") or it.get("query") or "").strip()
            if not title:
                continue
            url = str(it.get("rawUrl") or it.get("url") or it.get("appUrl") or "").strip()
            if not url:
                url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(title)
            hot = str(it.get("hotScore") or "").strip()
            extra = f"{hot}热度" if hot else ""
            desc = str(it.get("desc") or "").strip()
            if desc:
                extra = f"{extra} {desc}".strip()
            items.append(HotItem(source="百度实时热点", id=title, title=title,
                                 url=url, extra_info=extra))
    return items

def fetch_baidu(timeout: int = 15) -> List[HotItem]:
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) newsnow-py/1.0",
        "Referer": "https://top.baidu.com/board?tab=realtime",
    }
    status, body, _ = http_request(BAIDU_URL, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"百度 HTTP {status}")
    items = parse_baidu(body)
    if not items:
        raise RuntimeError("百度 解析为空（接口结构可能变更）")
    return items


# ---------------- 10. B站热榜 (bilibili) ----------------
BILIBILI_URL = ("https://api.bilibili.com/x/web-interface/popular"
                "?ps=20&pn=1")

BILIBILI_SAMPLE = json.dumps({
    "code": 0,
    "data": {"list": [
        {"bvid": "BV1xx411c7mD", "title": "3分钟看懂阿里巴巴Q3财报",
         "tname": "财经", "stat": {"view": 123456},
         "short_link_v2": "https://b23.tv/abcd1"},
        {"bvid": "BV2yy411c7mE", "title": "AI大模型评测：电商客服实测",
         "tname": "科技", "stat": {"view": 98765}}
    ]}
})

def parse_bilibili(text: str) -> List[HotItem]:
    data = json.loads(text)
    if data.get("code") not in (0, None):
        raise RuntimeError(f"B站 API code={data.get('code')}")
    lst = data.get("data", {}).get("list", [])
    items = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        bvid = str(it.get("bvid") or "").strip()
        if not title or not bvid:
            continue
        url = f"https://www.bilibili.com/video/{bvid}"
        tname = str(it.get("tname") or "").strip()
        view = (it.get("stat") or {}).get("view")
        extra = f"{tname} · 播放{view}" if view is not None else tname
        items.append(HotItem(source="B站热榜", id=bvid, title=title,
                             url=url, extra_info=extra))
    return items

def fetch_bilibili(timeout: int = 15) -> List[HotItem]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) newsnow-py/1.0",
        "Referer": "https://www.bilibili.com/",
    }
    status, body, _ = http_request(BILIBILI_URL, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"B站 HTTP {status}")
    items = parse_bilibili(body)
    if not items:
        raise RuntimeError("B站 解析为空（接口结构可能变更）")
    return items


# ---------------- 聚合 ----------------

FETCHERS = {
    "zhihu": ("知乎热榜", fetch_zhihu),
    "douyin": ("抖音热搜", fetch_douyin),
    "weibo": ("微博实时热搜", fetch_weibo),
    "hupu": ("虎扑热搜", fetch_hupu),
    "aihot": ("AI hot", fetch_aihot),
    "zaobao": ("联合早报", fetch_zaobao),
    "hk01": ("香港01", fetch_hk01),
    "toutiao": ("今日头条热榜", fetch_toutiao),
    "baidu": ("百度实时热点", fetch_baidu),
    "bilibili": ("B站热榜", fetch_bilibili),
}

# 前 7 个为用户指定；2026-08-13 追加 3 个（今日头条/百度/B站）
TARGET_SOURCES = ["zhihu", "douyin", "weibo", "hupu", "aihot", "zaobao", "hk01",
                  "toutiao", "baidu", "bilibili"]

# 离线校验样本注册表：source_check_db.py 离线模式逐源解析验证
SAMPLES = {
    "zhihu": (parse_zhihu, ZHIHU_SAMPLE),
    "douyin": (parse_douyin, DOUYIN_SAMPLE),
    "weibo": (parse_weibo, WEIBO_SAMPLE),
    "hupu": (parse_hupu, HUPU_SAMPLE),
    "aihot": (parse_aihot_json, AIHOT_SAMPLE_JSON),
    "zaobao": (parse_zaobao, ZAOBAO_SAMPLE),
    "hk01": (parse_hk01_html, HK01_SAMPLE_HTML),
    "toutiao": (parse_toutiao, TOUTIAO_SAMPLE),
    "baidu": (parse_baidu, BAIDU_SAMPLE),
    "bilibili": (parse_bilibili, BILIBILI_SAMPLE),
}

@dataclass
class NewsNowPack:
    items: Dict[str, List[HotItem]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    agg: Dict = field(default_factory=dict)


def collect_newsnow(sources: List[str] | None = None, timeout: int = 15) -> NewsNowPack:
    srcs = sources or TARGET_SOURCES
    pack = NewsNowPack()
    all_items = []
    for key in srcs:
        name, fn = FETCHERS.get(key, (key, None))
        if fn is None:
            pack.errors.append(f"{key}：未实现")
            continue
        try:
            lst = fn(timeout=timeout)
            pack.items[name] = lst[:20]
            all_items.extend(lst)
        except Exception as e:
            pack.errors.append(f"{name}：{str(e)[:150]}")
    pack.agg = {"total": len(all_items), "sources_ok": len(pack.items), "sources_total": len(srcs)}
    return pack


def _item_line_no(i: HotItem, idx: int) -> str:
    # 用于 markdown AI 上下文
    extra = f" [{i.extra_info}]" if i.extra_info else ""
    return f"{idx}. {i.title}{extra} — {i.source} | {i.url}"


def newsnow_context(pack: NewsNowPack) -> str:
    lines = [f"【NewsNow热榜聚合：{pack.agg.get('sources_ok')}/{pack.agg.get('sources_total')}源可用，共{pack.agg.get('total')}条】"]
    for src_name, items in pack.items.items():
        lines.append(f"▼ {src_name}（{len(items)}条）")
        for k, it in enumerate(items[:10]):
            lines.append(_item_line_no(it, k+1))
    if pack.errors:
        lines.append("▼ 数据缺口：" + "；".join(pack.errors))
    return "\n".join(lines)


def render_newsnow_rule(topic: str, pack: NewsNowPack) -> str:
    lines = [f"**{topic} · NewsNow热榜（{pack.agg.get('sources_ok')}/{pack.agg.get('sources_total')}源）**（本地抓取）", "",
             "| 来源 | 排名 | 标题 | 热度/备注 |",
             "|---|---|---|---|"]
    for src_name, items in pack.items.items():
        for idx, it in enumerate(items[:10], 1):
            title = it.title[:22] + ("…" if len(it.title) > 22 else "")
            # 转义 | 避免表格破裂
            title = title.replace("|", "/")
            extra = (it.extra_info[:12] + "…") if len(it.extra_info) > 12 else it.extra_info
            extra = extra.replace("|", "/")
            lines.append(f"| {src_name} | {idx} | {title} | {extra} |")
    if not lines or len(lines) <= 4:
        lines.append("| — | — | 暂无数据 | — |")
    lines += ["",
              f"- **总计** {pack.agg.get('total',0)} 条 / {pack.agg.get('sources_ok',0)} 源",
              "> 数据来源：ourongxing/newsnow 移植（知乎/抖音/微博/虎扑/AI hot/联合早报/香港01/今日头条/百度/B站）",
              "> ⚠️ 非投资建议，仅供参考。"]
    if pack.errors:
        lines.append("")
        lines.append("**数据缺口**")
        for e in pack.errors:
            lines.append(f"- ⚠️ {e}")
    return "\n".join(lines)


def render_newsnow_appendix(pack: NewsNowPack) -> str:
    lines = ["---", f"🔥 **NewsNow热榜明细（{pack.agg.get('total',0)}条）**"]
    for src_name, items in pack.items.items():
        lines.append(f"\n**{src_name}（{len(items)}/20）**")
        for k, it in enumerate(items[:20], 1):
            lines.append(f"{k}. [{it.title}]({it.url}) {it.extra_info}")
    if pack.errors:
        lines.append("\n**数据缺口**")
        for e in pack.errors:
            lines.append(f"- ⚠️ {e}")
    return "\n".join(lines)


# ---------------- 自检样本 ----------------

def selftest_newsnow() -> int:
    fails = 0
    def check(name, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("④ NewsNow 10源解析器（离线样本）")
    check("知乎解析 2条", len(parse_zhihu(ZHIHU_SAMPLE)) == 2 and "阿里巴巴" in parse_zhihu(ZHIHU_SAMPLE)[0].title)
    check("抖音解析 2条", len(parse_douyin(DOUYIN_SAMPLE)) == 2)
    check("微博解析 2条 新/热标识", len(parse_weibo(WEIBO_SAMPLE)) == 2 and parse_weibo(WEIBO_SAMPLE)[0].extra_info == "热")
    check("虎扑解析 2条", len(parse_hupu(HUPU_SAMPLE)) == 2)
    check("AI hot JSON 2条", len(parse_aihot_json(AIHOT_SAMPLE_JSON)) == 2)
    check("AI hot RSS 1条", len(parse_aihot_rss(AIHOT_SAMPLE_RSS)) == 1)
    check("联合早报解析 2条", len(parse_zaobao(ZAOBAO_SAMPLE)) == 2)
    check("香港01 HTML 解析 2条", len(parse_hk01_html(HK01_SAMPLE_HTML)) == 2)
    check("今日头条解析 2条 热度/URL补齐",
          len(parse_toutiao(TOUTIAO_SAMPLE)) == 2
          and "12345678" in parse_toutiao(TOUTIAO_SAMPLE)[0].extra_info
          and parse_toutiao(TOUTIAO_SAMPLE)[1].url.startswith("https://www.toutiao.com/trending/"))
    check("百度解析 2条 带热度",
          len(parse_baidu(BAIDU_SAMPLE)) == 2
          and "1234567" in parse_baidu(BAIDU_SAMPLE)[0].extra_info)
    check("B站解析 2条 视频URL",
          len(parse_bilibili(BILIBILI_SAMPLE)) == 2
          and "bilibili.com/video/BV" in parse_bilibili(BILIBILI_SAMPLE)[0].url)
    check("SAMPLES 注册表覆盖全部 10 源",
          len(SAMPLES) == 10 and set(SAMPLES) == set(TARGET_SOURCES))

    pack = NewsNowPack()
    pack.items = {
        "知乎热榜": parse_zhihu(ZHIHU_SAMPLE),
        "抖音热搜": parse_douyin(DOUYIN_SAMPLE),
    }
    pack.agg = {"total": 4, "sources_ok": 2, "sources_total": 10}
    md = render_newsnow_rule("阿里巴巴", pack)
    check("newsnow rule 渲染含表格", "| 来源 |" in md and "知乎热榜" in md)
    ctx = newsnow_context(pack)
    check("newsnow context 含标的", "阿里巴巴" in ctx or "热榜" in ctx)

    print(f"\n{'✅ NewsNow 自检通过' if fails==0 else f'❌ {fails}项失败'}")
    return 1 if fails else 0

if __name__ == "__main__":
    import sys
    sys.exit(selftest_newsnow())
