#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯快讯提取脚本
每个数据源只提取 1 条最新内容，不做任何分析

2026-10 同步接口换代：华尔街见闻改 api-one.wallstcn.com、财联社改
签名端点（sign=md5(sha1(排序 query))）、格隆汇改 lives/v4、
法布财经（FastBull）无公开 JSON，用正则从服务端渲染列表页取第一条。
正式解析逻辑见 pushplus_deepseek.py 的 collect_feeds，本脚本只是快速探活。
"""

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime


def cls_signed_url(base, extra=None):
    """财联社签名：与 pushplus_deepseek.cls_signed_url 同一算法。"""
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "8.7.9"}
    params.update({k: str(v) for k, v in (extra or {}).items()})
    qs = urllib.parse.urlencode(sorted(params.items()))
    sign = hashlib.md5(
        hashlib.sha1(qs.encode("utf-8")).hexdigest().encode("utf-8")
    ).hexdigest()
    return f"{base}?{qs}&sign={sign}"


def build_specs():
    return [
        ("MKTNews 快讯", ["https://api.mktnews.net/api/flash?limit=5"]),
        ("华尔街见闻 快讯", ["https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=5"]),
        ("华尔街见闻 最新", ["https://api-one.wallstcn.com/apiv1/content/information-flow?channel=global-channel&accept=article&limit=5"]),
        ("华尔街见闻 最热", ["https://api-one.wallstcn.com/apiv1/content/articles/hot?period=all"]),
        ("财联社 电报", [cls_signed_url(
            "https://www.cls.cn/v1/roll/get_roll_list",
            {"last_time": int(time.time()), "refresh_type": 1, "rn": 10})]),
        ("财联社 深度", [cls_signed_url(
            "https://www.cls.cn/v3/depth/home/assembled/1000")]),
        ("财联社 热门", [cls_signed_url(
            "https://www.cls.cn/v2/article/hot/list")]),
        ("雪球 热门股票", []),  # 专用流程
        ("格隆汇 事件", ["https://www.gelonghui.com/api/live-channels/all/lives/v4"]),
        ("法布财经 快讯", ["https://www.fastbull.com/cn/express-news"]),
        ("法布财经 头条", ["https://www.fastbull.com/cn/news"]),
        ("金十数据", ["https://www.jin10.com/flash_newest.js"]),
    ]


FEED_SPECS = build_specs()

TITLE_KEYS = ["title", "content_text", "content_short", "content",
              "digest", "summary", "brief", "name", "description"]
LIST_KEYS = ["data", "list", "items", "result", "news", "lives",
             "roll_data", "depth_list", "top_article", "day_items"]

REFERERS = [
    ("wallstcn.com", "https://wallstreetcn.com/"),
    ("cls.cn", "https://www.cls.cn/"),
    ("gelonghui.com", "https://www.gelonghui.com/live"),
    ("fastbull.com", "https://www.fastbull.com/cn/"),
    ("jin10.com", "https://www.jin10.com/"),
]


def fetch(url, timeout=15):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    for frag, referer in REFERERS:
        if frag in url:
            req.add_header("Referer", referer)
            break
    if "jin10.com" in url:
        req.add_header("x-app-id", "rU6QIu7JHe2gOUTe")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def extract_one(payload, source):
    """从任意 JSON 中提取第一条标题（向下钻常见列表键/嵌套包裹）。"""
    for _ in range(4):
        if isinstance(payload, dict):
            nxt = None
            for key in LIST_KEYS:
                if isinstance(payload.get(key), (list, dict)):
                    nxt = payload[key]
                    break
            if nxt is None:
                break
            payload = nxt
        else:
            break
    if isinstance(payload, list) and payload:
        for row in payload:
            if not isinstance(row, dict):
                continue
            if row.get("is_ad") or row.get("resource_type") in ("ad", "theme"):
                continue
            scopes = [row] + [row[k] for k in ("data", "resource")
                              if isinstance(row.get(k), dict)]
            for sc in scopes:
                for k in TITLE_KEYS:
                    v = sc.get(k)
                    if isinstance(v, str) and len(v.strip()) >= 4:
                        title = re.sub(r"<[^>]+>", "", v).strip()
                        return f"{source}：{title}"
    return f"{source}：（提取失败）"


def extract_fastbull(body, source):
    """FastBull 列表页正则粗提取（探活用；正式解析在 pushplus_deepseek）。"""
    if "express" in source or "快讯" in source:
        m = re.search(r'title_name[^>]*>([^<]{4,300})<', body)
        if m:
            text = m.group(1).strip()
            m2 = re.match(r"【(.+?)】", text)
            return f"{source}：{m2.group(1) if m2 and len(m2.group(1)) >= 4 else text}"
    else:
        m = re.search(r'trending_type[\s\S]{0,2000}?class="title"[^>]*>([^<]{4,200})<', body)
        if m:
            return f"{source}：{m.group(1).strip()}"
    return f"{source}：（提取失败）"


def main():
    print(f"【快讯提取】{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    for name, urls in FEED_SPECS:
        if not urls:
            print(f"{name}：（无公开端点）")
            continue

        item = None
        for url in urls:
            body = fetch(url)
            if not body:
                continue
            if "fastbull.com" in url:
                item = extract_fastbull(body, name)
                if "失败" not in item:
                    break
                continue
            try:
                data = json.loads(body)
                item = extract_one(data, name)
                if item and "失败" not in item:
                    break
            except Exception:
                # 金十数据特殊处理
                if "jin10" in url:
                    m = re.search(r'[\{\[]', body)
                    if m:
                        try:
                            data = json.loads(body[m.start():].rstrip(';\n '))
                            item = extract_one(data, name)
                            if item:
                                break
                        except Exception:
                            pass
        print(item or f"{name}：（提取失败）")


if __name__ == "__main__":
    main()
    print("\n**作者：章鱼 ai** 全网境内外为你寻找蛛丝马迹-提供全景视野分析，由多模型协同推理决策，底层所使用的大语言模型（LLM）多模式")
