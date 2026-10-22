#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
source_check_db.py — 数据源检查验证数据库

对全部行情/快讯/热榜数据源做「解析校验 + 结果落库」，用 SQLite 持久化
每次检查的运行记录，便于追踪某个源何时开始报错、报错率与延迟趋势。

覆盖范围：
 - 离线模式（默认，不触网）：社媒热榜 10 源（newsnow_sources.SAMPLES
   注册表内的离线样本 → 解析器 → 字段级校验规则）+ 境外行情 4 源
   （us_quote.US_PARSERS 固定样本 → 字段校验，含延迟源标注规则）。
 - 在线模式（--live 真抓取）：社媒热榜 10 源 + 境外行情 4 源
   + 财经快讯 7 源（stock_news_scan 的 Google/财联社/华尔街见闻/格隆汇/
   金十/MKTNews/雪球，任一失败只记一行 fail，不中断其余源）。

状态口径：ok = 通过；warn = 有校验违例或结果为空；fail = 抓取/解析异常。

用法：
    python source_check_db.py                     # 离线校验全部源并落库
    python source_check_db.py --live              # 真抓取校验并落库
    python source_check_db.py --history toutiao   # 查看单源历史记录
    python source_check_db.py --report            # 打印最近一次运行报告
    python source_check_db.py --selftest          # 内存库自检（不落盘）
    python source_check_db.py --db my.db --live   # 指定数据库文件

环境变量 SOURCE_CHECK_DB 可覆盖默认库文件（默认 ./source_check.db）。
配置说明详见 DATA_SOURCES.md。仅依赖标准库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

UTC = timezone.utc
DB_VERSION = 1
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "source_check.db")

# ---------------- 数据结构 ----------------


@dataclass
class CheckResult:
    source_key: str
    source_name: str
    group_name: str                     # 社媒热榜 / 财经快讯
    status: str = "ok"                  # ok / warn / fail
    items_count: int = 0
    latency_ms: int = 0
    error: str = ""
    violations: list[str] = field(default_factory=list)


# ---------------- 数据库 ----------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS check_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    total       INTEGER DEFAULT 0,
    ok          INTEGER DEFAULT 0,
    warn        INTEGER DEFAULT 0,
    fail        INTEGER DEFAULT 0,
    db_version  INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS source_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES check_runs(run_id),
    source_key      TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    group_name      TEXT NOT NULL,
    status          TEXT NOT NULL,
    items_count     INTEGER DEFAULT 0,
    violations_count INTEGER DEFAULT 0,
    latency_ms      INTEGER DEFAULT 0,
    error           TEXT DEFAULT '',
    detail          TEXT DEFAULT '',
    checked_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_key_time
    ON source_checks(source_key, checked_at);
CREATE INDEX IF NOT EXISTS idx_checks_run
    ON source_checks(run_id);
