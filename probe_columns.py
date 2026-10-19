#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性线上探活（验证后删除）：金十 50 条 + 香港券商/国外投行栏目真实采集。"""

import json
import os
import time
from datetime import datetime, timezone

OUT = "probe_out"
os.makedirs(OUT, exist_ok=True)
results = {"probed_at": datetime.now(timezone.utc).isoformat()}

# 1) 金十 ≤50
try:
    import pushplus_deepseek as pp
    spec = next(s for s in pp.FEED_SPECS if s.name == "金十数据")
    t0 = time.monotonic()
    items = pp.fetch_feed_json(spec, 48, 20, datetime.now(timezone.utc))
    results["jin10"] = {
        "ok": True, "limit": spec.limit, "count": len(items),
        "elapsed_s": round(time.monotonic() - t0, 1),
        "titles": [i.title[:50] for i in items[:3]],
        "ages_h": [i.age_h for i in (items[0], items[-1])] if items else [],
    }
    print(f"OK jin10: {len(items)}/{spec.limit}")
except Exception as e:  # noqa: BLE001
    results["jin10"] = {"ok": False, "error": str(e)[:200]}
    print(f"FAIL jin10: {e}")

# 2) 香港券商栏目
try:
    import hk_brokers as hb
    t0 = time.monotonic()
    pack = hb.collect_hk_brokers(topic="港股市场", hours=48, timeout=12)
    results["hk_brokers"] = {
        "ok": pack.agg.get("n", 0) > 0, "agg": pack.agg,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "per_firm": {k: [i.title[:50] for i in v[:2]]
                     for k, v in pack.reports.items()},
        "quotes": pack.quotes,
        "errors": pack.errors[:14],
        "appendix_head": hb.render_hk_brokers_appendix(
            pack, max_per_firm=2)[:900],
    }
    print(f"OK hk_brokers: {pack.agg}")
except Exception as e:  # noqa: BLE001
    results["hk_brokers"] = {"ok": False, "error": str(e)[:300]}
    print(f"FAIL hk_brokers: {e}")

# 3) 国外投行研报（72h，简报并入口径）
try:
    import foreign_institution_weekly as fiw
    t0 = time.monotonic()
    pack = fiw.collect_foreign_weekly(topic="全球市场", hours=72, timeout=12)
    results["foreign_weekly_72h"] = {
        "ok": pack.agg.get("n", 0) > 0, "agg": pack.agg,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "per_inst": {k: [i.title[:50] for i in v[:2]]
                     for k, v in pack.reports.items()},
        "errors": pack.errors[:14],
    }
    print(f"OK foreign(72h): {pack.agg}")
except Exception as e:  # noqa: BLE001
    results["foreign_weekly_72h"] = {"ok": False, "error": str(e)[:300]}
    print(f"FAIL foreign(72h): {e}")

# 4) 搜索源横评：Google/Bing/Yahoo RSS + GDELT + DeepSeek 原始返回
try:
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET

    def probe_get(url, ua="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"):
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")

    def rss_items(xml_text):
        root = ET.fromstring(xml_text)
        out = []
        for it in root.iter("item"):
            t = (it.findtext("title") or "").strip()
            d = (it.findtext("pubDate") or "").strip()
            if t:
                out.append(f"{d[:22]} | {t[:60]}")
        return out

    from stock_news_scan import GOOGLE_RSS as G_TPL
    engines = {}
    QUERIES = ["中金公司 研报", "高盛 评级 目标价"]
    for query in QUERIES:
        q = urllib.parse.quote_plus(query)
        tests = {
            "google_when7d": G_TPL.format(q=urllib.parse.quote_plus(query + " when:7d")),
            "google_plain": G_TPL.format(q=q),
            "bing_rss": f"https://www.bing.com/news/search?q={q}&format=rss",
            "yahoo_rss": f"https://news.search.yahoo.com/rss?p={q}",
            "gdelt": ("https://api.gdeltproject.org/api/v2/doc/doc"
                      f"?query={q}&mode=artlist&maxrecords=10&format=json&timespan=3d"),
        }
        for name, url in tests.items():
            key = f"{name}|{query}"
            try:
                st, body = probe_get(url)
                if name == "gdelt":
                    arts = (json.loads(body).get("articles") or [])
                    engines[key] = {"status": st, "count": len(arts),
                                    "samples": [f"{a.get('seendate','')[:12]} | "
                                                f"{(a.get('title') or '')[:60]} | "
                                                f"{a.get('domain','')}"
                                                for a in arts[:3]]}
                else:
                    items = rss_items(body)
                    engines[key] = {"status": st, "count": len(items),
                                    "samples": items[:3]}
            except Exception as e:  # noqa: BLE001
                engines[key] = {"error": str(e)[:160]}
    # DeepSeek 原始（huge window 看返回的日期真面目）
    try:
        import deepseek_search
        rows = deepseek_search.fetch_deepseek_search(
            "中金公司 研报 目标价", timeout=30, hours=24 * 3650)
        engines["deepseek_raw"] = {"count": len(rows),
                                   "samples": [f"{r.get('ts')} | "
                                               f"{(r.get('title') or '')[:55]} | "
                                               f"{r.get('url','')[:60]}"
                                               for r in rows[:5]]}
    except Exception as e:  # noqa: BLE001
        engines["deepseek_raw"] = {"error": str(e)[:200]}
    results["search_engines"] = engines
    print("OK engines:", {k: v.get("count", v.get("error", "?")[:30])
                          for k, v in engines.items()})
except Exception as e:  # noqa: BLE001
    results["search_engines"] = {"error": str(e)[:200]}
    print(f"FAIL engines: {e}")

with open(f"{OUT}/results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)
print("DONE")
