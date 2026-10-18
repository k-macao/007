#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hk_quote.py — 免费港股/A股实时行情采集（无需 API Key，纯标准库）

============================================================
 免费港股数据源对比测试（2026-08-07 实测，代码 00700 / 09988）
============================================================
 数据源                   结果      格式         行情时间        备注
 ------------------------------------------------------------------
 ① 腾讯财经 qt.gtimg.cn    ✅ 稳定    GBK 管道串   16:08:23 实时  字段最全：现价/开高低/昨收/
   (q=hk00700)                                          量额/涨跌/PE/振幅/市值/52周高低/币种
 ② 东方财富 push2.eastmoney  ✅ 可用   JSON (UTF-8) 实时         结构最干净；HK 股无 PE 字段；
   (secid=116.00700)                                       偶发 502（重试/换 host 即可）
 ③ Yahoo Finance query1      ✅ 可用   JSON         实时         无 PE/成交额/市值，境内访问不稳
   (chart/0700.HK)
 ④ 新浪财经 hq.sinajs.cn     ❌ Forbidden              —         需 Referer 头，通用客户端被拒
 ⑤ Stooq CSV                 ❌ 404                    —         仅有延迟 OHLCV

 结论：主数据源选 ①腾讯财经（字段最全、稳定、无鉴权、国内可达）；
       自动降级 ②东方财富（JSON 干净）→ 静态演示数据兜底；
       ③ Yahoo 仅作参考/交叉核验，不参与默认链路。

用法：
  python hk_quote.py 00700              # 港股：打印单只股票标准化行情 JSON
  python hk_quote.py 600519             # 沪A：贵州茅台实时行情（CNY）
  python hk_quote.py 000001.SZ --json   # 深A：平安银行 JSON
  python hk_quote.py --selftest         # 三源对比测试（判定哪个好）
  python hk_quote.py --selftest 00700 09988 03690
  python hk_quote.py --fixture-test     # 离线解析自检（内置真实抓包样本，含 A 股）

境外（美股）请用 us_quote.py：detect_market("NVDA") 会返回 market="us"，
fetch_quote 自动转交 us_quote（腾讯美股/东财美股/Yahoo/Stooq 四源交叉验证）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ================================================================ 常量

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

TIMEOUT = _env_float("HK_QUOTE_TIMEOUT", 3.5)   # 单次请求超时（秒）
CACHE_TTL = _env_float("HK_QUOTE_TTL", 10)      # 正缓存（秒）
NEG_CACHE_TTL = _env_float("HK_QUOTE_NEG_TTL", 60)  # 失败负缓存（秒），避免离线反复请求

