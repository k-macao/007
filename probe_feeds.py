#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性线上探活（验证后删除）：在 Actions 真实网络里逐源跑修复后的端点，
结果写 probe_out/ 由工作流提交回分支供沙箱侧检视。"""

import json
import os
from datetime import datetime, timezone

import pushplus_deepseek as pp

TARGETS = [
    "华尔街见闻 快讯", "华尔街见闻 最新", "华尔街见闻 最热",
    "财联社 电报", "财联社 深度", "财联社 热门",
    "格隆汇 事件", "法布财经 快讯", "法布财经 头条",
]

OUT = "probe_out"
os.makedirs(OUT, exist_ok=True)
now = datetime.now(timezone.utc)
results = {"probed_at": now.isoformat(), "feeds": {}, "scan": {}}


def snippet(body: str, markers: list[str]) -> str:
    parts = [f"len={len(body)}"]
    for m in markers:
        i = body.find(m)
        parts.append(f"\n===== marker {m!r} at {i} =====\n")
        if i >= 0:
            parts.append(body[max(0, i - 400): i + 2600])
    return "\n".join(parts)


for spec in pp.FEED_SPECS:
    if spec.name not in TARGETS:
        continue
    try:
        items = pp.fetch_feed_json(spec, 48, 20, now)
        results["feeds"][spec.name] = {
            "ok": True, "count": len(items),
            "titles": [i.title[:60] for i in items[:3]],
            "ages_h": [i.age_h for i in items[:3]],
            "urls": [i.url for i in items[:3]],
        }
        print(f"OK  {spec.name}: {len(items)} 条")
    except Exception as e:  # noqa: BLE001
        results["feeds"][spec.name] = {"ok": False, "error": str(e)[:200]}
        print(f"FAIL {spec.name}: {e}")
        if spec.kind in ("fb_express", "fb_news"):
            try:
                status, body = pp.http_request(
                    spec.urls[0], headers=spec.headers or None, timeout=20)
                fn = f"{OUT}/{'fb_express' if spec.kind == 'fb_express' else 'fb_news'}_snippet.txt"
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(f"HTTP {status}\n")
                    f.write(snippet(body, ["news-list", "title_name",
                                           "trending_type", "data-date"]))
                results["feeds"][spec.name]["snippet_file"] = fn
                results["feeds"][spec.name]["http_status"] = status
            except Exception as e2:  # noqa: BLE001
                results["feeds"][spec.name]["snippet_error"] = str(e2)[:200]

# stock_news_scan 的三个同名源也一并验证
try:
    import stock_news_scan as scan
    for name, fn in [("财联社 电报(scan)", scan.fetch_cls),
                     ("华尔街见闻 快讯(scan)", scan.fetch_wscn),
                     ("格隆汇 事件(scan)", scan.fetch_gelonghui)]:
        try:
            rows = fn(20)
            results["scan"][name] = {
                "ok": True, "count": len(rows),
                "titles": [r["title"][:60] for r in rows[:3]],
                "has_ts": [r["ts"] is not None for r in rows[:3]],
            }
            print(f"OK  {name}: {len(rows)} 条")
        except Exception as e:  # noqa: BLE001
            results["scan"][name] = {"ok": False, "error": str(e)[:200]}
            print(f"FAIL {name}: {e}")
except Exception as e:  # noqa: BLE001
    results["scan"]["_import"] = {"ok": False, "error": str(e)[:200]}

with open(f"{OUT}/results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)

all_ok = all(v.get("ok") for v in results["feeds"].values())
print("ALL_OK" if all_ok else "SOME_FAILED")
