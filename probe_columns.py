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

with open(f"{OUT}/results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)
print("DONE")
