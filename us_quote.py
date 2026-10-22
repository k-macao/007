#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
us_quote.py — 境外（美股）实时行情采集 + 四源交叉验证（免 Key，纯标准库）

与 hk_quote.py（港股/A股 主备链路）互补：本模块面向美股等境外标的，
采集 **4 个免费源并做交叉验证**——不只取首个成功的源，而是汇总所有
可用源报价，给出中位共识价、逐源偏离度与一致性结论。

============================================================================
 境外（美股）数据源一览（2026-08-14 设计，样本见 FIXTURES）
============================================================================
 ① 腾讯财经美股 qt.gtimg.cn/q=us{CODE}        GBK 管道串   主源（字段最全）
 ② 东方财富美股 push2 secid=105/106/107.{CODE} JSON        备源（自动试交易所）
 ③ Yahoo Finance chart/{CODE}                 JSON        核验（多 host 容灾）
 ④ Stooq stooq.com/q/l/?s={code}.us           CSV         延迟源（≥15min，仅核验/兜底）

用法：
  python us_quote.py NVDA              # 取首个成功源 + 交叉验证结论
  python us_quote.py AAPL --verify     # 逐源报价表 + 交叉验证 Markdown
  python us_quote.py --selftest        # 离线样本自检（不触网）
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from statistics import median

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 6.0                    # 单次请求超时（秒）；美股源较多，略放宽
CACHE_TTL = 10                   # 正缓存（秒）
NEG_CACHE_TTL = 60               # 失败负缓存（秒）