"""


def default_db_path() -> str:
    return os.environ.get("SOURCE_CHECK_DB") or DEFAULT_DB


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def begin_run(con: sqlite3.Connection, mode: str) -> int:
    cur = con.execute(
        "INSERT INTO check_runs (mode, started_at, db_version) VALUES (?,?,?)",
        (mode, _now_iso(), DB_VERSION))
    con.commit()
    return int(cur.lastrowid)


def record_check(con: sqlite3.Connection, run_id: int, r: CheckResult) -> None:
    con.execute(
        "INSERT INTO source_checks (run_id, source_key, source_name, group_name,"
        " status, items_count, violations_count, latency_ms, error, detail,"
        " checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, r.source_key, r.source_name, r.group_name, r.status,
         r.items_count, len(r.violations), r.latency_ms, r.error,
         json.dumps(r.violations, ensure_ascii=False), _now_iso()))
    con.commit()


def finish_run(con: sqlite3.Connection, run_id: int,
               results: list[CheckResult]) -> None:
    ok = sum(1 for r in results if r.status == "ok")
    warn = sum(1 for r in results if r.status == "warn")
    fail = sum(1 for r in results if r.status == "fail")
    con.execute(
        "UPDATE check_runs SET finished_at=?, total=?, ok=?, warn=?, fail=?"
        " WHERE run_id=?",
        (_now_iso(), len(results), ok, warn, fail, run_id))
    con.commit()


# ---------------- 校验规则 ----------------

def _title_url(item) -> tuple[str, str]:
    """兼容 HotItem / ScanItem / dict 三种条目形态。"""
    if isinstance(item, dict):
        return (str(item.get("title") or "").strip(),
                str(item.get("url") or "").strip())
    return (str(getattr(item, "title", "") or "").strip(),
            str(getattr(item, "url", "") or "").strip())


def validate_items(items: list) -> list[str]:
    """字段级校验规则。返回违例描述列表（空 = 全通过）。"""
    violations: list[str] = []
    if not items:
        return ["结果为空：未返回任何条目"]
    if len(items) > 50:
        violations.append(f"条目数量异常偏多：{len(items)} 条（>50）")
    no_title = no_url = bad_url = dup = 0
    seen: set[str] = set()
    for it in items:
        title, url = _title_url(it)
        if not title or len(title) < 2:
            no_title += 1
        if len(title) > 120:
            no_title += 1  # 计入标题异常
        if not url:
            no_url += 1
        elif not url.startswith(("http://", "https://")):
            bad_url += 1
        if title in seen:
            dup += 1
        else:
            seen.add(title)
    if no_title:
        violations.append(f"标题缺失/长度异常 {no_title} 条")
    if no_url:
        violations.append(f"链接缺失 {no_url} 条")
    if bad_url:
        violations.append(f"链接非 http(s) {bad_url} 条")
    if dup and dup / max(len(items), 1) > 0.3:
        violations.append(f"重复标题 {dup} 条（超 30%）")
    return violations


# ---------------- 检查执行 ----------------

def check_one(key: str, name: str, group: str,
              produce: Callable[[], list]) -> CheckResult:
    """执行单个源检查：produce() 抛出异常记 fail，违例记 warn。"""
    r = CheckResult(source_key=key, source_name=name, group_name=group)
    t0 = time.monotonic()
    try:
        items = produce()
        r.items_count = len(items)
        r.violations = validate_items(items)
        r.status = "warn" if r.violations else "ok"
    except Exception as e:  # noqa: BLE001 —— 逐源隔离，失败也要落库
        r.status = "fail"
        r.error = str(e)[:200]
    r.latency_ms = int((time.monotonic() - t0) * 1000)
    return r


def run_checks(con: sqlite3.Connection, live: bool = False, timeout: int = 12,
               only: list[str] | None = None) -> tuple[int, list[CheckResult]]:
    """跑一次完整检查并落库。返回 (run_id, results)。

    live=False：离线模式，用 newsnow_sources.SAMPLES 样本走解析器；
    live=True ：真抓取社媒 10 源 + 财经 7 源（stock_news_scan 可导入时）。
    """
    from newsnow_sources import FETCHERS, SAMPLES, TARGET_SOURCES

    run_id = begin_run(con, "live" if live else "offline")
    results: list[CheckResult] = []

    # ---- 社媒热榜（NewsNow 移植源）----
    for key in TARGET_SOURCES:
        if only and key not in only:
            continue
        name, fetch_fn = FETCHERS[key]
        if live:
            produce = lambda fn=fetch_fn: fn(timeout=timeout)
        else:
            parse_fn, sample = SAMPLES[key]
            produce = lambda fn=parse_fn, s=sample: fn(s)
        r = check_one(key, name, "社媒热榜", produce)
        results.append(r)
        record_check(con, run_id, r)

    # ---- 财经快讯 7 源（仅在线模式；离线样本结构依赖股票查询上下文）----
    if live:
        try:
            from datetime import datetime as _dt
            import stock_news_scan as scan

            finance_jobs: list[tuple[str, str, Callable[[], list]]] = []
            for platform, fn in scan.FINANCE_FETCHERS.items():
                if platform in ("公开新闻检索", "Google新闻"):
                    finance_jobs.append(
                        ("google", platform,
                         lambda fn=fn: fn("股票", timeout)))
                else:
                    finance_jobs.append(
                        (platform.split()[0].lower(), platform,
                         lambda fn=fn: fn(timeout)))
            q = scan.build_stock_query("09988", "阿里巴巴")
            finance_jobs.append(
                ("xueqiu", "投资社区热榜",
                 lambda: scan.fetch_xueqiu(q, timeout, _dt.now(scan.UTC))))
            for key, name, produce in finance_jobs:
                if only and key not in only:
                    continue
                r = check_one(key, name, "财经快讯", produce)
                results.append(r)
                record_check(con, run_id, r)
        except ImportError as e:
            r = CheckResult(source_key="finance", source_name="财经快讯组",
                            group_name="财经快讯", status="fail",
                            error=f"stock_news_scan 不可导入：{e}")
            results.append(r)
            record_check(con, run_id, r)

    # ---- 境外（美股）行情 4 源（us_quote，离线样本/在线实抓）----
    try:
        import us_quote
        us_offline = [("us_tencent", "美股行情主源", "主源样本"),
                      ("us_eastmoney", "美股行情备源", "备源样本"),
                      ("us_yahoo", "国际行情核验源", "核验样本"),
                      ("us_stooq", "延迟核验源", "延迟样本")]
        for key, name, _tag in us_offline:
            if only and key not in only and key.removeprefix("us_") not in only:
                continue
            if live:
                fn = us_quote.US_FETCHERS[key.removeprefix("us_")]
                produce = lambda fn=fn: fn("AAPL")
            else:
                parse_fn, sample, code = us_quote.US_PARSERS[key]
                produce = lambda fn=parse_fn, smp=sample, c=code: fn(smp, c)
            t0 = time.monotonic()
            r = CheckResult(source_key=key, source_name=name,
                            group_name="境外行情")
            try:
                q = produce()
                r.items_count = 1 if q else 0
                r.violations = us_quote.validate_us_quote(q)
                r.status = "warn" if r.violations else "ok"
            except Exception as e:  # noqa: BLE001
                r.status = "fail"
                r.error = str(e)[:200]
            r.latency_ms = int((time.monotonic() - t0) * 1000)
            results.append(r)
            record_check(con, run_id, r)
    except ImportError as e:
        r = CheckResult(source_key="us_quote", source_name="境外行情组",
                        group_name="境外行情", status="fail",
                        error=f"us_quote 不可导入：{e}")
        results.append(r)
        record_check(con, run_id, r)

    # ---- AI 智能检索 1 源 ----
    try:
        import deepseek_search
        key = "deepseek"
        name = "AI 智能检索"
        if only and key not in only and "deepseek_search" not in only:
            pass
        else:
            if live:
                def _produce_ds():
                    return deepseek_search.fetch_deepseek_search("阿里巴巴 09988", timeout=timeout, hours=48)
                r = check_one(key, name, "AI搜索", _produce_ds)
            else:
                def _produce_ds_offline():
                    import json as _json
                    content = _json.loads(deepseek_search.DEEPSEEK_SAMPLE_JSON)["choices"][0]["message"]["content"]
                    return deepseek_search.parse_deepseek_response(content)
                r = check_one(key, name, "AI搜索", _produce_ds_offline)
            results.append(r)
            record_check(con, run_id, r)
    except ImportError as e:
        r = CheckResult(source_key="deepseek", source_name="AI 智能检索",
                        group_name="AI搜索", status="fail",
                        error=f"deepseek_search 不可导入：{e}")
        results.append(r)
        record_check(con, run_id, r)

    finish_run(con, run_id, results)
    return run_id, results


# ---------------- 报告 ----------------

def latest_report(con: sqlite3.Connection) -> dict:
    """取最近一次运行的摘要 + 明细。"""
    row = con.execute(
        "SELECT run_id, mode, started_at, finished_at, total, ok, warn, fail"
        " FROM check_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if not row:
        return {"run": None, "checks": []}
    cols = ["run_id", "mode", "started_at", "finished_at", "total",
            "ok", "warn", "fail"]
    run = dict(zip(cols, row))
    checks = con.execute(
        "SELECT source_key, source_name, group_name, status, items_count,"
        " violations_count, latency_ms, error, detail, checked_at"
        " FROM source_checks WHERE run_id=? ORDER BY id", (run["run_id"],)
    ).fetchall()
    keys = ["source_key", "source_name", "group_name", "status", "items_count",
            "violations_count", "latency_ms", "error", "detail", "checked_at"]
    return {"run": run, "checks": [dict(zip(keys, c)) for c in checks]}


def source_history(con: sqlite3.Connection, key_or_name: str,
                   limit: int = 20) -> list[dict]:
    rows = con.execute(
        "SELECT run_id, source_key, source_name, status, items_count,"
        " violations_count, latency_ms, error, checked_at"
        " FROM source_checks WHERE source_key=? OR source_name=?"
        " ORDER BY id DESC LIMIT ?",
        (key_or_name, key_or_name, limit)).fetchall()
    keys = ["run_id", "source_key", "source_name", "status", "items_count",
            "violations_count", "latency_ms", "error", "checked_at"]
    return [dict(zip(keys, r)) for r in rows]


_STATUS_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌"}


def render_report_md(report: dict) -> str:
    """最近一次运行的 Markdown 报告。"""
    run = report.get("run")
    if not run:
        return "（数据库中暂无检查记录，先运行 `python source_check_db.py`）"
    lines = [
        f"🩺 **数据源检查验证报告**（run #{run['run_id']} · "
        f"{'在线抓取' if run['mode'] == 'live' else '离线样本'}）",
        "",
        f"- 时间：{run['started_at']} → {run.get('finished_at') or '—'}",
        f"- 结论：✅ {run['ok']} / ⚠️ {run['warn']} / ❌ {run['fail']}"
        f"（共 {run['total']} 源）",
        "",
        "| 组 | 源 | 状态 | 条目 | 延迟ms | 违例 | 备注 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in report.get("checks", []):
        note = c.get("error") or ""
        detail = c.get("detail") or ""
        if not note and detail:
            try:
                note = "；".join(json.loads(detail))
            except Exception:
                note = detail
        note = (note[:40] + "…") if len(note) > 41 else note
        note = note.replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {c['group_name']} | {c['source_name']} "
            f"| {_STATUS_ICON.get(c['status'], '❓')} {c['status']} "
            f"| {c['items_count']} | {c['latency_ms']} "
            f"| {c['violations_count']} | {note} |")
    lines += ["", "> 校验历史见 SQLite 库（source_check.db 的 source_checks 表）；",
              "> 单源历史：`python source_check_db.py --history <源键名>`。"]
    return "\n".join(lines)


# ---------------- 自检（内存库，不落盘） ----------------

def selftest_checkdb() -> int:
    fails = 0

    def check(name: str, cond: bool):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("🩺 数据源检查验证数据库（内存库自检，不落盘不触网）")
    con = connect(":memory:")

    # 1) 规则引擎
    from newsnow_sources import HotItem
    good = [HotItem(source="t", id="1", title="正常新闻标题甲", url="https://a.com/1")]
    check("正常条目不违例", validate_items(good) == [])
    check("空结果被拦截", validate_items([]) != [])
    bad = [{"title": "", "url": "ftp://x"}, {"title": "重复标题一二三四",
            "url": ""}, {"title": "重复标题一二三四", "url": "https://a.com/2"}]
    v = validate_items(bad)
    check("缺标题/坏链接/缺链接被拦截",
          any("标题" in x for x in v) and any("非 http" in x for x in v)
          and any("缺失" in x for x in v))

    # 2) 离线跑检 + 落库 + 报告（10 社媒 + 4 境外 + 1 DeepSeek = 15）
    run_id, results = run_checks(con, live=False)
    check("覆盖社媒热榜 10 源 + 境外行情 4 源 + DeepSeek 1 源", len(results) == 15
          and {r.source_key for r in results} >=
          {"zhihu", "toutiao", "baidu", "bilibili",
           "us_tencent", "us_eastmoney", "us_yahoo", "us_stooq",
           "deepseek"})
    check("样本解析全部 ok 且有条目",
          all(r.status == "ok" and r.items_count >= 1 for r in results))
    n_rows = con.execute(
        "SELECT COUNT(*) FROM source_checks WHERE run_id=?",
        (run_id,)).fetchone()[0]
    check("落库行数=源数", n_rows == len(results))
    run_row = con.execute(
        "SELECT total, ok, fail FROM check_runs WHERE run_id=?",
        (run_id,)).fetchone()
    check("运行台账汇总正确",
          run_row == (len(results), len(results), 0))

    # 3) 失败与告警路径
    r_fail = check_one("broken", "坏源", "社媒热榜",
                       lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    check("抓取异常记 fail 且带错误",
          r_fail.status == "fail" and "boom" in r_fail.error)
    record_check(con, run_id, r_fail)
    r_warn = check_one("empty", "空源", "社媒热榜", lambda: [])
    check("空结果记 warn", r_warn.status == "warn"
          and "结果为空" in r_warn.violations[0])

    # 4) 报告与历史
    md = render_report_md(latest_report(con))
    check("报告含全部新三源 + AI 智能检索",
          all(s in md for s in ("综合资讯热榜", "搜索实时热点", "视频社区热榜", "AI 智能检索"))
          and "run #" in md)
    hist = source_history(con, "toutiao")
    check("单源历史可查询",
          len(hist) == 1 and hist[0]["status"] == "ok")
    con.close()

    print(f"\n{'✅ 检查验证数据库自检通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


# ---------------- CLI ----------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="数据源检查验证数据库：解析校验 + 结果落盘（SQLite）")
    p.add_argument("--live", action="store_true",
                   help="真抓取校验（默认离线样本校验，不触网）")
    p.add_argument("--report", action="store_true",
                   help="只打印最近一次运行报告，不重新检查")
    p.add_argument("--history", metavar="KEY_OR_NAME", default="",
                   help="打印单个源的历史检查记录")
    p.add_argument("--db", default="", help="数据库文件路径"
                   "（默认 SOURCE_CHECK_DB 环境变量或 ./source_check.db）")
    p.add_argument("--timeout", type=int, default=12, help="单源抓取超时秒数")
    p.add_argument("--only", default="", help="只检查这些源键名（逗号分隔）")
    p.add_argument("--selftest", action="store_true", help="内存库自检后退出")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest_checkdb()

    db_path = args.db or default_db_path()
    con = connect(db_path)

    if args.history:
        rows = source_history(con, args.history)
        if not rows:
            print(f"（无「{args.history}」的历史记录）")
            con.close()
            return 1
        print(f"📜 {args.history} 最近 {len(rows)} 条检查记录：")
        for r in rows:
            icon = _STATUS_ICON.get(r["status"], "❓")
            print(f"  {icon} run#{r['run_id']} {r['checked_at']} "
                  f"{r['status']} 条目{r['items_count']} "
                  f"违例{r['violations_count']} {r['latency_ms']}ms "
                  f"{r['error']}")
        con.close()
        return 0

    if not args.report:
        only = [s.strip() for s in args.only.replace("，", ",").split(",")
                if s.strip()] or None
        run_id, results = run_checks(con, live=args.live,
                                     timeout=args.timeout, only=only)
        ok = sum(1 for r in results if r.status == "ok")
        print(f"🩺 检查完成 run#{run_id}（{'在线' if args.live else '离线'}）："
              f"✅{ok} ⚠️{sum(1 for r in results if r.status == 'warn')} "
              f"❌{sum(1 for r in results if r.status == 'fail')} "
              f"/ 共{len(results)}源 → 已写入 {os.path.basename(db_path)}\n")

    print(render_report_md(latest_report(con)))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