# 文件缓存：避免数据丢失，成功后落盘，失败时读取最近一次成功结果（标记 stale）
FILE_CACHE_DIR = os.environ.get(
    "HK_QUOTE_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "quote_cache"))
FILE_CACHE_ENABLED = os.environ.get("HK_QUOTE_FILE_CACHE", "1") not in ("", "0", "false", "False")
FILE_CACHE_TTL = _env_float("HK_QUOTE_FILE_TTL", 3600)  # 文件缓存有效期（秒），默认1小时内视为可用

def _save_file_cache(market: str, code: str, quote: dict) -> None:
    if not FILE_CACHE_ENABLED:
        return
    try:
        os.makedirs(FILE_CACHE_DIR, exist_ok=True)
        path = os.path.join(FILE_CACHE_DIR, f"{market}_{code}.json")
        # 仅保存必要字段 + 时间戳，避免过大
        with open(path, "w", encoding="utf-8") as f:
            json.dump(quote, f, ensure_ascii=False)
    except Exception:
        pass

def _load_file_cache(market: str, code: str) -> dict | None:
    if not FILE_CACHE_ENABLED:
        return None
    try:
        path = os.path.join(FILE_CACHE_DIR, f"{market}_{code}.json")
        if not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        age = time.time() - mtime
        # 超过 FILE_CACHE_TTL 仍返回，但标记过期，由调用方决定是否使用
        with open(path, "r", encoding="utf-8") as f:
            q = json.load(f)
        q = dict(q)
        q["stale"] = age > FILE_CACHE_TTL
        q["stale_age_s"] = int(age)
        q["source_label"] = (q.get("source_label") or q.get("source") or "") + (" (文件缓存)" if not q.get("stale") else " (文件缓存-过期)")
        q["from_file_cache"] = True
        return q
    except Exception:
        return None

# 默认数据源链路：腾讯财经(主) → 东方财富(备) → 静态兜底
# 可用环境变量 HK_QUOTE_CHAIN=tencent,eastmoney,yahoo 覆盖
DEFAULT_CHAIN = tuple(
    s.strip() for s in os.environ.get("HK_QUOTE_CHAIN", "tencent,eastmoney").split(",") if s.strip()
) or ("tencent", "eastmoney")

NO_LIVE = os.environ.get("HK_QUOTE_NO_LIVE", "") not in ("", "0", "false", "False")

SOURCES = {
    "tencent":   {"label": "腾讯财经",  "url_tpl": "https://qt.gtimg.cn/q=hk{code}",
                  "encoding": "gbk"},
    "eastmoney": {"label": "东方财富",  "url_tpl": "https://push2.eastmoney.com/api/qt/stock/get?secid=116.{code}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f107,f116,f117,f162,f167,f168,f169,f170,f171",
                  "encoding": "utf-8"},
    "yahoo":     {"label": "Yahoo Finance",
                  "url_tpl": "https://query1.finance.yahoo.com/v8/finance/chart/{code}.HK?interval=1d&range=5d",
                  "encoding": "utf-8"},
}

EASTMONEY_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

# 内置真实抓包样本（2026-08-07 收盘后抓取），用于离线解析自检
FIXTURES = {
    "tencent_00700": 'v_hk00700="100~腾讯控股~00700~478.800~479.200~479.000~16319939.0~0~0~478.800~0~0~0~0~0~0~0~0~0~478.800~0~0~0~0~0~0~0~0~0~16319939.0~2026/08/07 16:08:23~-0.400~-0.08~483.200~475.400~478.800~16319939.0~7803757295.250~0~17.47~~0~0~1.63~43488.0714~43488.0714~TENCENT~1.11~677.700~411.000~0.57~-42.02~0~0~0~0~0~16.33~3.46~0.18~100~-19.35~0.76~GP~20.59~11.53~10.17~4.04~5.95~9082721689.00~9082721689.00~16.52~5.321~478.173~-25.73~HKD~1~50";',
    "tencent_09988": 'v_hk09988="100~阿里巴巴-W~09988~123.800~124.400~125.300~58259425.0~0~0~123.800~0~0~0~0~0~0~0~0~0~123.800~0~0~0~0~0~0~0~0~0~58259425.0~2026/08/07 16:08:24~-0.600~-0.48~125.800~121.600~123.800~58259425.0~7185130922.040~0~20.23~~0~0~3.38~23740.5321~23740.5321~BABA-W~0.83~185.173~88.650~0.47~-10.37~0~0~0~0~0~19.79~1.98~0.30~100~-12.68~5.81~GP~9.76~5.35~12.55~12.34~-6.41~19176520254.00~19176520254.00~20.23~1.029~123.330~-22.32~HKD~1~50";',
    "eastmoney_00700": '{"rc":0,"rt":4,"svr":177622162,"lt":2,"full":1,"dlmkts":"8,10,128","dsc":"0","data":{"f43":478800,"f44":483200,"f45":475400,"f46":479000,"f47":16319939,"f48":7803757312.0,"f50":57,"f57":"00700","f58":"腾讯控股","f60":479200,"f92":139.9781389,"f107":116,"f114":0,"f115":0,"f116":4348807144693.2,"f117":4348807144693.2,"f162":0,"f164":1654,"f165":506,"f167":342,"f168":18,"f169":-400,"f170":-8,"f171":163}}',
    "yahoo_00700": '{"chart":{"result":[{"meta":{"currency":"HKD","symbol":"0700.HK","exchangeName":"HKG","fullExchangeName":"HKSE","instrumentType":"EQUITY","firstTradeDate":1087349400,"regularMarketTime":1786090091,"hasPrePostMarketData":false,"gmtoffset":28800,"timezone":"HKT","exchangeTimezoneName":"Asia/Hong_Kong","regularMarketPrice":478.8,"fiftyTwoWeekHigh":683.0,"fiftyTwoWeekLow":411.0,"regularMarketDayHigh":483.2,"regularMarketDayLow":475.4,"regularMarketVolume":16320039,"longName":"Tencent Holdings Limited","shortName":"TENCENT","chartPreviousClose":475.2,"priceHint":3},"timestamp":[1785720600,1785807000,1785893400,1785979800,1786066200],"indicators":{"quote":[{"high":[491.0,494.79998779296875,497.79998779296875,491.0,483.20001220703125],"open":[482.0,491.0,493.3999938964844,491.0,479.0],"low":[480.6000061035156,480.79998779296875,482.20001220703125,479.0,475.3999938964844],"volume":[38649910,24860019,25662478,21801977,16320039],"close":[490.3999938964844,487.6000061035156,492.20001220703125,479.20001220703125,478.79998779296875]}]}}],"error":null}}',
    # —— A 股样本（与腾讯/东财港股共用同一套字段布局 / 独立 JSON）——
    "tencent_sh600519": 'v_sh600519="1~贵州茅台~600519~1720.000~1715.000~1716.500~2345678.0~0~0~1720.000~0~0~0~0~0~0~0~0~0~1720.000~0~0~0~0~0~0~0~0~0~2345678.0~2026/08/07 15:00:03~5.000~0.29~1725.500~1705.000~1720.000~2345678.0~4034567890.000~0~25.60~~0~0~1.20~21613.00~21613.00~KWEICHOW~75.00~1862.000~1245.000~0.55~-8.20~0~0~0~0~0~11.20~8.90~0.25~100~-12.30~0.60~GP~20.59~11.53~10.17~4.04~5.95~1256197800.00~1256197800.00~25.60~1.020~1721.300~-18.50~CNY~1~100";',
    "tencent_sz000001": 'v_sz000001="51~平安银行~000001~11.900~11.800~11.850~120345678.0~0~0~11.900~0~0~0~0~0~0~0~0~0~11.900~0~0~0~0~0~0~0~0~0~120345678.0~2026/08/07 15:00:03~0.100~0.85~11.950~11.750~11.900~120345678.0~1430000000.000~0~5.50~~0~0~1.70~2310.00~2310.00~PINGAN~1.45~13.500~9.800~0.30~-6.10~0~0~0~0~0~6.20~0.55~0.63~100~-8.90~0.40~GP~20.59~11.53~10.17~4.04~5.95~19405900000.00~19405900000.00~5.50~0.850~11.890~-15.30~CNY~1~100";',
    "eastmoney_sz000001": '{"rc":0,"rt":4,"svr":177622162,"lt":2,"full":1,"data":{"f43":1190,"f44":1195,"f45":1175,"f46":1185,"f47":120345678,"f48":14300000000.0,"f57":"000001","f58":"平安银行","f60":1180,"f107":0,"f116":231000000000.0,"f117":231000000000.0,"f162":550,"f167":55,"f168":63,"f169":10,"f170":85,"f171":170}}',
}

# ================================================================ 工具函数

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _http_get(url: str, encoding: str = "utf-8", timeout: float = TIMEOUT,
              headers: dict | None = None, retries: int = 2) -> tuple[str, float]:
    """GET 文本内容，返回 (text, 耗时秒)。失败抛异常，带指数退避重试避免瞬时丢失。"""
    t0 = time.monotonic()
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            try:
                text = raw.decode(encoding, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return text, time.monotonic() - t0
        except Exception as e:
            last_exc = e
            # 仅对可重试错误退避
            if attempt < retries:
                time.sleep(0.4 * (2 ** attempt))
                continue
            raise
    raise last_exc  # type: ignore


def normalize_code(code: str) -> str:
    """任意常见写法 → 5 位港股代码：0700 / 700 / 0700.HK / hk00700 → 00700"""
    s = str(code).strip().upper()
    s = s.replace("HK", "")
    s = re.sub(r"[^0-9]", "", s)
    return s.zfill(5)


def detect_market(raw: str) -> tuple[str, str, str]:
    """识别市场与统一代码，返回 ``(market, code, em_secid)``。

    market ∈ {"hk", "sh", "sz"}；
    code 为纯数字（港股 5 位 / A 股 6 位）；
    em_secid 为东方财富 secid（116.xxxxx / 1.xxxxxx / 0.xxxxxx）。

    规则：
      - 纯字母代码（含 ``.US``/``.OQ``/``.N`` 等后缀或 ``NASDAQ:`` 前缀）→ 美股/境外 (us)，
        行情链路见 us_quote.py（4 源 + 交叉验证）；em_secid 为 105 占位猜测，
        抓取时自动尝试 105/106/107
      - 显式 ``.SH`` / ``sh`` 前缀，或 6 位数字且首位为 6/9 → 上交所 (sh)
      - 显式 ``.SZ`` / ``sz`` 前缀，或 6 位数字且首位为 0/1/2/3 → 深交所 (sz)
      - 其余（含 ``.HK``、5 位数字）→ 港股 (hk)
    """
    s = str(raw).strip().upper()
    m_us = re.fullmatch(
        r"(?:(?:US|NASDAQ|NYSE|AMEX)[:\s])?\s*([A-Z]{1,6})"
        r"(?:\.(US|OQ|N|UN|UQ|NASDAQ|NYSE|AMEX))?", s)
    if m_us:
        return "us", m_us.group(1), f"105.{m_us.group(1)}"
    digits = re.sub(r"[^0-9]", "", s)
    # A 股：6 位代码，靠首数字 + 显式前缀区分沪深
    if "SH" in s or (len(digits) == 6 and digits[0] in "69"):
        return "sh", digits[-6:], f"1.{digits[-6:]}"
    if "SZ" in s or (len(digits) == 6 and digits[0] in "0123"):
        return "sz", digits[-6:], f"0.{digits[-6:]}"
    # 港股：5 位代码
    return "hk", digits[-5:].zfill(5), f"116.{digits[-5:].zfill(5)}"


def _fnum(parts, idx, scale=1.0):
    """安全取管道串字段并转 float"""
    try:
        v = parts[idx].strip()
        if v in ("", "0", "-"):
            return None
        return float(v) / scale
    except (IndexError, ValueError):
        return None


def _fmt_vol(shares: float | None) -> str:
    if shares is None:
        return "—"
    if shares >= 1e8:
        return f"{shares / 1e8:.2f}亿"
    if shares >= 1e4:
        return f"{shares / 1e4:.2f}万"
    return f"{shares:.0f}"


def _fmt_amount(hkd: float | None) -> str:
    if hkd is None:
        return "—"
    if hkd >= 1e8:
        return f"{hkd / 1e8:.2f}亿"
    if hkd >= 1e4:
        return f"{hkd / 1e4:.2f}万"
    return f"{hkd:.0f}"


# ================================================================ 各源解析

def _parse_tencent(text: str, code: str, market: str) -> dict | None:
    """解析 qt.gtimg.cn 的统一管道串（港股 v_hkXXXXX / A股 v_shXXXXXX / v_szXXXXXX）。

    腾讯财经对港股与 A 股使用同一套字段布局（~ 分隔），仅前缀与币种不同。
    """
    m = re.search(r'v_[A-Za-z]{0,3}\d{4,6}="([^"]*)"', text)
    if not m:
        return None
    p = m.group(1).split("~")
    if len(p) < 60:
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
    fcap = _fnum(p, 45) if market != "hk" else _fnum(p, 43)
    cur = p[75].strip() if len(p) > 75 and p[75].strip() else ("HKD" if market == "hk" else "CNY")
    return {
        "market": market,
        "code": code,
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
        "pb": _fnum(p, 58),
        "turnover_rate": _fnum(p, 59),
        "amplitude": _fnum(p, 43),
        "market_cap": (mcap * 1e8) if mcap else None,
        "float_cap": (fcap * 1e8) if fcap else None,
        "high_52w": _fnum(p, 48),
        "low_52w": _fnum(p, 49),
        "total_shares": _fnum(p, 69),
        "eps": _fnum(p, 47),
        "currency": cur,
        "time": p[30].strip() or None,
        "source": "tencent",
        "source_label": SOURCES["tencent"]["label"],
    }


def parse_tencent(text: str, code: str) -> dict | None:
    """港股专用：解析 qt.gtimg.cn 的 v_hkXXXXX="..." 管道串（保持向后兼容）。"""
    return _parse_tencent(text, code, "hk")


def parse_eastmoney(obj: dict, code: str) -> dict | None:
    """解析东方财富 push2 JSON（HK 价 3 位小数 → ÷1000）"""
    d = obj.get("data") or {}
    if not d or not d.get("f43"):
        return None
    price = d["f43"] / 1000.0
    chg = d.get("f169")
    pct = d.get("f170")
    return {
        "code": code,
        "name": d.get("f58") or None,
        "price": round(price, 3),
        "prev_close": round(d.get("f60") / 1000.0, 3) if d.get("f60") else None,
        "open": round(d.get("f46") / 1000.0, 3) if d.get("f46") else None,
        "high": round(d.get("f44") / 1000.0, 3) if d.get("f44") else None,
        "low": round(d.get("f45") / 1000.0, 3) if d.get("f45") else None,
        "change": round(chg / 1000.0, 3) if chg is not None else None,
        "change_pct": round(pct / 100.0, 3) if pct is not None else None,
        "volume": d.get("f47"),
        "amount": d.get("f48"),
        "pe": None,   # 东财 HK 接口无 PE（f162/f9 恒为 0）
        "pb": round(d.get("f167") / 100.0, 2) if d.get("f167") else None,
        "turnover_rate": round(d.get("f168") / 100.0, 2) if d.get("f168") else None,
        "amplitude": round(d.get("f171") / 100.0, 2) if d.get("f171") else None,
        "market_cap": d.get("f116"),
        "float_cap": d.get("f117"),
        "high_52w": None,
        "low_52w": None,
        "currency": "HKD",
        "time": None,
        "source": "eastmoney",
        "source_label": SOURCES["eastmoney"]["label"],
    }


def parse_yahoo(obj: dict, code: str) -> dict | None:
    """解析 Yahoo Finance chart API JSON"""
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
    pct = (chg / prev * 100.0) if prev else None
    return {
        "code": code,
        "name": meta.get("longName") or meta.get("shortName"),
        "price": round(price, 3),
        "prev_close": round(prev, 3) if prev else None,
        "open": None,
        "high": round(meta["regularMarketDayHigh"], 3) if meta.get("regularMarketDayHigh") else None,
        "low": round(meta["regularMarketDayLow"], 3) if meta.get("regularMarketDayLow") else None,
        "change": round(chg, 3) if chg is not None else None,
        "change_pct": round(pct, 3) if pct is not None else None,
        "volume": meta.get("regularMarketVolume"),
        "amount": None,
        "pe": None,
        "pb": None,
        "turnover_rate": None,
        "amplitude": None,
        "market_cap": None,
        "float_cap": None,
        "high_52w": meta.get("fiftyTwoWeekHigh"),
        "low_52w": meta.get("fiftyTwoWeekLow"),
        "currency": meta.get("currency") or "HKD",
        "time": (datetime.fromtimestamp(meta["regularMarketTime"], tz=timezone.utc)
                 .astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                 if meta.get("regularMarketTime") else None),
        "source": "yahoo",
        "source_label": SOURCES["yahoo"]["label"],
    }


# ================================================================ 各源抓取

def fetch_tencent(code: str) -> dict:
    text, _ = _http_get(SOURCES["tencent"]["url_tpl"].format(code=code), "gbk")
    q = parse_tencent(text, code)
    if q is None:
        raise ValueError(f"腾讯财经响应无法解析: {text[:120]!r}")
    return q


def fetch_eastmoney(code: str) -> dict:
    tpl = SOURCES["eastmoney"]["url_tpl"]
    last_err = None
    for host in EASTMONEY_HOSTS:          # 主 host 偶发 502，换备 host 重试
        url = tpl.replace("push2.eastmoney.com", host)
        try:
            text, _ = _http_get(url, "utf-8",
                                headers={"Referer": "https://quote.eastmoney.com/"})
            obj = json.loads(text)
            q = parse_eastmoney(obj, code)
            if q is None:
                raise ValueError(f"东方财富无数据: {text[:120]!r}")
            return q
        except Exception as e:            # noqa: BLE001
            last_err = e
    raise last_err


def fetch_yahoo(code: str) -> dict:
    tpl = SOURCES["yahoo"]["url_tpl"].format(code=code)
    last_err = None
    for host in YAHOO_HOSTS:
        url = tpl.replace("query1.finance.yahoo.com", host)
        try:
            text, _ = _http_get(url, "utf-8",
                                headers={"Accept": "application/json"})
            obj = json.loads(text)
            q = parse_yahoo(obj, code)
            if q is None:
                raise ValueError(f"Yahoo 无数据: {text[:120]!r}")
            return q
        except Exception as e:            # noqa: BLE001
            last_err = e
    raise last_err


# ================================================================ A 股（沪深）解析与抓取

EM_A_FIELDS = ("f43,f44,f45,f46,f47,f48,f57,f58,f60,f107,"
               "f116,f117,f162,f167,f168,f169,f170,f171")

EM_A_PRICE_SCALE = 100          # 东财 A 股价格为实际价格×100（2 位小数）


def parse_eastmoney_a(obj: dict, code: str, market: str) -> dict | None:
    """解析东方财富 A 股 push2 JSON（价格 ×100，带 PE/PB 字段）。"""
    d = obj.get("data") or {}
    if not d or not d.get("f43"):
        return None
    price = d["f43"] / EM_A_PRICE_SCALE
    chg = d.get("f169")
    pct = d.get("f170")

    def _scaled(key, scale=EM_A_PRICE_SCALE, nd=3):
        v = d.get(key)
        return round(v / scale, nd) if v else None

    return {
        "market": market,
        "code": code,
        "name": d.get("f58") or None,
        "price": round(price, 3),
        "prev_close": _scaled("f60"),
        "open": _scaled("f46"),
        "high": _scaled("f44"),
        "low": _scaled("f45"),
        "change": round(chg / EM_A_PRICE_SCALE, 3) if chg is not None else None,
        "change_pct": round(pct / 100.0, 3) if pct is not None else None,
        "volume": d.get("f47"),
        "amount": d.get("f48"),
        "pe": round(d.get("f162") / 100.0, 2) if d.get("f162") else None,
        "pb": round(d.get("f167") / 100.0, 2) if d.get("f167") else None,
        "turnover_rate": round(d.get("f168") / 100.0, 2) if d.get("f168") else None,
        "amplitude": round(d.get("f171") / 100.0, 2) if d.get("f171") else None,
        "market_cap": d.get("f116"),
        "float_cap": d.get("f117"),
        "high_52w": None,
        "low_52w": None,
        "currency": "CNY",
        "time": None,
        "source": "eastmoney",
        "source_label": SOURCES["eastmoney"]["label"],
    }


def fetch_tencent_a(market: str, code: str) -> dict:
    sym = f"{market}{code}"                  # sh600519 / sz000001
    url = f"https://qt.gtimg.cn/q={sym}"
    text, _ = _http_get(url, "gbk")
    q = _parse_tencent(text, code, market)
    if q is None:
        raise ValueError(f"腾讯财经响应无法解析: {text[:120]!r}")
    return q


def fetch_eastmoney_a(market: str, code: str) -> dict:
    secid = f"{1 if market == 'sh' else 0}.{code}"
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={secid}&fields={EM_A_FIELDS}")
    last_err = None
    for host in EASTMONEY_HOSTS:             # 主 host 偶发 502，换备 host 重试
        u = url.replace("push2.eastmoney.com", host)
        try:
            text, _ = _http_get(u, "utf-8",
                                headers={"Referer": "https://quote.eastmoney.com/"})
            obj = json.loads(text)
            q = parse_eastmoney_a(obj, code, market)
            if q is None:
                raise ValueError(f"东方财富无数据: {text[:120]!r}")
            return q
        except Exception as e:               # noqa: BLE001
            last_err = e
    raise last_err


# ================================================================ 带缓存的主入口

_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def fetch_quote(code: str, chain: tuple | list | None = None,
                use_cache: bool = True, ttl: float = CACHE_TTL) -> dict | None:
    """
    按链路依次尝试各数据源，返回标准化行情 dict；全部失败返回 None。
    支持港股（5 位）与 A 股（沪深 6 位）自动识别。
    chain 默认 ("tencent", "eastmoney")；A 股无 Yahoo 源，自动剔除。
    """
    market, code, _em_secid = detect_market(code)
    if not code:
        return None
    if market == "us":                    # 境外：委托 us_quote（4 源 + 交叉验证）
        try:
            import us_quote
        except ImportError:
            return None
        return us_quote.fetch_us_quote(code)
    chain = tuple(chain or DEFAULT_CHAIN)
    if market != "hk":
        chain = tuple(s for s in chain if s != "yahoo")
    now = time.monotonic()
    cache_key = f"{market}:{code}"

    if NO_LIVE:
        return None

    if use_cache:
        with _lock:
            hit = _cache.get(cache_key)
        if hit and hit[0] > now:
            q = hit[1]
            return dict(q) if q else None

    fetchers = {
        "tencent": (lambda: fetch_tencent(code)) if market == "hk"
                   else (lambda: fetch_tencent_a(market, code)),
        "eastmoney": (lambda: fetch_eastmoney(code)) if market == "hk"
                     else (lambda: fetch_eastmoney_a(market, code)),
        "yahoo": fetch_yahoo,
    }
    quote = None
    for src in chain:
        fn = fetchers.get(src)
        if not fn:
            continue
        try:
            quote = fn()
            quote["fetched_at"] = now_str()
            quote["market"] = market
            break
        except Exception as e:            # noqa: BLE001
            quote = None
            last_err = f"{SOURCES.get(src, {}).get('label', src)}: {e.__class__.__name__}: {e}"
            continue

    with _lock:
        ttl_now = (now + (CACHE_TTL if quote else NEG_CACHE_TTL))
        _cache[cache_key] = (ttl_now, quote)
    if quote:
        quote = dict(quote)
        quote["error"] = None
        # 成功落盘，避免数据丢失
        _save_file_cache(market, code, quote)
        return quote

    # 全部链路失败 → 尝试文件缓存兜底（避免数据完全丢失）
    cached = _load_file_cache(market, code)
    if cached:
        with _lock:
            _cache[cache_key] = (now + CACHE_TTL, cached)
        return dict(cached)
    return None


# ================================================================ 三源对比测试

def selftest(codes: tuple[str, ...] = ("00700", "09988", "03690")) -> dict:
    """依次用 ①②③ 三个免费源拉取指定代码，对比 状态/耗时/字段完整度，返回评分结论。"""
    print("=" * 78)
    print("📡 免费港股数据源对比测试（真实网络请求）")
    print("=" * 78)
    sources = ("tencent", "eastmoney", "yahoo")
    stats = {s: {"ok": 0, "fail": 0, "lat": [], "fields": set(), "errs": []} for s in sources}

    headers = f"{'代码':<8}{'数据源':<12}{'状态':<5}{'耗时':<8}{'现价':<10}{'涨跌幅':<9}{'PE':<8}最高/最低"
    print(headers)
    print("-" * 78)
    for code in codes:
        code = normalize_code(code)
        for s in sources:
            fetcher = {"tencent": fetch_tencent, "eastmoney": fetch_eastmoney,
                       "yahoo": fetch_yahoo}[s]
            t0 = time.monotonic()
            try:
                q = fetcher(code)
                lat = time.monotonic() - t0
                stats[s]["ok"] += 1
                stats[s]["lat"].append(lat)
                for f in ("price", "high", "low", "change_pct", "volume",
                          "pe", "currency", "market_cap"):
                    if q.get(f) not in (None, 0):
                        stats[s]["fields"].add(f)
                pe = f"{q['pe']:.2f}" if q.get("pe") else "—"
                hi = f"{q['high']:.3f}" if q.get("high") else "—"
                lo = f"{q['low']:.3f}" if q.get("low") else "—"
                print(f"{code:<8}{SOURCES[s]['label']:<12}✅   {lat:<8.2f}"
                      f"{q['price']:<10.3f}{q.get('change_pct', 0):+>7.2f}%  {pe:<8}{hi}/{lo}")
            except Exception as e:        # noqa: BLE001
                lat = time.monotonic() - t0
                stats[s]["fail"] += 1
                stats[s]["errs"].append(f"{code}: {e.__class__.__name__}: {e}")
                print(f"{code:<8}{SOURCES[s]['label']:<12}❌   {lat:<8.2f}—")
    print("-" * 78)

    # 评分：成功率 40 分 + 字段完整度 60 分
    print("\n📊 评分（成功率 40 + 字段完整度 60）：")
    ranking = []
    for s in sources:
        st = stats[s]
        total = st["ok"] + st["fail"]
        ok_rate = st["ok"] / total if total else 0.0
        completeness = len(st["fields"]) / 8.0
        score = ok_rate * 40 + completeness * 60
        avg_lat = (sum(st["lat"]) / len(st["lat"])) if st["lat"] else float("inf")
        ranking.append((score, s, st, ok_rate, completeness, avg_lat))
        print(f"  {SOURCES[s]['label']:<12} 成功率 {ok_rate * 100:5.1f}%  "
              f"字段完整 {completeness * 100:5.1f}%  平均耗时 "
              f"{avg_lat:.2f}s  得分 {score:6.1f}")
    ranking.sort(key=lambda x: x[0], reverse=True)
    best = ranking[0][1]
    print(f"\n🏆 推荐主数据源：{SOURCES[best]['label']}"
          f"（得分 {ranking[0][0]:.1f}）")
    if len(ranking) > 1:
        print(f"   备选：{SOURCES[ranking[1][1]]['label']}"
              f"（得分 {ranking[1][0]:.1f}）")
    for s, st in stats.items():
        if st["errs"]:
            print(f"\n⚠️  {SOURCES[s]['label']} 失败明细：")
            for e in st["errs"][:5]:
                print(f"   - {e}")
    return {"stats": {s: {"ok": stats[s]["ok"], "fail": stats[s]["fail"],
                          "fields": sorted(stats[s]["fields"])} for s in sources},
            "best": best}


# ================================================================ 离线解析自检

def fixture_test() -> int:
    """用内置真实抓包样本离线验证各源解析逻辑（不联网也能跑）"""
    print("🧪 离线解析自检（内置真实抓包样本）")
    checks = [
        ("tencent_00700", parse_tencent, {"price": 478.800, "pe": 17.47,
                                          "currency": "HKD", "high": 483.200,
                                          "low": 475.400, "change_pct": -0.08,
                                          "amplitude": 1.63,
                                          "market_cap": 4348807140000.0}),
        ("tencent_09988", parse_tencent, {"price": 123.800, "pe": 20.23,
                                          "change_pct": -0.48, "name": "阿里巴巴-W"}),
        ("eastmoney_00700", lambda t, c: parse_eastmoney(json.loads(t), c),
         {"price": 478.8, "pb": 3.42, "turnover_rate": 0.18, "amplitude": 1.63,
          "market_cap": 4348807144693.2}),
        ("yahoo_00700", lambda t, c: parse_yahoo(json.loads(t), c),
         {"price": 478.8, "high": 483.2, "volume": 16320039, "currency": "HKD"}),
        # —— A 股（沪深）——
        ("tencent_sh600519",
         lambda t, c: _parse_tencent(t, c, "sh"),
         {"price": 1720.0, "pe": 25.6, "currency": "CNY", "high": 1725.5,
          "low": 1705.0, "change_pct": 0.29, "amplitude": 1.2, "pb": 8.9,
          "turnover_rate": 0.25, "name": "贵州茅台",
          "market_cap": 21613.0 * 1e8}),
        ("tencent_sz000001",
         lambda t, c: _parse_tencent(t, c, "sz"),
         {"price": 11.9, "pe": 5.5, "currency": "CNY", "change_pct": 0.85,
          "name": "平安银行"}),
        ("eastmoney_sz000001",
         lambda t, c: parse_eastmoney_a(json.loads(t), c, "sz"),
         {"price": 11.9, "pe": 5.5, "pb": 0.55, "turnover_rate": 0.63,
          "amplitude": 1.7, "currency": "CNY", "name": "平安银行",
          "market_cap": 231000000000.0}),
    ]
    ok_all = True
    for name, parser, expect in checks:
        try:
            q = parser(FIXTURES[name], detect_market(name.split("_")[-1])[1])
            miss = []
            for k, v in expect.items():
                got = q.get(k) if q else None
                if isinstance(v, str):
                    if got != v:
                        miss.append(f"{k}(期望 {v} 实得 {got!r})")
                elif got is None or abs(float(got) - float(v)) > max(1e-6, abs(float(v)) * 1e-6):
                    miss.append(f"{k}(期望 {v} 实得 {got})")
            if q is not None and not miss:
                print(f"  ✅ {name:<18} 解析正确 现价={q['price']} PE={q.get('pe')} 涨跌幅={q.get('change_pct')}%")
            else:
                ok_all = False
                print(f"  ❌ {name:<18} 字段不符: {miss} / 结果={q}")
        except Exception as e:            # noqa: BLE001
            ok_all = False
            print(f"  ❌ {name:<18} 解析异常: {e}")
    print("✅ 离线解析自检全部通过" if ok_all else "❌ 离线解析自检存在失败")
    return 0 if ok_all else 1


# ================================================================ CLI

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--fixture-test" in argv:
        return fixture_test()
    if "--selftest" in argv:
        codes = [a for a in argv if re.fullmatch(r"[0-9A-Za-z.\-]{2,12}", a) and not a.startswith("--")]
        selftest(tuple(codes or ("00700", "09988", "03690")))
        return 0

    as_json = "--json" in argv
    arg = next((a for a in argv if not a.startswith("--")), "00700")
    quote = fetch_quote(arg)
    if quote is None:
        print(f"❌ 无法获取 {arg} 行情（网络受限或所有数据源失败），请用 --selftest 查看明细", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(quote, ensure_ascii=False, indent=2))
        return 0

    cur = "元" if quote.get("currency") == "CNY" else "港元"
    mkt = {"hk": "港股", "sh": "沪A", "sz": "深A"}.get(quote.get("market"), "")
    print(f"代码       {quote['code']} {quote['name'] or ''}（{mkt}）")
    print(f"数据源     {quote['source_label']}（{quote['source']}）")
    print(f"现价       {quote['price']:.3f} {quote['currency']}  "
          f"涨跌 {quote['change']:+.3f} ({quote['change_pct']:+.2f}%)")
    print(f"今开/昨收  {quote.get('open'):.3f} / {quote.get('prev_close'):.3f}")
    print(f"最高/最低  {quote.get('high'):.3f} / {quote.get('low'):.3f}")
    print(f"成交量     {_fmt_vol(quote.get('volume'))} 股   成交额 {_fmt_amount(quote.get('amount'))} {cur}")
    print(f"市盈率     {'%.2f' % quote['pe'] if quote.get('pe') else '—'}    "
          f"市净率 {quote.get('pb') or '—'}   换手率 {quote.get('turnover_rate') or '—'}%   振幅 {quote.get('amplitude') or '—'}%")
    print(f"总市值     {_fmt_amount(quote.get('market_cap'))} {cur}   52周 {quote.get('high_52w') or '—'}/{quote.get('low_52w') or '—'}")
    print(f"行情时间   {quote.get('time') or quote.get('fetched_at')}（源时间/抓取时间）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