import os
def _env_float_us(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

# 文件缓存：避免数据丢失（与 hk_quote.py 同策略）
FILE_CACHE_DIR = os.environ.get(
    "US_QUOTE_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "quote_cache"))
FILE_CACHE_ENABLED = os.environ.get("US_QUOTE_FILE_CACHE", "1") not in ("", "0", "false", "False")
FILE_CACHE_TTL = _env_float_us("US_QUOTE_FILE_TTL", 3600)

def _save_file_cache_us(code: str, quote: dict) -> None:
    if not FILE_CACHE_ENABLED:
        return
    try:
        os.makedirs(FILE_CACHE_DIR, exist_ok=True)
        path = os.path.join(FILE_CACHE_DIR, f"us_{code}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(quote, f, ensure_ascii=False)
    except Exception:
        pass

def _load_file_cache_us(code: str) -> dict | None:
    if not FILE_CACHE_ENABLED:
        return None
    try:
        path = os.path.join(FILE_CACHE_DIR, f"us_{code}.json")
        if not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        age = time.time() - mtime
        with open(path, "r", encoding="utf-8") as f:
            q = json.load(f)
        q = dict(q)
        q["stale"] = age > FILE_CACHE_TTL
        q["stale_age_s"] = int(age)
        q["source_label"] = generalize_source(q.get("source_label") or q.get("source") or "") + (" (文件缓存)" if not q.get("stale") else " (文件缓存-过期)")
        q["from_file_cache"] = True
        return q
    except Exception:
        return None

EM_US_PRICE_SCALE = 1000         # 东财美股价格为实际价格×1000（3 位小数）
EM_US_MARKET_IDS = ("105", "106", "107")   # 105=NASDAQ 106=NYSE 107=AMEX，未知交易所逐个尝试
EM_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

from source_names import generalize_source, sanitize_error

# 交叉验证阈值：±0.8% 内视为一致（覆盖汇率/微小延迟抖动），±2% 内基本一致
CROSS_TOL_OK = 0.008
CROSS_TOL_LOOSE = 0.02

US_SOURCES = {
    "tencent":   {"label": "美股行情主源",
                  "url_tpl": "https://qt.gtimg.cn/q=us{code}", "encoding": "gbk"},
    "eastmoney": {"label": "美股行情备源",
                  "url_tpl": ("https://push2.eastmoney.com/api/qt/stock/get"
                              "?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f57,"
                              "f58,f60,f107,f116,f117,f152,f169,f170,f171"),
                  "encoding": "utf-8"},
    "yahoo":     {"label": "国际行情核验源",
                  "url_tpl": ("https://query1.finance.yahoo.com/v8/finance/chart/"
                              "{code}?interval=1d&range=5d"), "encoding": "utf-8"},
    "stooq":     {"label": "延迟核验源",
                  "url_tpl": "https://stooq.com/q/l/?s={lower}.us&f=sd2t2ohlcv&h&e=csv",
                  "encoding": "utf-8"},
}
DEFAULT_US_CHAIN = ("tencent", "eastmoney", "yahoo")   # stooq 默认仅交叉验证/兜底
ALL_US_SOURCES = ("tencent", "eastmoney", "yahoo", "stooq")


def now_str() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _http_get(url: str, encoding: str = "utf-8", timeout: float = TIMEOUT,
              headers: dict | None = None, retries: int = 2) -> tuple[str, bytes]:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", UA)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode(encoding, "replace"), raw
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.4 * (2 ** attempt))
                continue
            raise
    raise last_exc  # type: ignore


def _fnum(parts, idx, scale=1.0):
    try:
        v = parts[idx].strip()
        if v in ("", "0", "-", "N/D"):
            return None
        return float(v) / scale
    except (IndexError, ValueError):
        return None


def _fmt_vol(shares) -> str:
    if shares is None:
        return "—"
    if shares >= 1e8:
        return f"{shares / 1e8:.2f}亿"
    if shares >= 1e4:
        return f"{shares / 1e4:.2f}万"
    return f"{shares:.0f}"


# ================================================================ 代码识别

def detect_us_code(raw: str) -> tuple[str, str] | None:
    """识别美股代码。返回 (统一大写代码, 东财 secid 猜测值) 或 None。

    支持：NVDA / nvda / NVDA.US / us:NVDA / NASDAQ:NVDA / NVDA.OQ。
    纯数字（港股/A股）返回 None，交给 hk_quote 处理。
    """
    s = str(raw or "").strip().upper()
    m = re.fullmatch(
        r"(?:(?:US|NASDAQ|NYSE|AMEX)[:\s])?\s*([A-Z]{1,6})"
        r"(?:\.(US|OQ|N|UN|UQ|NASDAQ|NYSE|AMEX))?", s)
    if not m:
        return None
    code = m.group(1)
    return code, f"105.{code}"          # secid 仅为占位猜测，抓取时自动试 105/106/107


# ================================================================ 四源解析

def parse_tencent_us(text: str, code: str) -> dict | None:
    """解析 qt.gtimg.cn 的 v_us{CODE}="..." 美股管道串（字段布局同港股系）。"""
    m = re.search(r'v_us[A-Za-z]{1,6}[^"]*="([^"]*)', text)
    if not m:
        return None
    p = m.group(1).split("~")
    if len(p) < 45:
        return None
    price = _fnum(p, 3)
    prev = _fnum(p, 4)
    if price is None or price <= 0:
        return None
    chg = _fnum(p, 31)
    pct = _fnum(p, 32)
    if chg is None and prev:
        chg = price - prev
    if pct is None and prev:
        pct = (price - prev) / prev * 100.0
    mcap = _fnum(p, 44)
    return {
        "market": "us", "code": code,
        "name": p[1].strip() or None,
        "price": round(price, 3),
        "prev_close": round(prev, 3) if prev else None,
        "open": round(_fnum(p, 5) or 0, 3) or None,
        "high": round(_fnum(p, 33) or 0, 3) or None,
        "low": round(_fnum(p, 34) or 0, 3) or None,
        "change": round(chg, 3) if chg is not None else None,
        "change_pct": round(pct, 3) if pct is not None else None,
        "volume": _fnum(p, 36),
        "amount": _fnum(p, 37),
        "pe": _fnum(p, 39),
        "pb": None, "turnover_rate": None, "amplitude": None,
        "market_cap": (mcap * 1e8) if mcap else None,
        "float_cap": None,
        "high_52w": _fnum(p, 48), "low_52w": _fnum(p, 49),
        "currency": "USD",
        "time": p[30].strip() or None,
        "source": "tencent", "source_label": US_SOURCES["tencent"]["label"],
        "delayed": False,
    }


def parse_eastmoney_us(obj: dict, code: str) -> dict | None:
    """解析东财美股 push2 JSON（价格 ×1000；secid 105/106/107 由抓取层尝试）。"""
    d = obj.get("data") or {}
    if not d or not d.get("f43"):
        return None
    chg = d.get("f169")
    pct = d.get("f170")

    def _scaled(key, nd=3):
        v = d.get(key)
        return round(v / EM_US_PRICE_SCALE, nd) if v else None

    return {
        "market": "us", "code": code,
        "name": d.get("f58") or None,
        "price": _scaled("f43"),
        "prev_close": _scaled("f60"),
        "open": _scaled("f46"), "high": _scaled("f44"), "low": _scaled("f45"),
        "change": round(chg / EM_US_PRICE_SCALE, 3) if chg is not None else None,
        "change_pct": round(pct / 100.0, 3) if pct is not None else None,
        "volume": d.get("f47"), "amount": d.get("f48"),
        "pe": None, "pb": None,
        "turnover_rate": round(d["f168"] / 100.0, 2) if d.get("f168") else None,
        "amplitude": round(d["f171"] / 100.0, 2) if d.get("f171") else None,
        "market_cap": d.get("f116"), "float_cap": d.get("f117"),
        "high_52w": None, "low_52w": None,
        "currency": "USD", "time": None,
        "source": "eastmoney", "source_label": US_SOURCES["eastmoney"]["label"],
        "delayed": False,
    }


def parse_yahoo_us(obj: dict, code: str) -> dict | None:
    """解析 Yahoo chart API（meta 区即含最新价；美股无需 .HK 后缀）。"""
    result = (obj.get("chart") or {}).get("result") or []
    if not result:
        return None
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    closes = (result[0].get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    prev = closes[-2] if len(closes) >= 2 else None
    chg = (price - prev) if prev else None
    return {
        "market": "us", "code": code,
        "name": meta.get("longName") or meta.get("shortName"),
        "price": round(price, 3),
        "prev_close": round(prev, 3) if prev else None,
        "open": None,
        "high": round(meta["regularMarketDayHigh"], 3)
        if meta.get("regularMarketDayHigh") else None,
        "low": round(meta["regularMarketDayLow"], 3)
        if meta.get("regularMarketDayLow") else None,
        "change": round(chg, 3) if chg is not None else None,
        "change_pct": round(chg / prev * 100.0, 3) if prev else None,
        "volume": meta.get("regularMarketVolume"), "amount": None,
        "pe": None, "pb": None, "turnover_rate": None, "amplitude": None,
        "market_cap": None, "float_cap": None,
        "high_52w": meta.get("fiftyTwoWeekHigh"),
        "low_52w": meta.get("fiftyTwoWeekLow"),
        "currency": meta.get("currency") or "USD",
        "time": (datetime.fromtimestamp(meta["regularMarketTime"], tz=timezone.utc)
                 .astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                 if meta.get("regularMarketTime") else None),
        "source": "yahoo", "source_label": US_SOURCES["yahoo"]["label"],
        "delayed": False,
    }


_STOOQ_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_stooq(text: str, code: str) -> dict | None:
    """解析 Stooq CSV（Symbol,Date,Time,Open,High,Low,Close,Volume；延迟>=15min）。"""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2 or not lines[0].lower().startswith("symbol"):
        return None
    p = lines[1].split(",")
    if len(p) < 8:
        return None
    close_s = p[6].strip().upper()
    if close_s in ("N/D", ""):
        return None
    try:
        close = float(close_s)
    except ValueError:
        return None
    if close <= 0:
        return None

    def _num(i):
        try:
            v = p[i].strip()
            return None if v.upper() == "N/D" else float(v)
        except (ValueError, IndexError):
            return None

    ts = f"{p[1].strip()} {p[2].strip()}".strip()
    return {
        "market": "us", "code": code, "name": None,
        "price": round(close, 3), "prev_close": None,
        "open": _num(3), "high": _num(4), "low": _num(5),
        "change": None, "change_pct": None,
        "volume": _num(7), "amount": None,
        "pe": None, "pb": None, "turnover_rate": None, "amplitude": None,
        "market_cap": None, "float_cap": None, "high_52w": None, "low_52w": None,
        "currency": "USD",
        "time": ts or None,
        "source": "stooq", "source_label": US_SOURCES["stooq"]["label"],
        "delayed": True,
    }


# ================================================================ 四源抓取

def fetch_tencent_us(code: str) -> dict:
    text, _ = _http_get(US_SOURCES["tencent"]["url_tpl"].format(code=code), "gbk")
    q = parse_tencent_us(text, code)
    if q is None:
        raise ValueError(f"腾讯财经(美股)响应无法解析: {text[:100]!r}")
    return q


def fetch_eastmoney_us(code: str) -> dict:
    """交易所未知：105(NASDAQ)/106(NYSE)/107(AMEX) × 主备 host 逐个尝试。"""
    from itertools import product
    tpl = US_SOURCES["eastmoney"]["url_tpl"]
    last_err = None
    for mkt, host in product(EM_US_MARKET_IDS, EM_HOSTS):
        url = tpl.replace("push2.eastmoney.com", host).format(secid=f"{mkt}.{code}")
        try:
            text, _ = _http_get(url, "utf-8",
                                headers={"Referer": "https://quote.eastmoney.com/"})
            obj = json.loads(text)
            q = parse_eastmoney_us(obj, code)
            if q is None:
                continue                      # 非本交易所：data 为空不报错，试下一个
            return q
        except Exception as e:                # noqa: BLE001
            last_err = e
    raise ValueError(f"东方财富(美股)三交易所均无数据: {last_err}")


def fetch_yahoo_us(code: str) -> dict:
    tpl = US_SOURCES["yahoo"]["url_tpl"].format(code=code)
    last_err = None
    for host in YAHOO_HOSTS:
        url = tpl.replace("query1.finance.yahoo.com", host)
        try:
            text, _ = _http_get(url, "utf-8", headers={"Accept": "application/json"})
            obj = json.loads(text)
            q = parse_yahoo_us(obj, code)
            if q is None:
                raise ValueError(f"Yahoo 无数据: {text[:100]!r}")
            return q
        except Exception as e:                # noqa: BLE001
            last_err = e
    raise last_err


def fetch_stooq(code: str) -> dict:
    text, _ = _http_get(US_SOURCES["stooq"]["url_tpl"].format(lower=code.lower()),
                        "utf-8")
    q = parse_stooq(text, code)
    if q is None:
        raise ValueError(f"Stooq 响应无法解析: {text[:100]!r}")
    return q


US_FETCHERS = {
    "tencent": fetch_tencent_us,
    "eastmoney": fetch_eastmoney_us,
    "yahoo": fetch_yahoo_us,
    "stooq": fetch_stooq,
}


# ================================================================ 交叉验证

def collect_us_quotes(code: str) -> dict:
    """拉取全部 4 源（能拿多少拿多少），供交叉验证。任何单源失败只记错误。"""
    pack = {"code": code, "quotes": [], "errors": [], "latency_ms": {}}
    for key in ALL_US_SOURCES:
        t0 = time.monotonic()
        try:
            q = US_FETCHERS[key](code)
            q["fetched_at"] = now_str()
            pack["quotes"].append(q)
        except Exception as e:                # noqa: BLE001 —— 逐源隔离
            pack["errors"].append(
                f"{US_SOURCES[key]['label']}：{e.__class__.__name__} {str(e)[:100]}")
        pack["latency_ms"][key] = int((time.monotonic() - t0) * 1000)
    pack["cross"] = cross_validate(pack["quotes"])
    return pack


def cross_validate(quotes: list[dict]) -> dict:
    """汇总多源报价 → 中位共识价 + 偏离度 + 一致性结论。"""
    prices = [q for q in quotes if q and q.get("price")]
    n_total = len(quotes)
    if not prices:
        return {"ok": False, "n_ok": 0, "n_total": n_total, "consensus": None,
                "spread_pct": None, "verdict": "❌ 全部行情源失败", "per_source": [],
                "outliers": []}
    cons = round(median(q["price"] for q in prices), 3)
    per_source = []
    for q in prices:
        dev = abs(q["price"] - cons) / cons if cons else 0.0
        per_source.append({"label": q.get("source_label") or q.get("source"),
                           "price": q["price"],
                           "change_pct": q.get("change_pct"),
                           "time": q.get("time"), "dev_pct": dev,
                           "delayed": bool(q.get("delayed"))})
    spread = max(s["dev_pct"] for s in per_source)
    n_ok = len(prices)
    outliers = [s["label"] for s in per_source if s["dev_pct"] > CROSS_TOL_LOOSE]
    if n_ok == 1:
        verdict = "⚠️ 仅 1 源可用，无法交叉验证"
    elif spread <= CROSS_TOL_OK:
        verdict = f"✅ 一致（{n_ok}/{n_total} 源交叉验证通过，最大偏离 {spread * 100:.2f}%）"
    elif spread <= CROSS_TOL_LOOSE:
        verdict = f"🟡 基本一致（{n_ok}/{n_total} 源，最大偏离 {spread * 100:.2f}%）"
    else:
        verdict = (f"❌ 分歧（最大偏离 {spread * 100:.2f}%；"
                   f"离群源：{'、'.join(outliers) or '—'}）")
    return {"ok": True, "n_ok": n_ok, "n_total": n_total, "consensus": cons,
            "spread_pct": round(spread * 100, 3), "verdict": verdict,
            "per_source": per_source, "outliers": outliers}


# ================================================================ 主入口（首个成功源 + 验证）

_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def fetch_us_quote(code: str, chain: tuple | list | None = None,
                   use_cache: bool = True) -> dict | None:
    """按链路取首个成功源（默认 腾讯→东财→Yahoo），失败负缓存防反复请求。

    返回 dict 附带交叉验证字段：``cross_verdict`` / ``consensus_price`` /
    ``cross_ok``（单源时 consensus==price）。需要完整逐源验证表时用
    :func:`fetch_us_verified`。
    """
    det = detect_us_code(code)
    if not det:
        return None
    code, _secid_guess = det
    chain = tuple(chain or DEFAULT_US_CHAIN) + ("stooq",)   # stooq 兜底
    hit = _cache.get(code)
    now = time.monotonic()
    if use_cache and hit and hit[0] > now:
        q = hit[1]
        return dict(q) if q else None
    quote = None
    for key in chain:
        fn = US_FETCHERS.get(key)
        if not fn:
            continue
        try:
            quote = fn(code)
            quote["fetched_at"] = now_str()
            break
        except Exception:                      # noqa: BLE001
            quote = None
    with _lock:
        _cache[code] = (now + (CACHE_TTL if quote else NEG_CACHE_TTL), quote)
    if not quote:
        # 文件缓存兜底
        cached = _load_file_cache_us(code)
        if cached:
            with _lock:
                _cache[code] = (now + CACHE_TTL, cached)
            return dict(cached)
        return None
    quote = dict(quote)
    quote["cross_verdict"] = "⚠️ 单源模式（未做交叉验证；用 fetch_us_verified 获取逐源核验）"
    quote["consensus_price"] = quote.get("price")
    quote["cross_ok"] = False
    quote["market"] = "us"
    quote["error"] = None
    _save_file_cache_us(code, quote)
    return quote


def fetch_us_verified(code: str, timeout: float | None = None) -> tuple[dict | None, dict]:
    """完整交叉验证入口：拉全部 4 源，返回 (主源行情 dict, 验证 pack)。

    主源按 DEFAULT_US_CHAIN 优先序从成功源中挑；延迟源（Stooq）不充当主源，
    仅参与核验。返回的 pack 可直接传给 us_cross_md / us_context_line。
    """
    pack = collect_us_quotes(code)
    cross = pack["cross"]
    primary: dict | None = None
    for key in DEFAULT_US_CHAIN:
        for q in pack["quotes"]:
            if q.get("source") == key and not q.get("delayed"):
                primary = dict(q)
                break
        if primary:
            break
    if primary is None and pack["quotes"]:
        primary = dict(pack["quotes"][0])      # 极端：只剩延迟源也展示（带标注）
    if primary is None:
        # 全部失败 → 文件缓存兜底
        cached = _load_file_cache_us(code)
        if cached:
            primary = dict(cached)
            # 保留原 cross，但标注来自文件缓存
            primary["cross_verdict"] = (primary.get("cross_verdict") or "") + " (文件缓存兜底)"
            pack["errors"].append(f"文件缓存兜底：使用 {primary.get('source_label')} 的历史数据")
            return primary, pack
    if primary is not None:
        primary["market"] = "us"
        primary["cross_verdict"] = cross["verdict"]
        primary["consensus_price"] = cross["consensus"]
        primary["cross_ok"] = cross["ok"] and cross["n_ok"] >= 2
        primary["error"] = None
        _save_file_cache_us(code, primary)
    return primary, pack


# ================================================================ 渲染

def us_cross_md(pack: dict) -> str:
    """交叉验证 Markdown 区块：逐源报价表 + 一致性结论。空包返回空串。"""
    cross = pack.get("cross") or {}
    quotes = pack.get("quotes") or []
    if not quotes and not pack.get("errors"):
        return ""
    code = pack.get("code", "")
    lines = [
        "### 🔁 境外行情四源交叉验证",
        "",
        "| 数据源 | 价格 | 涨跌幅 | 偏离中位 | 行情时间 | 备注 |",
        "|---|---|---|---|---|---|",
    ]
    lat = pack.get("latency_ms") or {}
    for q in quotes:
        price = q.get("price")
        dev = ""
        if price and cross.get("consensus"):
            dev = f"{abs(price - cross['consensus']) / cross['consensus'] * 100:.2f}%"
        note = []
        if q.get("delayed"):
            note.append("延迟源≥15min")
        note.append(f"{lat.get(q.get('source'), 0)}ms")
        pct = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else "—"
        lines.append(
            f"| {generalize_source(q.get('source_label'))} | "
            f"**{price:.3f}** {q.get('currency', 'USD')} | {pct} | {dev or '—'} | "
            f"{q.get('time') or '—'} | {' · '.join(note)} |")
    for e in pack.get("errors", []):
        e_clean = sanitize_error(e)
        lines.append(
            f"| {generalize_source(e_clean.split('：')[0])} | — | — | — | — | ❌ {e_clean.split('：')[-1][:30]} |")
    if cross.get("consensus") is not None:
        lines += [
            "",
            f"> **共识价（中位）{cross['consensus']:.3f} USD** · {cross['verdict']}",
        ]
    elif cross.get("verdict"):
        lines += ["", f"> {cross['verdict']}"]
    lines.append("> 交叉验证口径：±0.8% 内一致 / ±2% 内基本一致 / 超出记分歧并标离群源。")
    return "\n".join(lines)


def us_context_line(pack: dict) -> str:
    """注入 AI 上下文的一行交叉验证结论。"""
    cross = pack.get("cross") or {}
    if not cross.get("ok"):
        return "境外行情交叉验证：全部行情源失败（见数据缺口）。"
    srcs = "、".join(generalize_source(s["label"]) for s in cross.get("per_source", [])[:4])
    return (f"境外行情交叉验证：{cross['verdict']}；共识价 {cross['consensus']} USD"
            f"（参与源：{srcs}；最大偏离 {cross.get('spread_pct')}%，"
            f"延迟核验源仅参与核验）。")


# ================================================================ 离线样本与自检

FIXTURES = {
    # 真实抓包形态样本（腾讯美股 GBK 管道串，字段位置与港股一致，价 231.590）
        "tencent_nvda": 'v_usNVDA="200~英伟达~NVDA~183.160~181.250~183.010~51223344.0~0~0~183.160~0~0~0~0~0~0~0~0~0~183.160~0~0~0~0~0~0~0~0~0~51223344.0~2026-08-13 16:00:00~1.910~1.05~185.220~180.140~183.160~51223344.0~93742148550.400~0~62.35~0~0~0~1.44~44670.0686~44670.0686~NVIDIA~1.12~217.090~86.620~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~USD~0~0~0~0~0";',
    "eastmoney_nvda": {"rc": 0, "rt": 4, "svr": 177998601, "lt": 1,
                       "full": False, "dlmkts": "",
                       "data": {"f43": 183160, "f44": 185220, "f45": 180140,
                                 "f46": 183010, "f47": 51223344, "f48": 9374214855,
                                 "f57": "NVDA", "f58": "英伟达", "f60": 181250,
                                 "f107": 105, "f116": 4467006860000,
                                 "f117": 4467006860000, "f152": 3,
                                 "f169": 1910, "f170": 105, "f171": 282}},
    "yahoo_nvda": {"chart": {"result": [{"meta": {
        "currency": "USD", "symbol": "NVDA", "regularMarketPrice": 183.15,
        "regularMarketDayHigh": 185.22, "regularMarketDayLow": 180.14,
        "regularMarketVolume": 51223344, "longName": "NVIDIA Corporation",
        "fiftyTwoWeekHigh": 217.09, "fiftyTwoWeekLow": 86.62,
        "regularMarketTime": 1786622400},
        "indicators": {"quote": [{"close": [181.26, 183.15]}]}}], "error": None}},
    # Stooq 延迟 CSV（价格刻意略低于实时源，模拟延迟差异，但仍在 ±2% 内）
    "stooq_nvda": ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
                   "NVDA.US,2026-08-13,22:00:02,183.01,185.22,180.14,182.90,51223344\n"),
}

US_PARSERS = {   # 供 source_check_db 离线模式复用：source_check_db 校验注册表
    "us_tencent": (parse_tencent_us, FIXTURES["tencent_nvda"], "NVDA"),
    "us_eastmoney": (parse_eastmoney_us, FIXTURES["eastmoney_nvda"], "NVDA"),
    "us_yahoo": (parse_yahoo_us, FIXTURES["yahoo_nvda"], "NVDA"),
    "us_stooq": (parse_stooq, FIXTURES["stooq_nvda"], "NVDA"),
}


def validate_us_quote(q: dict | None) -> list[str]:
    """单源行情字段校验规则。返回违例列表（空=通过）；供检查验证数据库使用。"""
    v = []
    if not q:
        return ["行情为空"]
    if not q.get("price") or q["price"] <= 0:
        v.append("价格缺失/异常")
    if q.get("currency") != "USD":
        v.append(f"币种异常：{q.get('currency')}")
    if q.get("market") != "us":
        v.append("market 字段非 us")
    if q.get("change_pct") is not None and abs(q["change_pct"]) > 25:
        v.append(f"涨跌幅异常：{q['change_pct']:+.2f}%（±25% 外熔断）")
    if q.get("prev_close") and q.get("change_pct") is None:
        v.append("有昨收但缺涨跌幅（字段不完整）")
    return v


def selftest() -> int:
    fails = 0

    def check(name: str, cond: bool):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("🌐 境外（美股）行情 + 交叉验证（离线样本自检，不触网）")
    check("代码识别 NVDA", detect_us_code("NVDA") == ("NVDA", "105.NVDA"))
    check("代码识别带后缀/前缀/小写",
          detect_us_code("aapl.us")[0] == "AAPL"
          and detect_us_code("NASDAQ:NVDA")[0] == "NVDA"
          and detect_us_code("NVDA.OQ")[0] == "NVDA")
    check("港股/A股不误判", detect_us_code("09988") is None
          and detect_us_code("600519") is None)

    q1 = parse_tencent_us(FIXTURES["tencent_nvda"], "NVDA")
    check("腾讯解析：价格/名称/币种",
          q1 and q1["price"] == 183.160 and q1["name"] == "英伟达"
          and q1["currency"] == "USD" and q1["market"] == "us")
    check("腾讯解析：涨跌/52周/市值",
          q1["change_pct"] == 1.05 and q1["high_52w"] == 217.090
          and q1["market_cap"] and q1["market_cap"] > 1e12)
    q2 = parse_eastmoney_us(FIXTURES["eastmoney_nvda"], "NVDA")
    check("东财解析：千分位价/涨跌",
          q2 and q2["price"] == 183.160 and round(q2["change_pct"], 2) == 1.05)
    q3 = parse_yahoo_us(FIXTURES["yahoo_nvda"], "NVDA")
    check("Yahoo解析：价/名/时间",
          q3 and q3["price"] == 183.15 and "NVIDIA" in q3["name"]
          and q3["time"] is not None)
    q4 = parse_stooq(FIXTURES["stooq_nvda"], "NVDA")
    check("Stooq解析：CSV价 + 延迟标记",
          q4 and q4["price"] == 182.90 and q4["delayed"] is True)

    # 交叉验证：4 源样本 → 一致（最大偏离 ~0.14%）
    cross = cross_validate([q1, q2, q3, q4])
    check("四源交叉：共识价=中位价",
          cross["ok"] and cross["n_ok"] == 4
          and abs((cross["consensus"] or 0) - 183.155) < 1e-6)
    check("四源交叉：延迟源小偏离计为一致",
          "一致" in cross["verdict"] and (cross["spread_pct"] or 9) < 0.8)
    check("四源交叉：逐源明细 4 条且含 Stooq 延迟标记",
          len(cross["per_source"]) == 4
          and any(x["delayed"] for x in cross["per_source"]))
    # 人为构造分歧：压盘价 5%
    q5 = dict(q4, price=174.0, source_label="坏源")
    bad = cross_validate([q1, q2, q5])
    check("分歧检测：>2% 离群被点名",
          "分歧" in bad["verdict"] and "坏源" in bad["outliers"])
    check("单源提示无法验证",
          "无法交叉验证" in cross_validate([q1])["verdict"])
    check("全失败 verdict",
          not cross_validate([])["ok"])

    # 字段校验规则
    check("正常行情无违例", validate_us_quote(q1) == [])
    check("空行情被拦截", validate_us_quote(None) != [])
    check("异常币种被拦截",
          validate_us_quote(dict(q1, currency="HKD")) != [])

    # 渲染
    pack = {"code": "NVDA", "quotes": [q1, q2, q3, q4],
            "errors": ["Stooq(延迟)：演示错误"], "latency_ms": {"tencent": 12},
            "cross": cross}
    md = us_cross_md(pack)
    check("交叉验证区块含表格/共识/口径",
          "##" in md and "共识价" in md and "±0.8%" in md and "四源交叉验证" in md)
    ctx = us_context_line(pack)
    check("AI 上下文含结论与源", "交叉验证" in ctx and "USD" in ctx)

    print(f"\n{'✅ 境外行情自检通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="境外（美股）实时行情 + 四源交叉验证")
    p.add_argument("code", nargs="?", default="", help="美股代码（NVDA / AAPL / aapl.us）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出主源行情")
    p.add_argument("--verify", action="store_true", help="输出四源交叉验证 Markdown")
    p.add_argument("--no-cache", action="store_true", help="禁用缓存强制重新拉取")
    p.add_argument("--selftest", action="store_true", help="离线样本自检")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        return selftest()

    det = detect_us_code(args.code or "")
    if not det:
        print(f"❌ 无法识别美股代码：{args.code!r}（纯数字请用 hk_quote.py）")
        return 1
    code, _ = det

    if args.verify:
        quote, pack = fetch_us_verified(code)
        print(us_cross_md(pack) or "（无数据）")
        return 0 if pack.get("quotes") else 1

    q = fetch_us_quote(code, use_cache=not args.no_cache)
    if not q:
        print(f"❌ {code} 全部境外行情源失败")
        return 1
    if args.json:
        print(json.dumps(q, ensure_ascii=False, indent=2))
    else:
        pct = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else ""
        print(f"{code} {q.get('name') or ''}：{q['price']:.3f} USD {pct}"
              f"（{q.get('source_label')} · {q.get('time') or q.get('fetched_at')}）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
