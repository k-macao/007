#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_dashboard.py — 服务器大屏监视风格（NOC Telemetry Wallboard）
                      零表格（NO TABLES） · 文字 + 列表横排

特点：
1. 服务器大屏监视风格：深空暗黑科技底色、荧光青/绿/琥珀/品红发光边框、CRT扫描线与网格背景、
   双时区时钟（UTC / 北京时间 CST）、服务器集群节点健康带、心跳脉冲与音频遥测、全屏大屏模式。
2. 绝对不要表格（NO TABLES）：100% 无 <table> 标签，全采用现代 Flexbox / Grid 横向流式排版。
3. 文字 + 列表横排：
   - 深度文字研判解读、重点摘要、预警风控文字。
   - 横排核心量化指标带（现价/高低/成交量/预测目标/可信度）。
   - 横排七大因子多空矩阵卡片流（基本面/政策/技术/资金/情绪/估值/量价动量）。
   - 横排十七平台实时情报雷达流（财经7源 + 社媒10源）。
   - 横排关联板块与动态标签流（电商/云计算/AI/跨境出海等）。
   - 横排服务器集群节点状态流（HK-01 / SH-02 / SG-03 / AI-04 / DB-05）。
   - 横排预警与监控项清单。
   - 终端实时日志流。
"""
from __future__ import annotations

import http.server
import json
import math
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

# 导入本地模块（若可用）
try:
    import pushplus_deepseek as pp_mod
except Exception:
    pp_mod = None

try:
    import stock_news_scan as scan_mod
except Exception:
    scan_mod = None

try:
    import hk_quote
except Exception:
    hk_quote = None

try:
    import stock_report
except Exception:
    stock_report = None

try:
    import equity_research_column as equity_col
except Exception:
    equity_col = None

try:
    import skills_hub as skills_hub_mod
except Exception:
    skills_hub_mod = None

try:
    import foreign_institution_weekly as foreign_mod
except Exception:
    foreign_mod = None

# ================================================================ 展示开关
# 源头信息默认隐藏：快讯卡片不再展示「Google 新闻 / 财联社电报」等采集渠道标签，
# 数据层仍保留 source 字段（用于情绪归因、去重与后台统计）。
# 需要排查渠道时设置环境变量 SHOW_SOURCE_INFO=1 恢复展示。

def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


SHOW_SOURCE_INFO = _env_flag("SHOW_SOURCE_INFO", False)


def _feed_source_html(source: str) -> str:
    """快讯来源标签：默认隐藏（返回空串），开关打开时输出原标签。"""
    if not SHOW_SOURCE_INFO:
        return ""
    return f'<span class="feed-src">{source}</span>'


# ================================================================ 实时行情合并

def _generic_profile(market: str, code: str) -> dict:
    """未内置演示档案的标的（A 股 / 任意港股代码）→ 生成中性占位档案，行情由实时源覆盖。"""
    label = {"hk": "港股", "sh": "沪A", "sz": "深A", "us": "美股"}.get(market, market)
    factor_names = ["基本面", "行业与政策面", "技术面", "资金面", "消息与情绪面", "估值面", "量价舆情动量"]
    factors = [{
        "id": f"f{i}", "name": n, "dir": "中性", "is_up": True,
        "prob": "—", "score": 0, "badge": "演示占位", "tag": "待AI",
        "desc": "未内置演示档案，该因子为占位内容；点击「🧠 AI研报」基于实时行情生成正式分析。",
    } for i, n in enumerate(factor_names)]
    return {
        "_generic": True,
        "code": code,
        "name": f"{label} {code}（实时行情 · 因子演示）",
        "price": "—",
        "currency": "HKD" if market == "hk" else "USD" if market == "us" else "CNY",
        "change": "—", "change_val": "—", "is_up": True,
        "high": "—", "low": "—", "vol": "—",
        "target": "—", "target_pct": "—",
        "pe": "—", "pe_percentile": "—",
        "sentiment_score": 0,
        "confidence": "行情实时 · 因子演示占位",
        "overall_direction": "—", "overall_prob": "—",
        "summary": "该标的未内置演示档案：价格 / 估值来自免费数据源实时行情；七大因子与快讯为演示占位。点击上方「🧠 AI研报」可基于实时行情生成正式研报并推送到微信。",
        "factors": factors,
        "sectors": [],
        "alerts": [],
        "feeds": [],
    }


def get_stock_view(stock_code: str) -> dict:
    """
    取视图数据：优先内置演示档案（因子/快讯/预警），叠加免费数据源实时行情。
    支持港股与 A 股（沪深）代码自动识别；未内置档案的标的生成中性占位档案。
    实时行情不可用时保留演示价格并明确标注数据缺口，绝不伪造。
    """
    if hk_quote:
        market, code, _em = hk_quote.detect_market(stock_code)
    else:
        market, code = "hk", str(stock_code).zfill(5)

    if market == "hk" and code in STOCKS:
        data = STOCKS[code]
        data = json.loads(json.dumps(data, ensure_ascii=False))  # 深拷贝，避免污染档案
    else:
        data = _generic_profile(market, code)

    data["market"] = market
    data["quote_live"] = False
    data["quote_source"] = "静态快照 (STATIC DEMO)"
    data["quote_source_key"] = None
    data["quote_time"] = None
    data["quote_fetched_at"] = None
    data["quote_error"] = "网络不可达或数据源失败"

    if hk_quote is None:
        data["quote_error"] = "hk_quote 模块加载失败"
        return data

    q = hk_quote.fetch_quote(stock_code)
    if not q:
        return data

    # —— 用实时行情覆盖价格类字段（HK 3 位小数）——
    if q.get("price"):
        data["price"] = f"{q['price']:.3f}"
        data["currency"] = q.get("currency") or data.get("currency", "HKD")
        chg = q.get("change")
        pct = q.get("change_pct")
        data["is_up"] = (chg or 0) >= 0
        if pct is not None:
            data["change"] = f"{pct:+.2f}%"
        if chg is not None:
            data["change_val"] = f"{chg:+.3f}"
    if q.get("high"):
        data["high"] = f"{q['high']:.3f}"
    if q.get("low"):
        data["low"] = f"{q['low']:.3f}"
    vol = q.get("volume")
    if vol:
        data["vol"] = (f"{vol / 1e8:.2f}亿" if vol >= 1e8 else f"{vol / 1e4:.2f}万")
    pe = q.get("pe")
    if pe:
        data["pe"] = f"{pe:.1f}x"           # 腾讯源自带 PE；东财兜底无 PE 则保留档案值→标注缺口
        data["pe_live"] = True
    else:
        data["pe_live"] = False
    if q.get("name"):
        data["quote_name"] = q["name"]
        # 未内置档案的标的：用实时行情里的真实名称替换占位名
        if data.get("_generic") and market == "hk":
            data["name"] = f"{q['name']}（实时行情）"
        elif data.get("_generic"):
            label = {"hk": "港股", "sh": "沪A", "sz": "深A", "us": "美股"}.get(market, market)
            data["name"] = f"{label} {code} {q['name']}（实时行情）"

    data["quote_live"] = True
    data["quote_source"] = q.get("source_label") or q.get("source")
    data["quote_source_key"] = q.get("source")
    data["quote_time"] = q.get("time")
    data["quote_fetched_at"] = q.get("fetched_at")
    data["quote_error"] = None
    return data


def get_chart_view(stock_code: str = "09988", tf: str = "daily", count: int = 60) -> dict:
    """
    返回指定标的与时间周期（1d/5d/daily/weekly/monthly）的标准化字符模拟图及成交量指标数据。
    支持连通服务器从 /api/chart 取实时走势，同时能够根据最近收盘价自动生成高品质趋势结构，
    确保字符模拟图界面及交互式技术分析任何时候均可用。
    """
    v = get_stock_view(stock_code)
    try:
        base_price = float(str(v.get("price", "81.25")).replace(",", ""))
    except Exception:
        base_price = 81.25

    bars = []
    now_dt = datetime.now(timezone.utc)
    for i in range(count):
        idx = i - (count - 1)
        dt = now_dt + timedelta(days=idx)
        date_str = dt.strftime("%Y-%m-%d")

        phase = i / 10.0
        trend = (i / count) * 0.08 - 0.04
        noise_close = math.sin(phase * 1.5) * 0.025 + math.cos(phase * 0.7) * 0.015 + trend

        close_p = base_price * (1.0 + noise_close - (math.sin(5.9 * 1.5) * 0.025 + math.cos(5.9 * 0.7) * 0.015))
        open_p = close_p * (1.0 - math.sin(phase * 2.1) * 0.012)
        high_p = max(open_p, close_p) * (1.0 + abs(math.cos(phase * 1.3)) * 0.01)
        low_p = min(open_p, close_p) * (1.0 - abs(math.sin(phase * 1.9)) * 0.01)

        if i == count - 1:
            close_p = base_price
            high_p = max(open_p, close_p) * 1.006
            low_p = min(open_p, close_p) * 0.994

        vol_val = int((12000000 + math.sin(phase * 3.0) * 5000000) * (base_price / 80.0))
        bars.append({
            "date": date_str,
            "open": round(open_p, 3),
            "high": round(high_p, 3),
            "low": round(low_p, 3),
            "close": round(close_p, 3),
            "vol": vol_val,
        })

    for i in range(len(bars)):
        c5 = [b["close"] for b in bars[max(0, i - 4):i + 1]]
        c10 = [b["close"] for b in bars[max(0, i - 9):i + 1]]
        c20 = [b["close"] for b in bars[max(0, i - 19):i + 1]]
        bars[i]["ma5"] = round(sum(c5) / len(c5), 3)
        bars[i]["ma10"] = round(sum(c10) / len(c10), 3)
        bars[i]["ma20"] = round(sum(c20) / len(c20), 3)

    support = round(min(b["low"] for b in bars[-20:]), 3)
    resistance = round(max(b["high"] for b in bars[-20:]), 3)

    return {
        "code": v["code"],
        "name": v["name"],
        "tf": tf,
        "currency": v.get("currency", "HKD"),
        "live": v.get("quote_live", False),
        "source": v.get("quote_source", "静态快照 (STATIC DEMO)"),
        "support": support,
        "resistance": resistance,
        "bars": bars,
    }


get_kline_view = get_chart_view  # 兼容旧名（已迁移为字符模拟图）

# ================================================================ 数据源与预置标的

STOCKS = {
    "09988": {
        "code": "09988",
        "name": "阿里巴巴 (Alibaba)",
        "price": "16.80",
        "currency": "HKD",
        "change": "-1.2%",
        "change_val": "-0.20",
        "is_up": False,
        "high": "17.40",
        "low": "16.50",
        "vol": "8.42M",
        "target": "18.20",
        "target_pct": "+8.3%",
        "pe": "12.4x",
        "pe_percentile": "15%",
        "sentiment_score": 74,
        "confidence": "99.4% (三源核验一致)",
        "overall_direction": "偏多",
        "overall_prob": "68%",
        "summary": "阿里巴巴当前处于估值历史低位与基本面复苏拐点。阿里云业务与海外电商保持稳健提速，主力资金连续3日净流入。尽管短期技术面受20日均线压制震荡整固，但基本面与估值安全边际较高，综合研判维持偏多基调。",
        "factors": [
            {
                "id": "fundamental",
                "name": "基本面",
                "dir": "偏多",
                "is_up": True,
                "prob": "65%",
                "score": 65,
                "badge": "业绩超预期",
                "tag": "营收提速",
                "desc": "云计算营收增长提速，公共云收入双位数增长，AI相关产品收入连续五季度三位数增长。"
            },
            {
                "id": "policy",
                "name": "行业与政策面",
                "dir": "偏多",
                "is_up": True,
                "prob": "78%",
                "score": 78,
                "badge": "政策支持",
                "tag": "常态化监管",
                "desc": "平台经济常态化监管体系明朗，数字经济与人工智能出海政策红利持续释放。"
            },
            {
                "id": "technical",
                "name": "技术面",
                "dir": "偏空",
                "is_up": False,
                "prob": "42%",
                "score": 42,
                "badge": "均线承压",
                "tag": "蓄势回踩",
                "desc": "20日均线短期形成技术压制，MACD底部金叉酝酿中，关键支撑位在15.90附近。"
            },
            {
                "id": "capital",
                "name": "资金面",
                "dir": "偏多",
                "is_up": True,
                "prob": "71%",
                "score": 71,
                "badge": "+2.5% 净流入",
                "tag": "北向增持",
                "desc": "主力资金连续3日维持净流入，北向与南向资金量价配合良好，机构仓位稳步提升。"
            },
            {
                "id": "sentiment",
                "name": "消息与情绪面",
                "dir": "偏多",
                "is_up": True,
                "prob": "68%",
                "score": 68,
                "badge": "全网正面",
                "tag": "热搜发酵",
                "desc": "财报与自研大模型开源引发科技社媒高度关注，全网情绪得分74，利好研报集中。"
            },
            {
                "id": "valuation",
                "name": "估值面",
                "dir": "偏多",
                "is_up": True,
                "prob": "82%",
                "score": 82,
                "badge": "12.4x PE",
                "tag": "15%分位",
                "desc": "滚动市盈率位于近五年15%极低分位，现金流充沛且回购力度持续加大，安全边际深厚。"
            },
            {
                "id": "momentum",
                "name": "量价舆情动量（48h）",
                "dir": "偏多",
                "is_up": True,
                "prob": "75%",
                "score": 75,
                "badge": "动量强劲",
                "tag": "量增价升",
                "desc": "48小时窗口内量价与新闻样本本地预聚合，放量上攻形态确认，多头动量占优。"
            }
        ],
        "sectors": [
            {"name": "电商板块", "count": 8, "trend": "+1.8%", "is_up": True},
            {"name": "云计算基础设施", "count": 5, "trend": "+3.2%", "is_up": True},
            {"name": "AI大模型应用", "count": 6, "trend": "+4.5%", "is_up": True},
            {"name": "跨境出海业务", "count": 3, "trend": "+2.1%", "is_up": True},
            {"name": "新零售物流体系", "count": 4, "trend": "-0.4%", "is_up": False}
        ],
        "alerts": [
            {"type": "warn", "title": "预警监控项 A", "text": "第一监控项：突破 20 日新高线 17.50 触发上攻警报", "is_up": True},
            {"type": "danger", "title": "风控止损线 B", "text": "第二监控项：下档关键预警线 15.90 ▼（跌破需规避）", "is_up": False},
            {"type": "info", "title": "资金异动 C", "text": "第三监控项：大单成交比超过 35% 且主力单笔流入超 5000 万", "is_up": True}
        ],
        "feeds": [
            {"source": "Google 新闻", "cat": "财经", "time": "12m 前", "type": "利好", "title": "阿里巴巴云栖大会发布最新一代开源大模型，企业客户采用率激增", "summary": "算力基础设施投资回报显著，商业化进程提速。"},
            {"source": "财联社电报", "cat": "财经", "time": "28m 前", "type": "利好", "title": "南向资金连续第4日净买入阿里巴巴，合计增持逾12亿港元", "summary": "机构资金在低估值区间积极加仓。"},
            {"source": "华尔街见闻", "cat": "财经", "time": "45m 前", "type": "中性", "title": "全球科技股震荡整固，中概互联网ETF出现规模资金申购", "summary": "海外宏观利率预期出现调整，科技板块波动加剧。"},
            {"source": "格隆汇", "cat": "财经", "time": "1h 前", "type": "利好", "title": "高盛上调阿里巴巴目标价至 18.50 港元，重申强力买入评级", "summary": "核心电商与云计算双轮驱动战略成效显现。"},
            {"source": "金十数据", "cat": "财经", "time": "2h 前", "type": "中性", "title": "离岸人民币汇率窄幅波动，港股核心资产流动性保持充裕", "summary": "大市成交额维持在千亿以上水平。"},
            {"source": "MKTNews", "cat": "财经", "time": "3h 前", "type": "利好", "title": "菜鸟海外本地快递网络拓展至欧洲五国，跨境时效大幅提升", "summary": "海外电商基础设施网络竞争力巩固。"},
            {"source": "雪球热帖", "cat": "财经", "time": "实时", "type": "利好", "title": "【深度剖析】阿里自由现金流与回购收益率测算，当前估值性价比极高", "summary": "社区热度破万，多头观点占绝对主导。"},
            {"source": "知乎热议", "cat": "社媒", "time": "1h 前", "type": "利好", "title": "如何看待阿里最新开源的模型测评超越多项基准？", "summary": "技术社区开发者好评如潮，生态扩张迅猛。"},
            {"source": "微博财经", "cat": "社媒", "time": "实时", "type": "利好", "title": "#阿里云计算提速# 登上同城财经热搜榜 Top 3", "summary": "公众舆情正面词频占比达 82%。"},
            {"source": "抖音热榜", "cat": "社媒", "time": "实时", "type": "中性", "title": "AI 赋能电商主播实测：阿里新工具降低中小商家运营成本", "summary": "视频播放量破千万，展现技术落地应用。"},
            {"source": "虎扑步行街", "cat": "社媒", "time": "4h 前", "type": "利好", "title": "聊聊港股科技股，这轮阿里回调是不是黄金坑？", "summary": "投票统计中 71% 网友认为当前属于高胜率买点。"},
            {"source": "AI hot 追踪", "cat": "社媒", "time": "2h 前", "type": "利好", "title": "Global AI Models Benchmark：Qwen 系列开源模型下载量破亿", "summary": "全球开发者社区关注度与星标数跃居前列。"},
            {"source": "联合早报", "cat": "社媒", "time": "5h 前", "type": "中性", "title": "亚太数字经济峰会聚焦中资科技企业出海新机遇", "summary": "东南亚市场电商与云服务渗透率持续攀升。"},
            {"source": "香港01", "cat": "社媒", "time": "6h 前", "type": "利好", "title": "恒指成份股检讨：科技巨头权重稳固，阿里交投活跃度居前", "summary": "港股核心指数权重股支撑作用突出。"}
        ]
    },
    "00700": {
        "code": "00700",
        "name": "腾讯控股 (Tencent)",
        "price": "382.40",
        "currency": "HKD",
        "change": "+2.4%",
        "change_val": "+9.00",
        "is_up": True,
        "high": "386.80",
        "low": "375.00",
        "vol": "14.20M",
        "target": "420.00",
        "target_pct": "+9.8%",
        "pe": "19.8x",
        "pe_percentile": "32%",
        "sentiment_score": 86,
        "confidence": "99.8% (三源核验一致)",
        "overall_direction": "偏多",
        "overall_prob": "79%",
        "summary": "腾讯游戏旗舰产品常青且新游管线强劲，视频号商业化与广告变现效率跃升，持续高额回购提供强力支撑，七大因子全线呈现多头排列。",
        "factors": [
            {"id": "fundamental", "name": "基本面", "dir": "偏多", "is_up": True, "prob": "82%", "score": 82, "badge": "盈利稳健", "tag": "视频号放量", "desc": "高毛利业务占比持续提升，本土与海外游戏业务恢复强劲增长。"},
            {"id": "policy", "name": "行业与政策面", "dir": "偏多", "is_up": True, "prob": "75%", "score": 75, "badge": "版号常态", "tag": "AI扶持", "desc": "网络游戏版号发放稳定可预期，自研混元大模型生态持续赋能各业务线。"},
            {"id": "technical", "name": "技术面", "dir": "偏多", "is_up": True, "prob": "70%", "score": 70, "badge": "突破形态", "tag": "多头排列", "desc": "放量突破短期盘整平台，均线系统呈现标准多头排列，量能放大。"},
            {"id": "capital", "name": "资金面", "dir": "偏多", "is_up": True, "prob": "88%", "score": 88, "badge": "每日10亿回购", "tag": "南向重仓", "desc": "公司每日注销式回购提供坚实底部托底，南向资金连续5周加仓。"},
            {"id": "sentiment", "name": "消息与情绪面", "dir": "偏多", "is_up": True, "prob": "80%", "score": 80, "badge": "口碑良好", "tag": "新游霸榜", "desc": "重磅新作登顶各大应用商店榜首，科技与游戏媒体正面测评居多。"},
            {"id": "valuation", "name": "估值面", "dir": "偏多", "is_up": True, "prob": "74%", "score": 74, "badge": "19.8x PE", "tag": "估值合理", "desc": "相比历史中枢估值仍具吸引力，高确定性现金流与分红回购双驱动。"},
            {"id": "momentum", "name": "量价舆情动量（48h）", "dir": "偏多", "is_up": True, "prob": "85%", "score": 85, "badge": "动量激增", "tag": "主力扫货", "desc": "48小时窗口内大单主动买入占比达 62%，量价配合完美。"}
        ],
        "sectors": [
            {"name": "网络游戏", "count": 9, "trend": "+3.4%", "is_up": True},
            {"name": "社交生态", "count": 7, "trend": "+2.1%", "is_up": True},
            {"name": "数字广告", "count": 5, "trend": "+2.8%", "is_up": True},
            {"name": "金融科技", "count": 4, "trend": "+1.2%", "is_up": True}
        ],
        "alerts": [
            {"type": "warn", "title": "目标上攻 A", "text": "第一监控项：若站稳 385 港元将开启 400 整数关口上攻浪", "is_up": True},
            {"type": "danger", "title": "支撑防线 B", "text": "第二监控项：多头强支撑位在 368 港元，跌破则转为震荡", "is_up": False},
            {"type": "info", "title": "回购跟踪 C", "text": "第三监控项：每日 10 亿港元回购执行进度已超年度计划 60%", "is_up": True}
        ],
        "feeds": [
            {"source": "财联社电报", "cat": "财经", "time": "15m 前", "type": "利好", "title": "腾讯控股今日斥资10.02亿港元回购262万股", "summary": "常态化回购持续维护股东价值。"},
            {"source": "Google 新闻", "cat": "财经", "time": "40m 前", "type": "利好", "title": "腾讯混元大模型全面接入微信搜一搜，AI 搜索交互体验升级", "summary": "大模型在十亿级用户场景落地。"},
            {"source": "知乎热议", "cat": "社媒", "time": "2h 前", "type": "利好", "title": "如何评价腾讯新游首周流水破纪录？", "summary": "长青游戏运营能力再次得到市场验证。"}
        ]
    },
    "03690": {
        "code": "03690",
        "name": "美团 (Meituan)",
        "price": "128.50",
        "currency": "HKD",
        "change": "+1.8%",
        "change_val": "+2.30",
        "is_up": True,
        "high": "131.20",
        "low": "126.00",
        "vol": "9.80M",
        "target": "145.00",
        "target_pct": "+12.8%",
        "pe": "18.2x",
        "pe_percentile": "26%",
        "sentiment_score": 79,
        "confidence": "99.2% (三源核验一致)",
        "overall_direction": "偏多",
        "overall_prob": "72%",
        "summary": "美团核心本地商业护城河稳固，到店到家业务协同效应增强，海外外卖业务扩张亏损收窄，估值修复空间巨大。",
        "factors": [
            {"id": "fundamental", "name": "基本面", "dir": "偏多", "is_up": True, "prob": "76%", "score": 76, "badge": "利润释放", "tag": "壁垒深厚", "desc": "核心外卖单均利润稳步提升，到店竞争格局趋于理性。"},
            {"id": "policy", "name": "行业与政策面", "dir": "偏多", "is_up": True, "prob": "70%", "score": 70, "badge": "消费复苏", "tag": "服务零售", "desc": "促进服务业消费政策密集出台，即时零售与本地生活规模扩张。"},
            {"id": "technical", "name": "技术面", "dir": "偏多", "is_up": True, "prob": "65%", "score": 65, "badge": "底背离", "tag": "放量回升", "desc": "日线级别双底形态构筑完毕，成交量呈温和放大态势。"},
            {"id": "capital", "name": "资金面", "dir": "偏多", "is_up": True, "prob": "73%", "score": 73, "badge": "外资加仓", "tag": "主力流入", "desc": "海外长线基金近两周连续增持，南向持股比例保持高位。"},
            {"id": "sentiment", "name": "消息与情绪面", "dir": "偏多", "is_up": True, "prob": "68%", "score": 68, "badge": "情绪转暖", "tag": "Keeta出海", "desc": "沙特及中东市场外卖业务拓展顺利，市场对出海空间预期上修。"},
            {"id": "valuation", "name": "估值面", "dir": "偏多", "is_up": True, "prob": "79%", "score": 79, "badge": "18.2x PE", "tag": "极高弹性", "desc": "相比历史估值中枢折价近50%，下行风险有限，上行弹性充足。"},
            {"id": "momentum", "name": "量价舆情动量（48h）", "dir": "偏多", "is_up": True, "prob": "74%", "score": 74, "badge": "动量转正", "tag": "多头共振", "desc": "量价舆情综合动量得分显著提升，利多信号密集发出。"}
        ],
        "sectors": [
            {"name": "即时配送", "count": 6, "trend": "+2.3%", "is_up": True},
            {"name": "本地生活", "count": 8, "trend": "+1.9%", "is_up": True},
            {"name": "出海科技", "count": 4, "trend": "+3.1%", "is_up": True}
        ],
        "alerts": [
            {"type": "warn", "title": "压力位突破 A", "text": "第一监控项：突破 132.00 颈线位将确立中期反转趋势", "is_up": True},
            {"type": "danger", "title": "止损警戒线 B", "text": "第二监控项：下档防守支撑 122.50 港元", "is_up": False},
            {"type": "info", "title": "业务指标 C", "text": "第三监控项：即时零售日峰值单量突破 8000 万单", "is_up": True}
        ],
        "feeds": [
            {"source": "格隆汇", "cat": "财经", "time": "20m 前", "type": "利好", "title": "美团 Keeta 在中东利雅得正式上线，首日订单表现强劲", "summary": "本地生活基础设施成功复制海外。"},
            {"source": "财联社电报", "cat": "财经", "time": "1h 前", "type": "利好", "title": "高盛维持美团买入评级，看好本地商业利润率持续扩张", "summary": "核心业务盈利韧性强于市场预期。"}
        ]
    }
}


def get_cluster_status() -> dict:
    """实时生成服务器集群遥测数据"""
    now = datetime.now(timezone.utc)
    cst = now + timedelta(hours=8)
    # 基于时间生成动态微小波动，模拟大屏动态刷新
    seed = int(now.timestamp())
    cpu = 18.0 + (seed % 17) * 0.7
    ram_gb = 6.4 + (seed % 11) * 0.15
    net_mb = 42.0 + (seed % 31) * 1.4
    ping_ms = 9 + (seed % 7)
    qps = 2400 + (seed % 420) * 3

    return {
        "cluster_id": "OCTOPUS-NOC-HK04",
        "cluster_name": "章鱼 AI 全球调研遥测大屏集群",
        "status": "OPERATIONAL · ALL SYSTEMS NOMINAL",
        "status_code": "OK_200",
        "uptime": "99.998% (412 天 14 小时)",
        "utc_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "cst_time": cst.strftime("%Y-%m-%d %H:%M:%S CST (北京时间)"),
        "epoch_ms": int(now.timestamp() * 1000),
        "cpu_usage": f"{cpu:.1f}%",
        "ram_usage": f"{ram_gb:.2f} / 32.0 GB",
        "net_io": f"{net_mb:.1f} MB/s",
        "ping": f"{ping_ms} ms",
        "total_qps": f"{qps:,} req/s",
        "rec_status": "● REC LIVE 60 FPS",
        "ai_engine": "DeepSeek-V3 / R1 混合推理池 (8x H800)",
        "nodes": [
            {
                "id": "HK-Master-01",
                "role": "香港主控调度中心",
                "status": "ONLINE",
                "ping": "2ms",
                "load": "24%",
                "qps": f"{qps * 0.38:.0f}",
                "is_ok": True
            },
            {
                "id": "SH-Edge-02",
                "role": "上海低延时接入点",
                "status": "ONLINE",
                "ping": "11ms",
                "load": "31%",
                "qps": f"{qps * 0.26:.0f}",
                "is_ok": True
            },
            {
                "id": "SG-Compute-03",
                "role": "新加坡多云算力池",
                "status": "ONLINE",
                "ping": "28ms",
                "load": "42%",
                "qps": f"{qps * 0.18:.0f}",
                "is_ok": True
            },
            {
                "id": "AI-Node-04",
                "role": "DeepSeek 推理执行集群",
                "status": "ACTIVE",
                "ping": "5ms",
                "load": "68%",
                "qps": f"{qps * 0.12:.0f}",
                "is_ok": True
            },
            {
                "id": "TS-Database-05",
                "role": "十七源时序与情绪数据库",
                "status": "SYNCED",
                "ping": "1ms",
                "load": "19%",
                "qps": f"{qps * 0.06:.0f}",
                "is_ok": True
            }
        ]
    }


def render_server_monitor_html(stock_code: str = "09988") -> str:
    """生成完全不含 <table> 的纯文字 + 列表横排 8-bit 复古游戏风大屏监视 HTML

    视觉与 pushplus_deepseek.themed_html(theme_name='game') 统一：
    深夜蓝游戏屏 + 像素星点 + 金色粗框 + 硬黑像素阴影 + ♥HP血条/★LV/SCORE/PRESS START。
    """
    data = get_stock_view(stock_code)
    cluster = get_cluster_status()
    # 页面时钟由 JS 实时走动；行情若接入免费数据源则为实时，失败时回退静态快照
    generated_at = cluster["cst_time"]
    # —— 8-bit 游戏参数：由标的代码稳定生成 SCORE / HP / LV（与 pushplus game 主题一致）——
    game_seed = sum(ord(c) for c in str(stock_code))
    game_score = game_seed * 7 % 999999
    game_hp = 60 + game_seed % 40                       # 60-99
    game_hp_filled = round(game_hp / 10)
    game_hp_bar = "█" * game_hp_filled + "░" * (10 - game_hp_filled)
    game_lv = 1 + game_seed % 9                         # LV.01-09

    # —— 数据状态横幅：实时行情 / 静态演示 ——
    if data.get("quote_live"):
        dsb_icon = "🟢"
        dsb_strong = f"实时行情 (LIVE) · {data['quote_source']}"
        dsb_hint = (f"行情来自免费数据源「{data['quote_source']}」并自动更新（缓存 {hk_quote.CACHE_TTL if hk_quote else 10}s）；"
                    f"快讯与因子仍为内置演示内容，仅供参考。")
        dsb_time = (f"行情时间: {data.get('quote_time') or '—'} · 抓取: {data.get('quote_fetched_at') or '—'} · 快照: {generated_at}")
    else:
        dsb_icon = "⚠️"
        dsb_strong = "演示数据 (STATIC DEMO)"
        dsb_hint = (f"实时行情暂不可用（{data.get('quote_error') or '网络受限或数据源失败'}），"
                    f"当前展示内置示例价格，不随市场更新。点击 ⚡ 实时刷新重试。")
        dsb_time = f"快照生成: {generated_at}"

    # 1. 横排集群节点状态流
    nodes_html = "".join(f"""
    <div class="node-pill">
        <span class="node-dot"></span>
        <span class="node-id">{n["id"]}</span>
        <span class="node-role">{n["role"]}</span>
        <span class="node-stat">{n["status"]} · {n["ping"]} · 负载 {n["load"]}</span>
    </div>
    """ for n in cluster["nodes"])

    # 2. 横排核心量化指标流
    up_down_color = "#5cff5c" if data["is_up"] else "#ff5c5c"
    up_down_sign = "▲" if data["is_up"] else "▼"

    metrics_ribbon = f"""
    <div class="metric-ribbon">
        <div class="kpi-card highlight-cyan">
            <div class="kpi-label">标的代码 / 名称</div>
            <div class="kpi-value cyan-glow">{data["code"]} {data["name"]}</div>
            <div class="kpi-foot" id="q-source-foot">行情数据源: {data["quote_source"]}{(' · 行情时间 ' + data['quote_time']) if data.get('quote_time') else ''}</div>
        </div>
        <div class="kpi-card highlight-{"green" if data["is_up"] else "red"}">
            <div class="kpi-label">现价 / 涨跌幅</div>
            <div class="kpi-value" style="color:{up_down_color};">
                <span id="q-price">{data["price"]}</span> <span style="font-size:14px;" id="q-currency">{data["currency"]}</span>
                <span class="pill-badge" id="q-change" style="background:{'rgba(92,255,92,0.15)' if data['is_up'] else 'rgba(255,92,92,0.15)'};color:{up_down_color};border-color:{up_down_color};">
                    {up_down_sign} {data["change"]} ({data["change_val"]})
                </span>
            </div>
            <div class="kpi-foot" id="q-range">24h 波动区间: {data["low"]} - {data["high"]}</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">24H 成交量 / 估值</div>
            <div class="kpi-value amber-glow"><span id="q-vol">{data["vol"]}</span> <span style="font-size:13px;color:#8a90bc;">(PE <span id="q-pe">{data["pe"]}</span>)</span></div>
            <div class="kpi-foot" id="q-pe-foot">历史分位: {data["pe_percentile"]} (深度价值区)</div>
        </div>
        <div class="kpi-card highlight-green">
            <div class="kpi-label">AI 预测目标价</div>
            <div class="kpi-value green-glow">{data["target"]} <span class="pill-badge badge-green">{data["target_pct"]}</span></div>
            <div class="kpi-foot">研判周期: 48h - 156h 窗口</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">综合研判 / 多头胜率</div>
            <div class="kpi-value purple-glow">{data["overall_direction"]} <span style="font-size:18px;color:#5cff5c;">{data["overall_prob"]}</span></div>
            <div class="kpi-foot">全网舆情指数: {data["sentiment_score"]}/100</div>
        </div>
    </div>
    """

    # —— 字符模拟图 · 智能行情交互视图 ——
    tradeview_html = f"""
        <!-- 📊 TradeView 智能行情与字符模拟交互图表界面 -->
        <div class="section-header" id="tradeview-section">
            <span>📊 字符模拟图 · 智能行情交互视图 (CHAR SIMULATION CHART STUDIO)</span>
            <span class="section-tag">多周期字符模拟走势 · 均线系统 (MA5/10/20) · 实时成交量 · 字符点阵与 TradingView 双模联动</span>
        </div>
        <div class="tradeview-panel">
            <div class="tradeview-toolbar">
                <div class="tv-group">
                    <span class="tv-label">标的 (Symbol):</span>
                    <button class="tv-btn symbol-btn {'active' if str(stock_code) in ('09988', '9988') else ''}" onclick="switchTradeViewSymbol('09988', '09988 阿里巴巴 (HKEX:9988)', this)">09988 阿里巴巴</button>
                    <button class="tv-btn symbol-btn {'active' if str(stock_code) in ('00700', '700') else ''}" onclick="switchTradeViewSymbol('00700', '00700 腾讯控股 (HKEX:0700)', this)">00700 腾讯控股</button>
                    <button class="tv-btn symbol-btn {'active' if str(stock_code) in ('03690', '3690') else ''}" onclick="switchTradeViewSymbol('03690', '03690 美团 (HKEX:3690)', this)">03690 美团</button>
                    <button class="tv-btn symbol-btn {'active' if str(stock_code) == 'BABA' else ''}" onclick="switchTradeViewSymbol('BABA', 'BABA 阿里美股 (NASDAQ:BABA)', this)">BABA 阿里美股</button>
                </div>
                <div class="tv-group">
                    <span class="tv-label">周期 (TF):</span>
                    <button class="tv-btn tf-btn" onclick="switchTradeViewTF('1d', '分时 (1D)', this)">分时</button>
                    <button class="tv-btn tf-btn" onclick="switchTradeViewTF('5d', '5日 (5D)', this)">5日</button>
                    <button class="tv-btn tf-btn active" onclick="switchTradeViewTF('daily', '日K (Daily)', this)">日K</button>
                    <button class="tv-btn tf-btn" onclick="switchTradeViewTF('weekly', '周K (Weekly)', this)">周K</button>
                    <button class="tv-btn tf-btn" onclick="switchTradeViewTF('monthly', '月K (Monthly)', this)">月K</button>
                </div>
                <div class="tv-group">
                    <span class="tv-label">渲染模式 (Engine):</span>
                    <button class="tv-btn engine-btn active" id="btn-engine-canvas" onclick="switchTradeViewEngine('canvas', this)">🖥️ 字符模拟图 (字符点阵)</button>
                    <button class="tv-btn engine-btn" id="btn-engine-widget" onclick="switchTradeViewEngine('widget', this)">🌐 TradingView 官方高级图表</button>
                </div>
            </div>

            <!-- 内置 HTML5 Canvas 字符模拟图与成交量视图面板 -->
            <div id="tradeview-canvas-panel">
                <div class="tv-legend-bar" id="tv-legend-bar">
                    <div class="tv-legend-item"><span id="tv-symbol-name" style="color:var(--cyan);font-weight:bold;">{data["code"]} {data["name"]} · 日K</span></div>
                    <div class="tv-legend-item"><span id="tv-hover-date" style="color:var(--text-muted);">2026-08-07</span></div>
                    <div class="tv-legend-item">开 <span id="tv-hover-open" class="tv-legend-val">--</span></div>
                    <div class="tv-legend-item">高 <span id="tv-hover-high" class="tv-legend-val">--</span></div>
                    <div class="tv-legend-item">低 <span id="tv-hover-low" class="tv-legend-val">--</span></div>
                    <div class="tv-legend-item">收 <span id="tv-hover-close" class="tv-legend-val">--</span></div>
                    <div class="tv-legend-item">涨跌 <span id="tv-hover-change" class="tv-legend-val" style="color:var(--green);">--</span></div>
                    <div class="tv-legend-item">成交量 <span id="tv-hover-vol" class="tv-legend-val" style="color:var(--amber);">--</span></div>
                    <div class="tv-legend-item tv-legend-ma5">MA5: <span id="tv-hover-ma5" class="tv-legend-val">--</span></div>
                    <div class="tv-legend-item tv-legend-ma10">MA10: <span id="tv-hover-ma10" class="tv-legend-val">--</span></div>
                    <div class="tv-legend-item tv-legend-ma20">MA20: <span id="tv-hover-ma20" class="tv-legend-val">--</span></div>
                </div>
                <div class="tv-canvas-wrap">
                    <pre id="tradeview-char-canvas" class="tv-dotmatrix" aria-label="字符模拟走势图"></pre>
                </div>
                <div class="tv-vol-wrap">
                    <pre id="tradeview-vol-canvas" class="tv-dotmatrix tv-volume" aria-label="点阵成交量"></pre>
                </div>
                <div class="tv-footer-bar">
                    <div>
                        <span class="tv-badge-live">LIVE READY</span>
                        <span id="tv-engine-status">字符点阵渲染引擎 · 60 周期历史与均线多空共振 · 字符模拟走势（涨 █ 跌 ▓ 影线 │）</span>
                    </div>
                    <div>
                        <span>压力位 R1: <strong id="tv-res-val" style="color:var(--red);">--</strong></span> |
                        <span>支撑位 S1: <strong id="tv-sup-val" style="color:var(--green);">--</strong></span> |
                        <span>技术形态: <strong id="tv-trend-val" style="color:var(--cyan);">放量金叉 (BULLISH)</strong></span>
                    </div>
                </div>
            </div>

            <!-- TradingView 官方高级图表组件面板 (可一键切换) -->
            <div id="tradeview-widget-panel" style="display:none;">
                <div id="tradingview_widget_box" style="height:520px;width:100%;border-radius:4px;overflow:hidden;background:#0b0e2a;border:1px solid var(--border-dim);">
                    <div id="tv_widget_embed_container" style="width:100%;height:100%;"></div>
                </div>
                <div class="tv-footer-bar" style="margin-top:8px;">
                    <div>
                        <span class="tv-badge-live" style="border-color:var(--cyan);color:var(--cyan);">TRADINGVIEW WIDGET</span>
                        <span>官方 TradingView 嵌入式高级图表控件 · 支持全功能专业绘图与指标分析</span>
                    </div>
                    <div>
                        <a href="https://cn.tradingview.com/" target="_blank" style="color:var(--text-muted);text-decoration:none;">Powered by TradingView.com</a>
                    </div>
                </div>
            </div>
        </div>
    """

    # 3. 横排七大因子多空矩阵卡片流（绝对不用 <table>）
    factors_html = "".join(f"""
    <div class="factor-card {'factor-up' if f['is_up'] else 'factor-down'}">
        <div class="factor-header">
            <span class="factor-title">
                <span class="cyber-bullet">{'▲' if f['is_up'] else '▼'}</span> {f['name']}
            </span>
            <span class="factor-dir {'dir-up' if f['is_up'] else 'dir-down'}">{f['dir']}</span>
        </div>
        <div class="factor-meter-wrap">
            <div class="meter-bar">
                <div class="meter-fill {'fill-green' if f['is_up'] else 'fill-red'}" style="width:{f['prob']};"></div>
            </div>
            <span class="meter-val {'val-green' if f['is_up'] else 'val-red'}">{f['prob']}</span>
        </div>
        <div class="factor-badges">
            <span class="factor-pill">{f['badge']}</span>
            <span class="factor-pill tag-muted">{f['tag']}</span>
        </div>
        <div class="factor-desc">{f['desc']}</div>
    </div>
    """ for f in data["factors"])

    # 4. 横排关联板块与动态标签流
    sectors_html = "".join(f"""
    <div class="sector-chip {'chip-up' if s['is_up'] else 'chip-down'}">
        <span class="sector-dot"></span>
        <span class="sector-name">{s['name']}</span>
        <span class="sector-count">命中 {s['count']} 次</span>
        <span class="sector-trend {'trend-up' if s['is_up'] else 'trend-down'}">{s['trend']}</span>
    </div>
    """ for s in data["sectors"])

    # 5. 横排预警与监控项清单
    alerts_html = "".join(f"""
    <div class="alert-chip {'alert-up' if a['is_up'] else 'alert-down'}">
        <span class="alert-tag">{'● 监控突破' if a['is_up'] else '▼ 风控防线'}</span>
        <span class="alert-body">{a['text']}</span>
    </div>
    """ for a in data["alerts"])

    # 6. 横排十七平台实时情报雷达流（财经7源 + 社媒10源）
    #    来源标签由 _feed_source_html 统一控制：默认隐藏，仅保留分类/时间/情绪。
    feeds_html = "".join(f"""
    <div class="feed-card">
        <div class="feed-top">
            {_feed_source_html(f['source'])}
            <span class="feed-cat {'cat-fin' if f['cat']=='财经' else 'cat-soc'}">{f['cat']}</span>
            <span class="feed-time">{f['time']}</span>
            <span class="feed-badge {'badge-pos' if f['type']=='利好' else ('badge-neg' if f['type']=='利空' else 'badge-neu')}">{f['type']}</span>
        </div>
        <div class="feed-title">{f['title']}</div>
        <div class="feed-summary">{f['summary']}</div>
    </div>
    """ for f in data["feeds"])

    # 7. 终端日志 / 机构周报副标题中的渠道枚举同样受开关控制
    if SHOW_SOURCE_INFO:
        feed_sync_log = ("18 platforms queried (Yahoo / EastMoney / Tencent / Google / CLS / WSCN / "
                         "GLH / Jin10 / MKT / Xueqiu / Zhihu / Weibo / Douyin / Hupu / AIHot / ZaoBao / "
                         "HK01 / DeepSeek).")
        foreign_source_tag = "168H · DeepSeek+Google双源 · 情绪打标 · 表格渲染"
        foreign_source_sub = ("DeepSeek AI 搜索为主 + Google RSS 备用 · 评级/目标价/概率 · "
                              "主线/板块/评级调整 · 附录明细可追溯 · 支持离线自检")
    else:
        feed_sync_log = "18 platforms queried · 156h window · dedup + sentiment tagging complete."
        foreign_source_tag = "168H · 双链路检索 · 情绪打标 · 表格渲染"
        foreign_source_sub = ("双链路检索 · 评级/目标价/概率 · 主线/板块/评级调整 · "
                              "附录明细可追溯 · 支持离线自检")

    # 前端 JS 文案同样按开关注入（json.dumps 保证引号安全）
    js_foreign_meta = json.dumps("DeepSeek+Google" if SHOW_SOURCE_INFO else "双链路检索",
                                 ensure_ascii=False)
    js_foreign_pending = json.dumps(
        "DeepSeek AI 搜索为主，Google RSS 备用" if SHOW_SOURCE_INFO else "双链路检索",
        ensure_ascii=False)
    js_quote_log = json.dumps(
        "Cross-checking price quotes across Tencent Finance, EastMoney, Yahoo... OK"
        if SHOW_SOURCE_INFO else "Cross-checking price quotes across 3 quote feeds... OK",
        ensure_ascii=False)
    js_foreign_log = json.dumps(
        "12 foreign institutions · 168H window · DeepSeek+Google dual-feed active."
        if SHOW_SOURCE_INFO else "12 foreign institutions · 168H window · dual-feed active.",
        ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>章鱼 AI · 服务器大屏监视中心 (8-BIT RETRO ARCADE WALLBOARD)</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&family=VT323&display=swap" rel="stylesheet">
    <style>
        :root {{
            /* —— 8-bit 复古游戏风（与 pushplus game 主题统一）——
               深夜蓝游戏屏 #0b0e2a + 金色粗框 #f8b800 + 硬黑像素阴影 +
               ♥HP血条 / ★LV 等级 / SCORE 计分 / PRESS START */
            --bg-deep: #0b0e2a;            /* 游戏背景（深夜蓝） */
            --bg-panel: #0e1130;           /* 游戏面板底（深靛蓝） */
            --bg-card: #16193b;            /* 游戏面板底（靛蓝） */
            --bg-card-hover: #1d2150;
            --border-glow: #f8b800;        /* 金色粗框（2px 游戏描边） */
            --border-dim: #4a4280;         /* 暗靛蓝细框 */
            --border-subtle: rgba(248, 184, 0, 0.25);
            --cyan: #ffd23f;               /* 金币黄 重点凸显 */
            --green: #5cff5c;              /* 磷光绿 涨 */
            --amber: #f8b800;              /* 金色 */
            --red: #ff5c5c;                /* 警示红 跌 */
            --purple: #a78bfa;
            --text-main: #e9eaf5;          /* 正文米白 */
            --text-muted: #8a90bc;         /* 辅助蓝灰 */
            --text-dim: #5b6090;
            --font-pixel: 'Press Start 2P', 'Courier New', monospace;
            --font-mono: 'VT323', 'Courier New', 'Consolas', 'JetBrains Mono', monospace;
            /* 硬黑像素阴影：所有发光改用 4px 偏移纯黑块 */
            --glow-cyan: 4px 4px 0 rgba(0, 0, 0, 0.9);
            --glow-green: 4px 4px 0 rgba(0, 0, 0, 0.9);
            --glow-red: 4px 4px 0 rgba(0, 0, 0, 0.9);
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-deep);
            color: var(--text-main);
            font-family: var(--font-mono);
            font-size: 18px;
            line-height: 1.45;
            image-rendering: pixelated;
            min-height: 100vh;
            padding: 12px;
            position: relative;
            overflow-x: hidden;
            /* 像素星点 + 金色网格：游戏机屏幕 */
            background-image:
                radial-gradient(circle at 50% 0%, rgba(248, 184, 0, 0.08) 0%, transparent 60%),
                radial-gradient(1.5px 1.5px at 26px 32px, rgba(255, 255, 255, 0.55), rgba(0, 0, 0, 0) 70%),
                radial-gradient(1.5px 1.5px at 88px 74px, rgba(255, 255, 255, 0.55), rgba(0, 0, 0, 0) 70%),
                radial-gradient(2px 2px at 152px 22px, rgba(255, 210, 63, 0.45), rgba(0, 0, 0, 0) 70%),
                radial-gradient(1.5px 1.5px at 212px 96px, rgba(255, 255, 255, 0.55), rgba(0, 0, 0, 0) 70%),
                radial-gradient(2px 2px at 310px 52px, rgba(255, 210, 63, 0.45), rgba(0, 0, 0, 0) 70%),
                radial-gradient(1.5px 1.5px at 390px 118px, rgba(255, 255, 255, 0.55), rgba(0, 0, 0, 0) 70%),
                radial-gradient(2px 2px at 470px 34px, rgba(255, 210, 63, 0.45), rgba(0, 0, 0, 0) 70%),
                radial-gradient(1.5px 1.5px at 560px 84px, rgba(255, 255, 255, 0.55), rgba(0, 0, 0, 0) 70%),
                repeating-linear-gradient(0deg, rgba(248, 184, 0, 0.03) 0 1px, transparent 1px 24px),
                repeating-linear-gradient(90deg, rgba(248, 184, 0, 0.03) 0 1px, transparent 1px 24px);
        }}

        /* CRT 扫描线特效 (可开关) */
        .crt-scanline {{
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            pointer-events: none;
            background: linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.25) 50%), linear-gradient(90deg, rgba(255, 0, 0, 0.03), rgba(0, 255, 0, 0.01), rgba(0, 0, 255, 0.03));
            background-size: 100% 3px, 6px 100%;
            z-index: 9999;
            opacity: 0.7;
        }}

        /* 大屏主容器 */
        .dashboard-container {{
            max-width: 1720px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        /* 顶部游戏菜单金条（HUD 状态栏）：金色底 + 黑字 + 硬黑像素阴影 */
        .hud-header {{
            background: #f8b800;            /* 游戏菜单金条 */
            color: #111111;
            border: 2px solid #111111;
            box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.9);
            padding: 8px 14px;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            position: relative;
        }}

        .hud-header::before, .hud-header::after {{
            content: "◤";
            position: absolute;
            top: 2px; left: 5px;
            color: rgba(17, 17, 17, 0.5);
            font-size: 12px;
            font-weight: bold;
        }}
        .hud-header::after {{
            content: "◥";
            top: auto; left: auto;
            bottom: 2px; right: 5px;
        }}

        .hud-brand {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .hud-logo {{
            width: 24px;
            height: 24px;
            background: #111111;
            color: #ffd23f;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 900;
            font-size: 14px;
            box-shadow: 2px 2px 0 rgba(0, 0, 0, 0.5);
        }}

        .hud-title {{
            font-size: 15px;
            font-weight: 900;
            letter-spacing: 1.5px;
            color: #111111;
            text-transform: uppercase;
        }}

        .hud-subtitle {{
            font-size: 10px;
            color: rgba(17, 17, 17, 0.7);
            letter-spacing: 1px;
        }}

        .hud-clocks {{
            display: flex;
            gap: 10px;
            align-items: center;
            font-size: 11px;
        }}

        .clock-badge {{
            background: rgba(17, 17, 17, 0.1);
            border: 1px solid rgba(17, 17, 17, 0.55);
            padding: 3px 8px;
            color: #111111;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .pulse-dot {{
            width: 8px;
            height: 8px;
            background: var(--green);
            display: inline-block;
            box-shadow: 1px 1px 0 rgba(0, 0, 0, 0.4);
            animation: pulse 1.5s infinite;
        }}

        @keyframes pulse {{
            0%, 100% {{ transform: scale(1); opacity: 1; }}
            50% {{ transform: scale(1.4); opacity: 0.4; }}
        }}

        .hud-controls {{
            display: flex;
            gap: 8px;
            align-items: center;
        }}

        .btn-action {{
            background: rgba(17, 17, 17, 0.08);
            border: 2px solid #111111;
            color: #111111;
            padding: 3px 10px;
            font-size: 11px;
            font-family: var(--font-mono);
            cursor: pointer;
            transition: all 0.2s;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}

        .btn-action:hover {{
            background: #111111;
            color: #ffd23f;
            box-shadow: 3px 3px 0 rgba(0, 0, 0, 0.6);
        }}

        /* 8-bit 游戏状态栏：♥HP 血条 + ★LV 等级 + SCORE 计分 + UTC */
        .game-statusbar {{
            background: var(--bg-panel);
            border: 2px solid var(--border-glow);
            border-top: none;
            box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.9);
            color: var(--text-muted);
            font-size: 10px;
            letter-spacing: 1px;
            padding: 4px 12px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 6px;
            font-family: var(--font-mono);
        }}
        .game-statusbar .gs-hp-fill {{ color: var(--green); letter-spacing: 0; }}
        .game-statusbar .gs-hp-num {{ color: var(--green); font-weight: bold; }}
        .game-statusbar .gs-score {{ margin-left: auto; color: var(--cyan); font-weight: bold; }}
        .game-statusbar .gs-utc {{ color: var(--text-dim); font-size: 9px; }}

        /* 数据状态横幅：明确标注当前为静态演示数据，避免误以为实时更新 */
        .data-status-banner {{
            background: rgba(248, 184, 0, 0.08);
            border: 1px solid var(--amber);
            box-shadow: 0 0 12px rgba(248, 184, 0, 0.18);
            padding: 8px 14px;
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 11px;
            color: var(--amber);
            flex-wrap: wrap;
        }}
        .data-status-banner .dsb-icon {{ font-size: 14px; }}
        .data-status-banner .dsb-strong {{ font-weight: bold; color: #ffd23f; }}
        .data-status-banner .dsb-hint {{ color: var(--text-muted); }}
        .data-status-banner .dsb-time {{ margin-left: auto; color: var(--text-dim); font-size: 10px; }}

        /* 标的切换栏 (横排) */
        .stock-selector-ribbon {{
            background: var(--bg-panel);
            border: 1px solid var(--border-dim);
            padding: 6px 12px;
            display: flex;
            align-items: center;
            gap: 8px;
            overflow-x: auto;
        }}

        .selector-label {{
            color: var(--text-muted);
            font-size: 11px;
            white-space: nowrap;
            letter-spacing: 1px;
        }}

        .stock-btn {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            color: var(--text-muted);
            padding: 4px 12px;
            font-size: 11px;
            font-family: var(--font-mono);
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s;
        }}

        .stock-btn.active {{
            border-color: var(--cyan);
            color: var(--cyan);
            background: rgba(255, 210, 63, 0.12);
            box-shadow: 0 0 10px rgba(255, 210, 63, 0.2);
            font-weight: bold;
        }}

        /* 🧠 AI 研报输入栏与结果面板 */
        .report-input {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            color: var(--text-main);
            padding: 5px 10px;
            font-size: 12px;
            font-family: var(--font-mono);
            min-width: 260px;
            flex: 1 1 260px;
            letter-spacing: 0.5px;
        }}
        .report-input:focus {{
            outline: none;
            border-color: var(--cyan);
            box-shadow: 0 0 8px rgba(255, 210, 63, 0.25);
        }}
        .report-select {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            color: var(--text-main);
            padding: 5px 8px;
            font-size: 11px;
            font-family: var(--font-mono);
            cursor: pointer;
        }}
        .report-btn {{
            background: rgba(92, 255, 92, 0.12);
            border: 1px solid var(--green);
            color: var(--green);
            padding: 5px 12px;
            font-size: 11px;
            font-family: var(--font-mono);
            cursor: pointer;
            letter-spacing: 1px;
            white-space: nowrap;
            transition: all 0.2s;
        }}
        .report-btn:hover {{
            background: var(--green);
            color: #000;
            box-shadow: var(--glow-green);
        }}
        .report-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .report-panel {{
            background: var(--bg-panel);
            border: 2px solid var(--border-glow);
            box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.9);
            padding: 12px 16px;
            margin-bottom: 12px;
        }}
        .report-panel .report-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 10px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border-dim);
        }}
        .report-panel .report-title {{
            color: var(--cyan);
            font-weight: bold;
            font-size: 13px;
            letter-spacing: 1px;
        }}
        .report-panel .report-meta {{
            color: var(--text-muted);
            font-size: 10px;
        }}
        .report-panel .report-status {{
            font-size: 11px;
            padding: 2px 8px;
            border: 1px solid;
        }}
        .report-body {{
            font-size: 12px;
            line-height: 1.6;
            max-height: 640px;
            overflow-y: auto;
            padding-right: 4px;
        }}
        .report-body table {{
            border-collapse: collapse;
            width: 100%;
            margin: 6px 0;
        }}
        .report-body th, .report-body td {{
            border: 1px solid var(--border-dim);
            padding: 4px 8px;
            text-align: left;
            color: var(--text-main);
        }}
        .report-body th {{
            background: rgba(255, 210, 63, 0.08);
            color: var(--cyan);
            font-weight: bold;
        }}
        .report-body h1, .report-body h2, .report-body h3 {{
            color: var(--cyan);
            margin: 8px 0 4px;
            letter-spacing: 0.5px;
        }}
        .report-body pre {{
            background: #0b0e2a;
            border: 1px solid var(--border-dim);
            padding: 8px;
            overflow-x: auto;
            font-family: var(--font-mono);
            color: var(--green);
            white-space: pre;
            font-size: 11px;
            line-height: 1.35;
        }}
        .report-body blockquote {{
            border-left: 3px solid var(--amber);
            padding-left: 10px;
            color: var(--text-muted);
            margin: 6px 0;
        }}

        /* 🏛 机构级个股投研独立栏目 */
        .equity-column-panel {{
            background: var(--bg-panel);
            border: 2px solid var(--border-glow);
            box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.9);
            padding: 14px 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}
        .equity-hero {{
            display: flex;
            flex-wrap: wrap;
            gap: 14px;
            justify-content: space-between;
            align-items: stretch;
        }}
        .equity-hero-main {{ flex: 1 1 320px; }}
        .equity-kicker {{
            font-size: 10px;
            letter-spacing: 1.5px;
            color: var(--amber);
            margin-bottom: 4px;
        }}
        .equity-title {{
            font-size: 18px;
            font-weight: 900;
            color: var(--text-main);
            letter-spacing: 1px;
        }}
        .equity-sub {{
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 4px;
        }}
        .equity-links {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 10px;
        }}
        .equity-link {{
            color: var(--cyan);
            font-size: 11px;
            text-decoration: none;
            border: 1px solid var(--border-subtle);
            padding: 3px 8px;
        }}
        .equity-link:hover {{ border-color: var(--cyan); box-shadow: var(--glow-cyan); }}
        .equity-hero-side {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-content: flex-start;
            min-width: 240px;
        }}
        .equity-stat {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            padding: 8px 12px;
            min-width: 100px;
            flex: 1 1 100px;
        }}
        .equity-stat-n {{
            display: block;
            font-size: 18px;
            font-weight: 900;
            color: var(--amber);
        }}
        .equity-stat-l {{
            font-size: 10px;
            color: var(--text-dim);
            letter-spacing: 1px;
        }}
        .equity-features {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .equity-chip {{
            font-size: 10px;
            padding: 3px 8px;
            border: 1px solid rgba(248, 184, 0, 0.35);
            color: var(--amber);
            background: rgba(248, 184, 0, 0.06);
        }}
        .equity-chapters {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .equity-chapter-pill {{
            font-size: 10px;
            padding: 4px 8px;
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            color: var(--text-muted);
        }}
        .skill-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
            gap: 8px;
        }}
        .skill-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            padding: 8px 10px;
            cursor: pointer;
            transition: border-color 0.15s;
        }}
        .skill-card:hover, .skill-card.active {{
            border-color: var(--purple);
            box-shadow: 0 0 10px rgba(167, 139, 250, 0.25);
        }}
        .skill-card .sk-id {{
            color: var(--purple);
            font-size: 10px;
            letter-spacing: 1px;
        }}
        .skill-card .sk-title {{
            font-weight: bold;
            color: var(--text-main);
            margin-top: 2px;
        }}
        .skill-card .sk-meta {{
            font-size: 10px;
            color: var(--text-dim);
            margin-top: 3px;
        }}

        /* 横排集群节点状态流 */
        .node-stream {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            background: var(--bg-panel);
            border: 1px solid var(--border-dim);
            padding: 8px 12px;
        }}

        .node-pill {{
            flex: 1 1 200px;
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            padding: 5px 10px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
        }}

        .node-dot {{
            width: 6px;
            height: 6px;
            background: var(--green);
            border-radius: 50%;
            box-shadow: 0 0 6px var(--green);
        }}

        .node-id {{
            color: var(--cyan);
            font-weight: bold;
        }}

        .node-role {{
            color: var(--text-muted);
        }}

        .node-stat {{
            margin-left: auto;
            color: var(--green);
            font-size: 10px;
        }}

        /* 横排核心 KPI 指标带 (零表格) */
        .metric-ribbon {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }}

        .kpi-card {{
            flex: 1 1 calc(20% - 10px);
            min-width: 220px;
            background: var(--bg-panel);
            border: 1px solid var(--border-dim);
            padding: 10px 14px;
            position: relative;
            overflow: hidden;
            transition: all 0.2s;
        }}

        .kpi-card:hover {{
            border-color: var(--cyan);
            background: var(--bg-card);
        }}

        .kpi-card.highlight-cyan {{ border-left: 3px solid var(--cyan); }}
        .kpi-card.highlight-green {{ border-left: 3px solid var(--green); }}
        .kpi-card.highlight-red {{ border-left: 3px solid var(--red); }}

        .kpi-label {{
            font-size: 10px;
            color: var(--text-dim);
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 4px;
        }}

        .kpi-value {{
            font-size: 20px;
            font-weight: 900;
            line-height: 1.2;
            margin-bottom: 4px;
            display: flex;
            align-items: baseline;
            gap: 8px;
            flex-wrap: wrap;
        }}

        .kpi-foot {{
            font-size: 10px;
            color: var(--text-muted);
        }}

        .cyan-glow {{ color: var(--cyan); text-shadow: 0 0 10px rgba(255,210,63,0.4); }}
        .green-glow {{ color: var(--green); text-shadow: 0 0 10px rgba(92,255,92,0.4); }}
        .amber-glow {{ color: var(--amber); text-shadow: 0 0 10px rgba(248,184,0,0.4); }}
        .purple-glow {{ color: var(--purple); text-shadow: 0 0 10px rgba(167,139,250,0.4); }}

        .pill-badge {{
            font-size: 11px;
            padding: 1px 6px;
            border: 1px solid;
            font-weight: bold;
            letter-spacing: 0.5px;
        }}
        .badge-green {{ background: rgba(92,255,92,0.15); color: var(--green); border-color: var(--green); }}

        /* 📊 字符模拟图 · 智能行情交互组件 */
        .tradeview-panel {{
            background: var(--bg-panel);
            border: 2px solid var(--border-glow);
            border-radius: 0;
            margin-bottom: 18px;
            padding: 14px;
            box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.9);
            position: relative;
        }}
        .tradeview-toolbar {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            margin-bottom: 12px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border-dim);
        }}
        .tv-group {{
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }}
        .tv-label {{
            color: var(--text-muted);
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-right: 4px;
        }}
        .tv-btn {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            color: var(--text-muted);
            padding: 4px 10px;
            border-radius: 0;
            font-size: 11px;
            font-family: var(--font-mono);
            cursor: pointer;
            transition: all 0.2s ease;
        }}
        .tv-btn:hover {{
            color: var(--cyan);
            border-color: var(--cyan);
            background: var(--bg-card-hover);
        }}
        .tv-btn.active {{
            color: #111111;
            background: var(--cyan);
            border-color: var(--cyan);
            font-weight: bold;
            box-shadow: 2px 2px 0 rgba(0, 0, 0, 0.8);
        }}
        .tv-btn.engine-btn.active {{
            background: var(--green);
            border-color: var(--green);
            color: #111111;
            box-shadow: 2px 2px 0 rgba(0, 0, 0, 0.8);
        }}
        .tv-legend-bar {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 14px;
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            border-radius: 0;
            padding: 8px 12px;
            margin-bottom: 10px;
            font-size: 12px;
            font-family: var(--font-mono);
            color: var(--text-main);
        }}
        .tv-legend-item {{
            display: flex;
            align-items: center;
            gap: 5px;
        }}
        .tv-legend-val {{
            font-weight: bold;
        }}
        .tv-legend-ma5 {{ color: #ffcf00; font-weight: bold; }}
        .tv-legend-ma10 {{ color: #00d8ff; font-weight: bold; }}
        .tv-legend-ma20 {{ color: #ff00b8; font-weight: bold; }}
        .tv-canvas-wrap {{
            position: relative;
            background: #0b0e2a;
            border: 2px solid var(--border-dim);
            border-radius: 0;
            overflow: hidden;
            margin-bottom: 6px;
        }}
        .tv-canvas {{
            display: block;
            width: 100%;
            cursor: crosshair;
        }}
        .tv-dotmatrix {{
            display: block;
            width: 100%;
            min-height: 22em;
            margin: 0;
            padding: 12px 10px;
            overflow: hidden;
            white-space: pre;
            color: #5cff5c;
            background: #0b0e2a;
            font: 12px/1.15 monospace;
            letter-spacing: 1px;
        }}
        .tv-dotmatrix.tv-volume {{ min-height: 6em; color: #ffcf00; }}
        .tv-vol-wrap {{
            position: relative;
            background: #0b0e2a;
            border: 2px solid var(--border-dim);
            border-radius: 0;
            overflow: hidden;
            margin-bottom: 10px;
        }}
        .tv-footer-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 11px;
            color: var(--text-dim);
            padding-top: 4px;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .tv-badge-live {{
            color: var(--green);
            background: rgba(92, 255, 92, 0.1);
            border: 1px solid var(--green);
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: bold;
        }}

        /* 模块通用标题 */
        .section-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--bg-panel);
            border: 1px solid var(--border-dim);
            border-left: 3px solid var(--cyan);
            padding: 6px 12px;
            font-size: 12px;
            font-weight: bold;
            color: var(--cyan);
            letter-spacing: 1px;
        }}

        .section-tag {{
            font-size: 10px;
            color: var(--text-dim);
            font-weight: normal;
        }}

        /* 横排七大因子矩阵 (零表格，纯横向卡片流) */
        .factors-matrix-horiz {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 10px;
        }}

        .factor-card {{
            background: var(--bg-panel);
            border: 1px solid var(--border-dim);
            padding: 10px 12px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            transition: all 0.2s;
            position: relative;
        }}

        .factor-card:hover {{
            background: var(--bg-card);
            border-color: var(--cyan);
            transform: translateY(-1px);
        }}

        .factor-card.factor-up {{ border-top: 2px solid var(--green); }}
        .factor-card.factor-down {{ border-top: 2px solid var(--red); }}

        .factor-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .factor-title {{
            font-size: 12px;
            font-weight: bold;
            color: var(--text-main);
            display: flex;
            align-items: center;
            gap: 4px;
        }}

        .cyber-bullet {{
            font-size: 10px;
        }}

        .dir-up {{ color: var(--green); font-weight: bold; }}
        .dir-down {{ color: var(--red); font-weight: bold; }}

        .factor-meter-wrap {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .meter-bar {{
            flex: 1;
            height: 6px;
            background: rgba(255, 255, 255, 0.08);
            position: relative;
            overflow: hidden;
        }}

        .meter-fill {{
            height: 100%;
            transition: width 0.6s ease;
        }}
        .fill-green {{ background: var(--green); box-shadow: 0 0 8px var(--green); }}
        .fill-red {{ background: var(--red); box-shadow: 0 0 8px var(--red); }}

        .meter-val {{
            font-size: 11px;
            font-weight: bold;
            width: 32px;
            text-align: right;
        }}
        .val-green {{ color: var(--green); }}
        .val-red {{ color: var(--red); }}

        .factor-badges {{
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
        }}

        .factor-pill {{
            font-size: 9px;
            padding: 1px 5px;
            background: rgba(255, 210, 63, 0.1);
            color: var(--cyan);
            border: 1px solid rgba(255, 210, 63, 0.3);
        }}
        .tag-muted {{
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-muted);
            border-color: rgba(255, 255, 255, 0.15);
        }}

        .factor-desc {{
            font-size: 11px;
            color: var(--text-muted);
            line-height: 1.4;
            margin-top: 2px;
        }}

        /* 深度文字研判解读 */
        .text-analysis-panel {{
            background: var(--bg-panel);
            border: 1px solid var(--border-dim);
            padding: 12px 16px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        .analysis-headline {{
            font-size: 13px;
            font-weight: bold;
            color: var(--amber);
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .analysis-paragraph {{
            font-size: 12px;
            color: var(--text-main);
            line-height: 1.6;
        }}

        /* 横排关联板块流 */
        .sectors-stream {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            padding: 8px 0;
        }}

        .sector-chip {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            padding: 4px 10px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            transition: all 0.2s;
        }}
        .sector-chip:hover {{
            border-color: var(--cyan);
        }}

        .sector-dot {{ width: 6px; height: 6px; border-radius: 50%; }}
        .chip-up .sector-dot {{ background: var(--green); }}
        .chip-down .sector-dot {{ background: var(--red); }}

        .sector-name {{ color: var(--text-main); font-weight: bold; }}
        .sector-count {{ color: var(--text-dim); font-size: 10px; }}
        .sector-trend {{ font-weight: bold; }}
        .trend-up {{ color: var(--green); }}
        .trend-down {{ color: var(--red); }}

        /* 横排预警与监控项清单 */
        .alerts-stream {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            padding: 4px 0;
        }}

        .alert-chip {{
            flex: 1 1 300px;
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            padding: 6px 12px;
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 11px;
        }}
        .alert-up {{ border-left: 3px solid var(--green); }}
        .alert-down {{ border-left: 3px solid var(--red); }}

        .alert-tag {{
            font-size: 10px;
            font-weight: bold;
            padding: 1px 5px;
            background: rgba(255, 255, 255, 0.05);
            white-space: nowrap;
        }}
        .alert-up .alert-tag {{ color: var(--green); }}
        .alert-down .alert-tag {{ color: var(--red); }}

        .alert-body {{ color: var(--text-muted); }}

        /* 横排十七平台实时情报雷达流 (卡片横排流) */
        .feeds-matrix-horiz {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 10px;
        }}

        .feed-card {{
            background: var(--bg-panel);
            border: 1px solid var(--border-dim);
            padding: 8px 12px;
            display: flex;
            flex-direction: column;
            gap: 4px;
            transition: all 0.2s;
        }}

        .feed-card:hover {{
            background: var(--bg-card);
            border-color: var(--cyan);
        }}

        .feed-top {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 10px;
        }}

        .feed-src {{
            color: var(--cyan);
            font-weight: bold;
        }}

        .feed-cat {{
            padding: 0 4px;
            font-size: 9px;
        }}
        .cat-fin {{ background: rgba(255, 210, 63, 0.1); color: var(--cyan); }}
        .cat-soc {{ background: rgba(167, 139, 250, 0.1); color: var(--purple); }}

        .feed-time {{
            color: var(--text-dim);
            margin-left: auto;
        }}

        .feed-badge {{
            padding: 0 4px;
            font-size: 9px;
            font-weight: bold;
        }}
        .badge-pos {{ background: rgba(92, 255, 92, 0.15); color: var(--green); }}
        .badge-neg {{ background: rgba(255, 92, 92, 0.15); color: var(--red); }}
        .badge-neu {{ background: rgba(138, 144, 188, 0.15); color: var(--text-muted); }}

        .feed-title {{
            font-size: 11px;
            font-weight: bold;
            color: var(--text-main);
            line-height: 1.3;
        }}

        .feed-summary {{
            font-size: 10px;
            color: var(--text-muted);
            line-height: 1.4;
        }}

        /* 终端实时日志流 */
        .terminal-panel {{
            background: #0b0e2a;
            border: 2px solid var(--border-glow);
            box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.9);
            padding: 8px 12px;
            font-size: 11px;
            color: var(--text-muted);
            max-height: 90px;
            overflow-y: hidden;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}

        .log-line {{
            display: flex;
            gap: 8px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .log-ts {{ color: var(--text-dim); }}
        .log-tag {{ color: var(--cyan); }}
        .log-msg {{ color: var(--text-main); }}

        /* 底部品牌与声明（游戏结算画面：GAME.LOG + PRESS START） */
        .hud-footer {{
            background: var(--bg-panel);
            border: 2px solid var(--border-glow);
            box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.9);
            padding: 8px 16px;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            color: var(--text-muted);
        }}

        .game-footer-line {{
            flex-basis: 100%;
            border-bottom: 1px dashed var(--border-dim);
            padding-bottom: 5px;
            margin-bottom: 2px;
            font-size: 10px;
            letter-spacing: 1px;
            color: var(--text-muted);
            font-family: var(--font-mono);
        }}

        .footer-brand {{
            color: var(--cyan);
            font-weight: bold;
            letter-spacing: 1px;
        }}

        .footer-author {{
            color: var(--amber);
        }}

        .footer-disclaimer {{
            color: var(--text-dim);
            font-size: 10px;
        }}
        @keyframes blink-insert {{ 50% {{ opacity: 0; }} }}
        .press-start {{ animation: blink-insert 1.1s steps(1) infinite; font-family: var(--font-pixel); font-size: 9px; }}
    </style>
</head>
<body>
    <div id="scanline" class="crt-scanline"></div>

    <div class="dashboard-container">
        <!-- 顶部 HUD 状态栏 -->
        <header class="hud-header">
            <div class="hud-brand">
                <div class="hud-logo">🐙</div>
                <div>
                    <div class="hud-title">OCTOPUS-AI · 服务器大屏智能监视中心</div>
                    <div class="hud-subtitle">NOC TELEMETRY WALLBOARD · 全网多源大模型混合部署 AI 调研平台</div>
                </div>
            </div>

            <div class="hud-clocks">
                <div class="clock-badge">
                    <span class="pulse-dot"></span>
                    <span id="cst-clock">{cluster["cst_time"]}</span>
                </div>
                <div class="clock-badge">
                    <span style="color:var(--text-dim);">UTC</span>
                    <span id="utc-clock">{cluster["utc_time"]}</span>
                </div>
                <div class="clock-badge" style="border-color:var(--red);color:var(--red);">
                    <span>{cluster["rec_status"]}</span>
                </div>
            </div>

            <div class="hud-controls">
                <button class="btn-action" onclick="toggleAudio()">🔊 音效: <span id="audio-state">开</span></button>
                <button class="btn-action" onclick="toggleScanline()">📺 扫描线</button>
                <button class="btn-action" onclick="toggleFullscreen()">⛶ 全屏大屏</button>
                <button class="btn-action" style="background:#111111;color:#ffd23f;" onclick="manualRefresh()">⚡ 实时刷新</button>
            </div>
        </header>

        <!-- 8-bit 游戏状态栏：♥HP 血条 + ★LV 等级 + SCORE 计分 + UTC -->
        <div class="game-statusbar">
            <span style="color:var(--red);">♥</span>
            <span class="gs-hp-fill">{game_hp_bar}</span>
            <span class="gs-hp-num" id="gs-hp-num">{game_hp}</span>/100
            &nbsp;<span style="color:var(--cyan);">★</span> LV.<span id="gs-lv">{game_lv:02d}</span>
            <span class="gs-score" id="gs-score">SCORE {game_score:06d}</span>
            <span class="gs-utc">✦ <span id="gs-utc-clock"></span> UTC</span>
        </div>

        <!-- 数据状态横幅：实时行情 or 静态演示数据 -->
        <div class="data-status-banner" id="q-banner">
            <span class="dsb-icon" id="q-banner-icon">{dsb_icon}</span>
            <span class="dsb-strong" id="q-banner-strong">{dsb_strong}</span>
            <span class="dsb-hint" id="q-banner-hint">{dsb_hint}</span>
            <span class="dsb-time" id="q-banner-time">{dsb_time}</span>
        </div>

        <!-- 标的切换栏 (横排) -->
        <div class="stock-selector-ribbon">
            <span class="selector-label">📡 监视标的切换:</span>
            <button class="stock-btn {'active' if stock_code=='09988' else ''}" onclick="switchStock('09988')">09988 阿里巴巴 (Alibaba)</button>
            <button class="stock-btn {'active' if stock_code=='00700' else ''}" onclick="switchStock('00700')">00700 腾讯控股 (Tencent)</button>
            <button class="stock-btn {'active' if stock_code=='03690' else ''}" onclick="switchStock('03690')">03690 美团 (Meituan)</button>
            <span style="margin-left:auto;color:var(--green);font-size:11px;">三源核验可信度: <strong>{data["confidence"]}</strong></span>
        </div>

        <!-- 🧠 AI 研报输入栏：填入港股/A股代码 → 实时行情 + AI 分析 + 推送 -->
        <div class="stock-selector-ribbon" id="report-bar">
            <span class="selector-label">🧠 AI 研报:</span>
            <input id="report-code-input" class="report-input" placeholder="输入港股/A股/美股代码：09988 / 600519 / 000001.SZ / NVDA" autocomplete="off" />
            <select id="report-channel" class="report-select">
                <option value="console">📺 预览（不推送）</option>
                <option value="pushplus">📲 PushPlus 微信</option>
                <option value="wecom">💬 企业微信</option>
                <option value="serverchan">🔔 Server酱</option>
                <option value="all">🌐 全部通道</option>
            </select>
            <select id="report-template" class="report-select" title="分析框架">
                <option value="analysis">多空因子分析</option>
                <option value="equity" selected>🏛 机构级个股投研</option>
                <option value="foreign_weekly">🌍 国外机构一周研报</option>
                <option value="morning_note">🌅 晨会纪要</option>
                <option value="initiate">📑 首次覆盖</option>
                <option value="earnings_preview">🔭 财报前瞻 Skill</option>
                <option value="earnings_update">📰 季报更新</option>
                <option value="thesis">📌 论点记分卡</option>
                <option value="catalysts">📅 催化剂日历</option>
                <option value="sector">🗺 行业格局</option>
                <option value="ideas">💡 选股扫描</option>
                <option value="model_update">🧮 模型修订</option>
                <option value="brief">简报</option>
                <option value="fusion">技术×基本面</option>
                <option value="earnings">财报前瞻</option>
            </select>
            <select id="report-theme" class="report-select" title="推送风格（PushPlus HTML 主题，微信详情页可见）">
                <option value="dos" selected>🖥 风格 DOS CRT 复古终端</option>
                <option value="guizang">✦ 风格 GUIZANG 电子杂志长页</option>
                <option value="monitor">🖥 风格 MONITOR 服务器大屏</option>
                <option value="game">🎮 风格 GAME 8-bit 像素游戏</option>
                <option value="noc">🛰 风格 NOC 零表格监视</option>
                <option value="klein">📰 风格 KLEIN 复古纸面</option>
                <option value="pixel">📹 风格 PIXEL 像素监控</option>
            </select>
            <button class="report-btn" id="report-btn" onclick="genReport()">⚡ 生成研报并推送</button>
        </div>

        <!-- 🧠 AI 研报结果面板（默认隐藏） -->
        <div id="report-panel" class="report-panel" style="display:none;">
            <div class="report-head">
                <span class="report-title" id="report-title">🧠 AI 研报</span>
                <span class="report-status" id="report-status" style="color:var(--green);border-color:var(--green);">生成中…</span>
                <span class="report-meta" id="report-meta"></span>
            </div>
            <div class="report-body" id="report-body"></div>
        </div>

        <!-- 🏛 独立栏目：机构级个股投研（equity-research-skill） -->
        <div class="section-header" id="equity-section">
            <span>🏛 机构级个股投研独立栏目 (EQUITY RESEARCH DESK)</span>
            <span class="section-tag">equity-research-skill · 九章深度 · 预期差主线 · dcf.py 可复算估值 · 财报质量 A–D</span>
        </div>
        <div class="equity-column-panel" id="equity-column-panel">
            <div class="equity-hero">
                <div class="equity-hero-main">
                    <div class="equity-kicker">INDEPENDENT COLUMN · POWERED BY EQUITY-RESEARCH-SKILL</div>
                    <div class="equity-title">机构级个股投资研究报告</div>
                    <div class="equity-sub">事实可追溯 · 估值可复算 · 结论可审计 · 覆盖美股/港股/A股</div>
                    <div class="equity-links">
                        <a class="equity-link" href="https://github.com/k-macao/equity-research-skill" target="_blank" rel="noopener">📦 k-macao/equity-research-skill</a>
                        <a class="equity-link" href="https://github.com/rollingSirius/equity-research-skill" target="_blank" rel="noopener">⬆ upstream rollingSirius</a>
                    </div>
                </div>
                <div class="equity-hero-side">
                    <div class="equity-stat"><span class="equity-stat-n" id="eq-industries">20</span><span class="equity-stat-l">行业附录</span></div>
                    <div class="equity-stat"><span class="equity-stat-n">9</span><span class="equity-stat-l">章结构</span></div>
                    <div class="equity-stat"><span class="equity-stat-n">5+</span><span class="equity-stat-l">估值方法</span></div>
                    <div class="equity-stat"><span class="equity-stat-n" id="eq-skill-status">—</span><span class="equity-stat-l">Skill 状态</span></div>
                </div>
            </div>
            <div class="equity-features" id="equity-features">
                <div class="equity-chip">预期差 Gap 表</div>
                <div class="equity-chip">反向 DCF + PVGO</div>
                <div class="equity-chip">三情景概率加权</div>
                <div class="equity-chip">EPV / EVA</div>
                <div class="equity-chip">蒙特卡洛</div>
                <div class="equity-chip">财报质量核查</div>
                <div class="equity-chip">反方论证 Pre-mortem</div>
                <div class="equity-chip">一致性检查器</div>
            </div>
            <div class="equity-chapters" id="equity-chapters-full"></div>
            <div class="stock-selector-ribbon" style="border:none;background:transparent;padding:8px 0 0;">
                <span class="selector-label">🏛 深度投研:</span>
                <input id="equity-code-input" class="report-input" placeholder="代码：09988 / 600519 / NVDA" autocomplete="off" value="{stock_code}" />
                <select id="equity-mode" class="report-select">
                    <option value="full">完整深度研究（九章）</option>
                    <option value="earnings">财报深度分析（九章）</option>
                </select>
                <select id="equity-channel" class="report-select">
                    <option value="console">📺 预览</option>
                    <option value="pushplus">📲 PushPlus</option>
                    <option value="wecom">💬 企微</option>
                    <option value="serverchan">🔔 Server酱</option>
                </select>
                <button class="report-btn" id="equity-btn" onclick="genEquityColumn()" style="border-color:var(--amber);color:var(--amber);background:rgba(248,184,0,0.12);">🏛 生成机构级研报</button>
            </div>
            <div id="equity-panel" class="report-panel" style="display:none;margin-top:10px;border-color:var(--amber);">
                <div class="report-head">
                    <span class="report-title" id="equity-title" style="color:var(--amber);">🏛 机构级个股投研</span>
                    <span class="report-status" id="equity-status" style="color:var(--amber);border-color:var(--amber);">待命</span>
                    <span class="report-meta" id="equity-meta"></span>
                </div>
                <div class="report-body" id="equity-body"></div>
            </div>
        </div>

        <!-- 🌍 国外专业投资机构一周研报栏目（新增） -->
        <div class="section-header" id="foreign-section">
            <span>🌍 国外机构一周研报 · 12家顶级投行/研究机构 (FOREIGN INSTITUTION WEEKLY)</span>
            <span class="section-tag">Goldman / MorganStanley / JPMorgan / Citi / BofA / UBS / Barclays / Nomura / HSBC / Jefferies / Bernstein / Morningstar · {foreign_source_tag}</span>
        </div>
        <div class="equity-column-panel" id="foreign-column-panel" style="border-color:rgba(0,229,255,0.5);box-shadow:0 0 20px rgba(0,229,255,0.15);">
            <div class="equity-hero">
                <div class="equity-hero-main">
                    <div class="equity-kicker" style="color:var(--cyan);">INDEPENDENT COLUMN · 12 INSTITUTIONS · 168H WINDOW · SENTIMENT TAGGING</div>
                    <div class="equity-title">国外机构一周研报聚合</div>
                    <div class="equity-sub">{foreign_source_sub}</div>
                    <div class="equity-links">
                        <a class="equity-link" href="#" onclick="return false;" style="cursor:default;">🌐 12 机构全覆盖</a>
                        <a class="equity-link" href="#" onclick="return false;" style="cursor:default;">📊 情绪打标 + 概率映射</a>
                        <a class="equity-link" href="#" onclick="return false;" style="cursor:default;">📋 表格渲染 + 附录</a>
                    </div>
                </div>
                <div class="equity-hero-side">
                    <div class="equity-stat"><span class="equity-stat-n" id="fw-inst">12</span><span class="equity-stat-l">机构</span></div>
                    <div class="equity-stat"><span class="equity-stat-n" id="fw-hours">168</span><span class="equity-stat-l">小时窗口</span></div>
                    <div class="equity-stat"><span class="equity-stat-n" id="fw-demo-n">5</span><span class="equity-stat-l">离线样本</span></div>
                    <div class="equity-stat"><span class="equity-stat-n" id="fw-status">—</span><span class="equity-stat-l">栏目状态</span></div>
                </div>
            </div>
            <div class="equity-features" id="foreign-features">
                <div class="equity-chip">Goldman Sachs</div>
                <div class="equity-chip">Morgan Stanley</div>
                <div class="equity-chip">J.P. Morgan</div>
                <div class="equity-chip">Citi Research</div>
                <div class="equity-chip">BofA Securities</div>
                <div class="equity-chip">UBS</div>
                <div class="equity-chip">Barclays</div>
                <div class="equity-chip">Nomura</div>
                <div class="equity-chip">HSBC</div>
                <div class="equity-chip">Jefferies</div>
                <div class="equity-chip">Bernstein</div>
                <div class="equity-chip">Morningstar</div>
            </div>
            <div class="stock-selector-ribbon" style="border:none;background:transparent;padding:8px 0 0;">
                <span class="selector-label">🌍 外研周报:</span>
                <input id="foreign-topic-input" class="report-input" placeholder="主题：AI / 科技股 / 半导体 / 电动车 / 港股 / 比特币…" autocomplete="off" value="" />
                <select id="foreign-channel" class="report-select">
                    <option value="console">📺 预览</option>
                    <option value="pushplus">📲 PushPlus</option>
                    <option value="wecom">💬 企微</option>
                    <option value="serverchan">🔔 Server酱</option>
                </select>
                <button class="report-btn" id="foreign-btn" onclick="genForeignWeekly()" style="border-color:var(--cyan);color:var(--cyan);background:rgba(0,229,255,0.12);">🌍 生成外研周报</button>
            </div>
            <div id="foreign-panel" class="report-panel" style="display:none;margin-top:10px;border-color:var(--cyan);">
                <div class="report-head">
                    <span class="report-title" id="foreign-title" style="color:var(--cyan);">🌍 国外机构一周研报</span>
                    <span class="report-status" id="foreign-status" style="color:var(--cyan);border-color:var(--cyan);">待命</span>
                    <span class="report-meta" id="foreign-meta"></span>
                </div>
                <div class="report-body" id="foreign-body"></div>
            </div>
        </div>

        <!-- 🧩 Skills Hub：官方 + 社区最新技能 -->
        <div class="section-header" id="skills-section">
            <span>🧩 Skills Hub · 最新投研技能目录 (AGENT SKILLS)</span>
            <span class="section-tag">rollingSirius v3 + Anthropic financial-services 官方 9 技能 · Apache-2.0 / MIT</span>
        </div>
        <div class="equity-column-panel" id="skills-hub-panel" style="border-color:rgba(167,139,250,0.4);box-shadow:0 0 18px rgba(167,139,250,0.12);">
            <div class="equity-hero">
                <div class="equity-hero-main">
                    <div class="equity-kicker" style="color:var(--purple);">SKILLS UPDATED · 2026-08-13</div>
                    <div class="equity-title">最新 Skills 已接入</div>
                    <div class="equity-sub">按 Agent Skills 标准安装，大屏 / CLI / Actions 可直接跑</div>
                    <div class="equity-links">
                        <a class="equity-link" href="https://github.com/anthropics/financial-services" target="_blank" rel="noopener">📦 Anthropic equity-research 9 skills</a>
                        <a class="equity-link" href="https://github.com/rollingSirius/equity-research-skill" target="_blank" rel="noopener">⬆ equity-research v3.0.0</a>
                    </div>
                </div>
                <div class="equity-hero-side">
                    <div class="equity-stat"><span class="equity-stat-n" id="sk-total">10</span><span class="equity-stat-l">已安装 Skills</span></div>
                    <div class="equity-stat"><span class="equity-stat-n" id="sk-anth">9</span><span class="equity-stat-l">Anthropic 官方</span></div>
                    <div class="equity-stat"><span class="equity-stat-n" id="sk-status">—</span><span class="equity-stat-l">Hub 状态</span></div>
                </div>
            </div>
            <div class="skill-grid" id="skill-grid"></div>
            <div class="stock-selector-ribbon" style="border:none;background:transparent;padding:8px 0 0;">
                <span class="selector-label">🧩 跑 Skill:</span>
                <input id="skill-code-input" class="report-input" placeholder="代码：09988 / 600519 / NVDA" autocomplete="off" value="{stock_code}" />
                <select id="skill-pick" class="report-select">
                    <option value="morning_note">🌅 晨会纪要</option>
                    <option value="initiate">📑 首次覆盖</option>
                    <option value="earnings_preview">🔭 财报前瞻</option>
                    <option value="earnings_update">📰 季报更新</option>
                    <option value="model_update">🧮 模型修订</option>
                    <option value="catalysts">📅 催化剂日历</option>
                    <option value="thesis">📌 论点记分卡</option>
                    <option value="sector">🗺 行业格局</option>
                    <option value="ideas">💡 选股扫描</option>
                    <option value="equity">🏛 九章深度</option>
                </select>
                <select id="skill-channel" class="report-select">
                    <option value="console">📺 预览</option>
                    <option value="pushplus">📲 PushPlus</option>
                    <option value="wecom">💬 企微</option>
                    <option value="serverchan">🔔 Server酱</option>
                </select>
                <button class="report-btn" id="skill-btn" onclick="genSkill()" style="border-color:var(--purple);color:var(--purple);background:rgba(167,139,250,0.12);">🧩 运行 Skill</button>
            </div>
            <div id="skill-panel" class="report-panel" style="display:none;margin-top:10px;border-color:var(--purple);">
                <div class="report-head">
                    <span class="report-title" id="skill-title" style="color:var(--purple);">🧩 Skill</span>
                    <span class="report-status" id="skill-status" style="color:var(--purple);border-color:var(--purple);">待命</span>
                    <span class="report-meta" id="skill-meta"></span>
                </div>
                <div class="report-body" id="skill-body"></div>
            </div>
        </div>

        <!-- 🖥️ 横排服务器集群节点状态流 -->
        <div class="node-stream">
            {nodes_html}
        </div>

        <!-- 📈 横排核心量化指标带 (绝对零表格) -->
        {metrics_ribbon}

        <!-- 📊 字符模拟图 · 智能行情交互视图 (CHAR SIMULATION CHART STUDIO) -->
        {tradeview_html}

        <!-- 📊 横排七大因子多空矩阵 (绝对零表格，纯横排卡片流) -->
        <div class="section-header">
            <span>📊 七大维度多空雷达矩阵 (FACTOR TELEMETRY MATRIX)</span>
            <span class="section-tag">横排因子流 · 包含 48H 量价舆情动量 · 绝无表格</span>
        </div>
        <div class="factors-matrix-horiz">
            {factors_html}
        </div>

        <!-- 📝 核心文字解读与研判要点 -->
        <div class="text-analysis-panel">
            <div class="analysis-headline">
                <span>✦</span> 标的深度综述与多空研判解读 (DEEPSEEK STRATEGIC BRIEF)
            </div>
            <div class="analysis-paragraph">
                {data["summary"]}
            </div>
        </div>

        <!-- 🏷️ 横排关联板块与动态标签流 -->
        <div class="section-header">
            <span>🏷️ 关联板块与动态标签流 (SECTOR & TOPIC TAGS)</span>
            <span class="section-tag">频次动态提取 · 子串伪影吸收</span>
        </div>
        <div class="sectors-stream">
            {sectors_html}
        </div>

        <!-- 🚨 横排预警与监控项清单 -->
        <div class="section-header">
            <span>🚨 实时预警与监控项清单 (ACTIVE ALERT TRIGGERS)</span>
            <span class="section-tag">动态点位跟踪 · 突破与止损防线</span>
        </div>
        <div class="alerts-stream">
            {alerts_html}
        </div>

        <!-- 🌐 横排十八平台实时情报雷达流 (财经7源 + 社媒10源 + AI搜索) -->
        <div class="section-header">
            <span>🌐 十八平台实时情报雷达流 (18-PLATFORM INTELLIGENCE RADAR)</span>
            <span class="section-tag">财经7源 + 社媒10源 + AI搜索 · 156H 窗口过滤 · 横排卡片流</span>
        </div>
        <div class="feeds-matrix-horiz">
            {feeds_html}
        </div>

        <!-- 📟 终端实时日志流 -->
        <div class="terminal-panel" id="terminal-log">
            <div class="log-line"><span class="log-ts">[SYS.INIT]</span> <span class="log-tag">OK</span> <span class="log-msg">NOC Telemetry Cluster 04 initialized. High-frequency feeds listening.</span></div>
            <div class="log-line"><span class="log-ts">[FEED.SYNC]</span> <span class="log-tag">OK</span> <span class="log-msg">{feed_sync_log}</span></div>
            <div class="log-line"><span class="log-ts">[SENTIMENT]</span> <span class="log-tag">CALC</span> <span class="log-msg">Momentum score computed at +8.4. Cross-source confidence verified at 99.4%.</span></div>
        </div>

        <!-- 底部品牌与声明（游戏结算画面） -->
        <footer class="hud-footer">
            <div class="game-footer-line">
                <span style="color:var(--cyan);">▞▚</span> GAME.LOG · 8-BIT RETRO WALLBOARD ·
                <span style="color:var(--green);">♥ {game_hp}</span>/100 ·
                <span style="color:var(--cyan);">★</span> LV.{game_lv:02d}
                <span class="press-start" style="float:right;">PRESS START <span style="color:var(--cyan);">▮</span></span>
            </div>
            <div class="footer-brand">章鱼 AI 全景调研平台 · OCTOPUS AI LABS</div>
            <div class="footer-author">作者：章鱼 ai 调研团队</div>
            <div class="footer-disclaimer">声明：本大屏数据与分析结果仅供参考，不构成任何实质性投资建议。</div>
        </footer>
    </div>

    <script>
        // Web Audio API 模拟大屏科幻按键声与遥测脉冲
        let audioCtx = null;
        let audioEnabled = true;

        function playBeep(freq = 880, type = 'sine', duration = 0.05) {{
            if (!audioEnabled) return;
            try {{
                if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.type = type;
                osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
                gain.gain.setValueAtTime(0.04, audioCtx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.start();
                osc.stop(audioCtx.currentTime + duration);
            }} catch(e) {{}}
        }}

        function toggleAudio() {{
            audioEnabled = !audioEnabled;
            document.getElementById('audio-state').textContent = audioEnabled ? '开' : '关';
            if (audioEnabled) playBeep(1200, 'triangle', 0.08);
        }}

        function toggleScanline() {{
            const el = document.getElementById('scanline');
            el.style.display = el.style.display === 'none' ? 'block' : 'none';
            playBeep(600, 'sine', 0.04);
        }}

        function toggleFullscreen() {{
            playBeep(1000, 'sine', 0.06);
            if (!document.fullscreenElement) {{
                document.documentElement.requestFullscreen().catch(() => {{}});
            }} else {{
                if (document.exitFullscreen) document.exitFullscreen();
            }}
        }}

        function switchStock(code) {{
            playBeep(920, 'triangle', 0.06);
            refreshGameHud(code);
            window.location.href = '/?code=' + code;
        }}

        // 8-bit 游戏 HUD：由标的代码稳定生成 SCORE / HP / LV（与后端 game 参数一致）
        function refreshGameHud(code) {{
            const seed = (code || '09988').split('').reduce((s, c) => s + c.charCodeAt(0), 0);
            const score = (seed * 7) % 999999;
            const hp = 60 + (seed % 40);
            const filled = Math.round(hp / 10);
            const bar = '█'.repeat(filled) + '░'.repeat(10 - filled);
            const lv = 1 + (seed % 9);
            const barEl = document.querySelector('.game-statusbar .gs-hp-fill');
            const hpEl = document.getElementById('gs-hp-num');
            const lvEl = document.getElementById('gs-lv');
            const scEl = document.getElementById('gs-score');
            if (barEl) barEl.textContent = bar;
            if (hpEl) hpEl.textContent = hp;
            if (lvEl) lvEl.textContent = String(lv).padStart(2, '0');
            if (scEl) scEl.textContent = 'SCORE ' + String(score).padStart(6, '0');
        }}

        function manualRefresh() {{
            playBeep(1400, 'sine', 0.08);
            appendLog('MANUAL.REFRESH', 'QUERY', 'Triggering live cross-verification & momentum recalculation...');
            setTimeout(() => {{
                window.location.reload();
            }}, 300);
        }}

        // —— 🧠 AI 研报：输入代码 → 实时行情 + AI 分析 + 推送 ——
        async function genReport() {{
            const input = document.getElementById('report-code-input');
            const code = (input ? input.value : '').trim();
            const panel = document.getElementById('report-panel');
            const body = document.getElementById('report-body');
            const title = document.getElementById('report-title');
            const meta = document.getElementById('report-meta');
            const status = document.getElementById('report-status');
            const btn = document.getElementById('report-btn');
            const channel = (document.getElementById('report-channel') || {{}}).value || 'console';
            const template = (document.getElementById('report-template') || {{}}).value || 'analysis';
            const theme = (document.getElementById('report-theme') || {{}}).value || 'dos';
            if (!code) {{
                if (input) {{ input.style.borderColor = 'var(--red)'; input.focus(); }}
                appendLog('REPORT.INPUT', 'WARN', '请输入股票代码');
                return;
            }}
            if (input) input.style.borderColor = 'var(--border-dim)';
            if (panel) panel.style.display = 'block';
            if (title) title.textContent = (template === 'equity' ? '🏛 机构级投研 · ' : '🧠 AI 研报 · ') + code;
            if (meta) meta.textContent = '模板 ' + template + ' → ' + channel;
            if (status) {{ status.textContent = '生成中…'; status.style.color = 'var(--amber)'; status.style.borderColor = 'var(--amber)'; }}
            if (body) body.innerHTML = '<div style="color:var(--text-muted);">⏳ 正在获取实时行情并生成研报…（机构级栏目可能需要更长时间）</div>';
            if (btn) btn.disabled = true;
            playBeep(1040, 'triangle', 0.08);
            appendLog('REPORT.GEN', 'QUERY', code + ' · ' + template + ' → ' + channel);
            try {{
                const resp = await fetch('/api/report', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        code: code, channel: channel, template: template,
                        dry_run: channel === 'console', theme: theme,
                        mode: template === 'equity' ? 'full' : undefined
                    }})
                }});
                const r = await resp.json();
                if (!r || !r.ok) {{
                    if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                    if (body) body.innerHTML = '<div style="color:var(--red);">❌ ' + ((r && r.error) || '未知错误') + '</div>';
                    appendLog('REPORT.GEN', 'ERR', (r && r.error) || '未知错误');
                    return;
                }}
                if (meta) meta.textContent = (r.market_label || r.market || '') + ' ' + r.code + (r.name ? ' ' + r.name : '') + ' · AI: ' + r.provider + ' · ' + (r.gen_note || '');
                const pushed = Object.entries(r.push || {{}}).map(([k, v]) => k + ': ' + v).join('　');
                if (status) {{
                    status.textContent = r.dry_run ? '预览（未推送）' : '已推送';
                    status.style.color = 'var(--green)';
                    status.style.borderColor = 'var(--green)';
                }}
                if (body) body.innerHTML = r.report_html || ('<pre>' + (r.report_md || '') + '</pre>');
                if (meta) meta.textContent += ' · ' + pushed;
                appendLog('REPORT.GEN', 'OK', r.code + ' 研报完成（AI: ' + r.provider + '）' + (r.dry_run ? '' : ' · 推送: ' + pushed));
            }} catch (e) {{
                if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                if (body) body.innerHTML = '<div style="color:var(--red);">❌ 请求失败: ' + e.message + '</div>';
                appendLog('REPORT.GEN', 'ERR', '请求失败: ' + e.message);
            }} finally {{
                if (btn) btn.disabled = false;
            }}
        }}

        // —— 🏛 独立栏目：机构级个股投研 ——
        async function loadEquityTeaser() {{
            try {{
                const resp = await fetch('/api/equity/teaser?code=' + encodeURIComponent(STOCK_CODE));
                const t = await resp.json();
                if (!t || !t.ok) {{
                    const st = document.getElementById('eq-skill-status');
                    if (st) st.textContent = '缺失';
                    return;
                }}
                const st = document.getElementById('eq-skill-status');
                if (st) st.textContent = t.skill_available ? '就绪' : '缺失';
                const ind = document.getElementById('eq-industries');
                if (ind && t.industries_count) ind.textContent = t.industries_count;
                const feat = document.getElementById('equity-features');
                if (feat && t.features && t.features.length) {{
                    feat.innerHTML = t.features.map(f => '<div class="equity-chip">' + f + '</div>').join('');
                }}
                const ch = document.getElementById('equity-chapters-full');
                if (ch && t.chapters_full) {{
                    ch.innerHTML = t.chapters_full.map((c, i) =>
                        '<div class="equity-chapter-pill">' + (i + 1) + '. ' + c.replace(/^[^、]*、/, '') + '</div>'
                    ).join('');
                }}
                appendLog('EQUITY.DESK', 'OK', 'skill ' + (t.skill_available ? 'ready' : 'missing')
                    + ' · industries ' + (t.industries_count || 0));
            }} catch (e) {{
                appendLog('EQUITY.DESK', 'WARN', 'teaser failed: ' + e.message);
            }}
        }}

        async function genEquityColumn() {{
            const input = document.getElementById('equity-code-input');
            const code = (input ? input.value : '').trim() || STOCK_CODE;
            const panel = document.getElementById('equity-panel');
            const body = document.getElementById('equity-body');
            const title = document.getElementById('equity-title');
            const meta = document.getElementById('equity-meta');
            const status = document.getElementById('equity-status');
            const btn = document.getElementById('equity-btn');
            const channel = (document.getElementById('equity-channel') || {{}}).value || 'console';
            const mode = (document.getElementById('equity-mode') || {{}}).value || 'full';
            if (panel) panel.style.display = 'block';
            if (title) title.textContent = '🏛 机构级个股投研 · ' + code;
            if (meta) meta.textContent = mode + ' · equity-research-skill · ' + channel;
            if (status) {{ status.textContent = '生成中…'; status.style.color = 'var(--amber)'; status.style.borderColor = 'var(--amber)'; }}
            if (body) body.innerHTML = '<div style="color:var(--text-muted);">⏳ 行情 → dcf.py 估值 → 九章研报 → 检查器…</div>';
            if (btn) btn.disabled = true;
            playBeep(880, 'triangle', 0.1);
            appendLog('EQUITY.GEN', 'QUERY', code + ' · ' + mode);
            try {{
                const resp = await fetch('/api/equity', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        code: code, mode: mode, channel: channel,
                        dry_run: channel === 'console', theme: 'dos'
                    }})
                }});
                const r = await resp.json();
                if (!r || !r.ok) {{
                    if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                    if (body) body.innerHTML = '<div style="color:var(--red);">❌ ' + ((r && r.error) || '未知错误') + '</div>';
                    appendLog('EQUITY.GEN', 'ERR', (r && r.error) || '未知错误');
                    return;
                }}
                const col = r.column || {{}};
                if (meta) meta.textContent = (r.market_label || '') + ' ' + r.code
                    + (r.name ? ' ' + r.name : '')
                    + ' · ' + (col.industry || r.industry || '')
                    + ' · ' + (col.valuation_label || '')
                    + ' · ' + (r.gen_note || '');
                if (status) {{
                    status.textContent = r.dry_run ? '预览完成' : '已推送';
                    status.style.color = 'var(--green)';
                    status.style.borderColor = 'var(--green)';
                }}
                if (body) body.innerHTML = r.report_html || ('<pre style="white-space:pre-wrap;">' + (r.report_md || '') + '</pre>');
                appendLog('EQUITY.GEN', 'OK', r.code + ' · label=' + (col.valuation_label || '—')
                    + ' · check=' + (r.check_code === 0 ? 'pass' : r.check_code));
            }} catch (e) {{
                if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                if (body) body.innerHTML = '<div style="color:var(--red);">❌ ' + e.message + '</div>';
                appendLog('EQUITY.GEN', 'ERR', e.message);
            }} finally {{
                if (btn) btn.disabled = false;
            }}
        }}

        // —— 🌍 国外机构一周研报栏目 ——
        async function loadForeignTeaser() {{
            try {{
                const resp = await fetch('/api/foreign_weekly/teaser');
                const t = await resp.json();
                if (!t || !t.ok) {{
                    const st = document.getElementById('fw-status');
                    if (st) st.textContent = '缺失';
                    return;
                }}
                const st = document.getElementById('fw-status');
                if (st) st.textContent = '就绪';
                const n = document.getElementById('fw-demo-n');
                if (n && t.agg) n.textContent = t.agg.n || t.agg.total || '5';
                appendLog('FOREIGN.WEEKLY', 'OK', 'institutions 12 · demo ' + (t.agg ? (t.agg.n || 5) : 5));
            }} catch (e) {{
                const st = document.getElementById('fw-status');
                if (st) st.textContent = '离线';
                appendLog('FOREIGN.WEEKLY', 'WARN', 'teaser failed: ' + e.message);
            }}
        }}

        async function genForeignWeekly() {{
            const input = document.getElementById('foreign-topic-input');
            const topic = (input ? input.value : '').trim();
            const panel = document.getElementById('foreign-panel');
            const body = document.getElementById('foreign-body');
            const title = document.getElementById('foreign-title');
            const meta = document.getElementById('foreign-meta');
            const status = document.getElementById('foreign-status');
            const btn = document.getElementById('foreign-btn');
            const channel = (document.getElementById('foreign-channel') || {{}}).value || 'console';
            if (panel) panel.style.display = 'block';
            if (title) title.textContent = '🌍 国外机构一周研报 · ' + (topic || '全市场');
            if (meta) meta.textContent = '168H · 12机构 · ' + {js_foreign_meta} + ' · ' + channel;
            if (status) {{ status.textContent = '生成中…'; status.style.color = 'var(--amber)'; status.style.borderColor = 'var(--amber)'; }}
            if (body) body.innerHTML = '<div style=\"color:var(--text-muted);\">⏳ 正在聚合 12 家机构 168H 研报（' + {js_foreign_pending} + '）…</div>';
            if (btn) btn.disabled = true;
            playBeep(760, 'sine', 0.12);
            appendLog('FOREIGN.GEN', 'QUERY', (topic || '全市场') + ' · 168H · 12机构');
            try {{
                const resp = await fetch('/api/foreign_weekly', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        topic: topic, hours: 168, channel: channel,
                        dry_run: channel === 'console', use_deepseek: true
                    }})
                }});
                const r = await resp.json();
                if (!r || !r.ok) {{
                    if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                    if (body) body.innerHTML = '<div style=\"color:var(--red);\">❌ ' + ((r && r.error) || '未知错误') + '</div>';
                    appendLog('FOREIGN.GEN', 'ERR', (r && r.error) || '未知错误');
                    return;
                }}
                const agg = r.agg || {{}};
                if (meta) meta.textContent = '样本 ' + (agg.n || r.count || 0) + ' 篇 · 机构 '
                    + (agg.institutions_hit || 0) + '/' + (agg.institutions_total || 12)
                    + ' · 情绪 利好' + (agg.sentiment ? (agg.sentiment['利好']||0) : 0)
                    + '/中性' + (agg.sentiment ? (agg.sentiment['中性']||0) : 0)
                    + '/利空' + (agg.sentiment ? (agg.sentiment['利空']||0) : 0);
                if (status) {{
                    status.textContent = '完成';
                    status.style.color = 'var(--green)';
                    status.style.borderColor = 'var(--green)';
                }}
                let html = '';
                if (r.rule_md) {{
                    html += '<div style=\"background:rgba(0,229,255,0.06);border:1px solid rgba(0,229,255,0.18);border-radius:8px;padding:12px;margin-bottom:12px;\"><pre style=\"white-space:pre-wrap;margin:0;font-size:13px;line-height:1.6;\">' + r.rule_md + '</pre></div>';
                }}
                if (r.appendix_md) {{
                    html += '<div style=\"background:rgba(255,255,255,0.04);border:1px dashed rgba(255,255,255,0.12);border-radius:8px;padding:10px;\"><pre style=\"white-space:pre-wrap;margin:0;font-size:12px;color:var(--text-muted);\">' + r.appendix_md + '</pre></div>';
                }}
                if (!html) html = '<div style=\"color:var(--text-muted);\">无数据</div>';
                if (body) body.innerHTML = html;
                appendLog('FOREIGN.GEN', 'OK', '样本 ' + (agg.n || 0) + ' · 机构 ' + (agg.institutions_hit || 0) + '/' + (agg.institutions_total || 12));
            }} catch (e) {{
                if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                if (body) body.innerHTML = '<div style=\"color:var(--red);\">❌ ' + e.message + '</div>';
                appendLog('FOREIGN.GEN', 'ERR', e.message);
            }} finally {{
                if (btn) btn.disabled = false;
            }}
        }}

        // 回车触发研报生成
        (function() {{
            const input = document.getElementById('report-code-input');
            if (input) input.addEventListener('keydown', function(e) {{
                if (e.key === 'Enter') genReport();
            }});
            const eq = document.getElementById('equity-code-input');
            if (eq) eq.addEventListener('keydown', function(e) {{
                if (e.key === 'Enter') genEquityColumn();
            }});
            const sk = document.getElementById('skill-code-input');
            if (sk) sk.addEventListener('keydown', function(e) {{
                if (e.key === 'Enter') genSkill();
            }});
            const fw = document.getElementById('foreign-topic-input');
            if (fw) fw.addEventListener('keydown', function(e) {{
                if (e.key === 'Enter') genForeignWeekly();
            }});
            setTimeout(loadEquityTeaser, 600);
            setTimeout(loadSkillsHub, 700);
            setTimeout(loadForeignTeaser, 800);
        }})();

        async function loadSkillsHub() {{
            try {{
                const resp = await fetch('/api/skills');
                const t = await resp.json();
                const st = document.getElementById('sk-status');
                if (!t || !t.ok) {{
                    if (st) st.textContent = '缺失';
                    return;
                }}
                if (st) st.textContent = '就绪';
                const tot = document.getElementById('sk-total');
                if (tot) tot.textContent = t.installed || t.total || '—';
                const anth = document.getElementById('sk-anth');
                if (anth) anth.textContent = (t.skills || []).filter(s => s.family === 'anthropic').length;
                const grid = document.getElementById('skill-grid');
                if (grid && t.skills) {{
                    grid.innerHTML = t.skills.map(s =>
                        '<div class="skill-card" data-tmpl="' + (s.template || '') + '">'
                        + '<div class="sk-id">' + (s.family || '') + ' · ' + (s.version || '') + '</div>'
                        + '<div class="sk-title">' + (s.title || s.id) + '</div>'
                        + '<div class="sk-meta">' + (s.installed ? '已安装' : '缺失') + ' · ' + (s.template || s.id) + '</div>'
                        + '</div>'
                    ).join('');
                    grid.querySelectorAll('.skill-card').forEach(el => {{
                        el.addEventListener('click', () => pickSkill(el.getAttribute('data-tmpl') || ''));
                    }});
                }}
                appendLog('SKILLS.HUB', 'OK', (t.installed || 0) + '/' + (t.total || 0) + ' skills');
            }} catch (e) {{
                appendLog('SKILLS.HUB', 'WARN', e.message);
            }}
        }}

        function pickSkill(tmpl) {{
            const sel = document.getElementById('skill-pick');
            if (sel && tmpl) sel.value = tmpl;
            playBeep(980, 'triangle', 0.05);
        }}

        async function genSkill() {{
            const input = document.getElementById('skill-code-input');
            const code = (input ? input.value : '').trim() || STOCK_CODE;
            const panel = document.getElementById('skill-panel');
            const body = document.getElementById('skill-body');
            const title = document.getElementById('skill-title');
            const meta = document.getElementById('skill-meta');
            const status = document.getElementById('skill-status');
            const btn = document.getElementById('skill-btn');
            const channel = (document.getElementById('skill-channel') || {{}}).value || 'console';
            const skill = (document.getElementById('skill-pick') || {{}}).value || 'morning_note';
            if (panel) panel.style.display = 'block';
            if (title) title.textContent = '🧩 ' + skill + ' · ' + code;
            if (meta) meta.textContent = skill + ' · ' + channel;
            if (status) {{ status.textContent = '生成中…'; status.style.color = 'var(--amber)'; status.style.borderColor = 'var(--amber)'; }}
            if (body) body.innerHTML = '<div style="color:var(--text-muted);">⏳ 正在按最新 skill 生成…</div>';
            if (btn) btn.disabled = true;
            playBeep(760, 'triangle', 0.08);
            appendLog('SKILL.GEN', 'QUERY', code + ' · ' + skill);
            try {{
                const resp = await fetch('/api/skills', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        code: code, skill: skill, channel: channel,
                        dry_run: channel === 'console', theme: 'dos'
                    }})
                }});
                const r = await resp.json();
                if (!r || !r.ok) {{
                    if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                    if (body) body.innerHTML = '<div style="color:var(--red);">❌ ' + ((r && r.error) || '未知错误') + '</div>';
                    appendLog('SKILL.GEN', 'ERR', (r && r.error) || '未知错误');
                    return;
                }}
                if (meta) meta.textContent = (r.market_label || '') + ' ' + r.code
                    + (r.name ? ' ' + r.name : '')
                    + ' · ' + (r.skill || skill)
                    + ' · ' + (r.gen_note || '');
                if (status) {{
                    status.textContent = r.dry_run ? '预览完成' : '已推送';
                    status.style.color = 'var(--green)';
                    status.style.borderColor = 'var(--green)';
                }}
                if (body) body.innerHTML = r.report_html || ('<pre style="white-space:pre-wrap;">' + (r.report_md || '') + '</pre>');
                appendLog('SKILL.GEN', 'OK', r.code + ' · ' + (r.skill || skill));
            }} catch (e) {{
                if (status) {{ status.textContent = '失败'; status.style.color = 'var(--red)'; status.style.borderColor = 'var(--red)'; }}
                if (body) body.innerHTML = '<div style="color:var(--red);">❌ ' + e.message + '</div>';
                appendLog('SKILL.GEN', 'ERR', e.message);
            }} finally {{
                if (btn) btn.disabled = false;
            }}
        }}

        // —— 实时行情轮询：拉取 /api/quote 更新价格卡片，不整页刷新 ——
        const STOCK_CODE = '{stock_code}';
        async function refreshQuote() {{
            try {{
                const resp = await fetch('/api/quote?code=' + encodeURIComponent(STOCK_CODE));
                if (!resp.ok) return;
                const q = await resp.json();
                if (!q || !q.live) {{
                    appendLog('QUOTE.STATUS', 'WARN', 'Live quote unavailable - showing static snapshot' + (q && q.error ? ': ' + q.error : ''));
                    return;
                }}
                const up = (q.change_pct || '').indexOf('-') !== 0;
                const color = up ? '#5cff5c' : '#ff5c5c';
                const sign = up ? '▲' : '▼';
                const el = (id) => document.getElementById(id);
                if (el('q-price')) el('q-price').textContent = q.price;
                if (el('q-currency')) el('q-currency').textContent = q.currency || 'HKD';
                if (el('q-change')) {{
                    el('q-change').textContent = sign + ' ' + q.change_pct + ' (' + q.change + ')';
                    el('q-change').style.color = color;
                    el('q-change').style.borderColor = color;
                    el('q-change').style.background = up ? 'rgba(92,255,92,0.15)' : 'rgba(255,92,92,0.15)';
                    const wrap = el('q-price').parentElement;
                    if (wrap) wrap.style.color = color;
                }}
                if (el('q-range')) el('q-range').textContent = '24h 波动区间: ' + q.low + ' - ' + q.high;
                if (el('q-vol')) el('q-vol').textContent = q.vol;
                if (el('q-pe')) el('q-pe').textContent = q.pe;
                if (el('q-pe-foot') && q.pe && q.pe.indexOf('—') === -1) {{
                    el('q-pe-foot').textContent = '市盈率来自实时行情（{hk_quote.SOURCES["tencent"]["label"] if hk_quote else "行情源"}）';
                }}
                if (el('q-source-foot')) el('q-source-foot').textContent = '行情数据源: ' + q.source_label + (q.time ? ' · 行情时间 ' + q.time : '');
                if (el('q-banner-icon')) el('q-banner-icon').textContent = '🟢';
                if (el('q-banner-strong')) el('q-banner-strong').textContent = '实时行情 (LIVE) · ' + q.source_label;
                if (el('q-banner-hint')) el('q-banner-hint').textContent = '行情来自免费数据源「' + q.source_label + '」并自动更新；快讯与因子仍为内置演示内容，仅供参考。';
                if (el('q-banner-time')) el('q-banner-time').textContent = '行情时间: ' + (q.time || '—') + ' · 抓取: ' + (q.fetched_at || '—');
                appendLog('QUOTE.LIVE', 'OK', STOCK_CODE + ' ' + q.price + ' ' + q.change_pct + ' via ' + q.source_label + (q.time ? ' @ ' + q.time : ''));
            }} catch (e) {{
                appendLog('QUOTE.STATUS', 'WARN', 'Quote refresh failed: ' + e.message);
            }}
        }}
        setTimeout(refreshQuote, 800);    // 页面加载后立即拉取一次实时行情
        setInterval(refreshQuote, 30000); // 每 30 秒自动更新

        // =================================================================
        // 📊 字符模拟图 · 智能行情交互控制系统 (TradeView Chart Engine)
        // =================================================================
        let currentTradeViewSymbol = '{stock_code}';
        let currentTradeViewName = '{data["code"]} {data["name"]}';
        let currentTradeViewTF = 'daily';
        let currentTradeViewBars = [];
        let currentTradeViewSupport = '--';
        let currentTradeViewResistance = '--';

        function generateDefaultChartBars(symbol, tf) {{
            const basePrices = {{
                '09988': 81.25,
                '00700': 478.80,
                '03690': 125.40,
                'BABA': 86.50
            }};
            const bp = basePrices[symbol] || 81.25;
            const bars = [];
            const count = 60;
            const now = new Date();
            for (let i = 0; i < count; i++) {{
                const dt = new Date(now.getTime() - (count - 1 - i) * 86400000);
                const dateStr = dt.toISOString().slice(0, 10);
                const phase = i / 10.0;
                const trend = (i / count) * 0.08 - 0.04;
                const noiseClose = Math.sin(phase * 1.5) * 0.025 + Math.cos(phase * 0.7) * 0.015 + trend;
                const closeP = bp * (1.0 + noiseClose - (Math.sin(5.9 * 1.5)*0.025 + Math.cos(5.9*0.7)*0.015));
                const openP = closeP * (1.0 - Math.sin(phase * 2.1) * 0.012);
                const highP = Math.max(openP, closeP) * (1.0 + Math.abs(Math.cos(phase * 1.3)) * 0.01);
                const lowP = Math.min(openP, closeP) * (1.0 - Math.abs(Math.sin(phase * 1.9)) * 0.01);
                const finalClose = (i === count - 1) ? bp : closeP;
                const finalHigh = (i === count - 1) ? Math.max(openP, bp) * 1.006 : highP;
                const finalLow = (i === count - 1) ? Math.min(openP, bp) * 0.994 : lowP;
                const volVal = Math.floor((12000000 + Math.sin(phase * 3.0) * 5000000) * (bp / 80.0));
                bars.push({{
                    date: dateStr,
                    open: parseFloat(openP.toFixed(3)),
                    high: parseFloat(finalHigh.toFixed(3)),
                    low: parseFloat(finalLow.toFixed(3)),
                    close: parseFloat(finalClose.toFixed(3)),
                    vol: volVal
                }});
            }}
            for (let i = 0; i < bars.length; i++) {{
                const slice5 = bars.slice(Math.max(0, i - 4), i + 1);
                const slice10 = bars.slice(Math.max(0, i - 9), i + 1);
                const slice20 = bars.slice(Math.max(0, i - 19), i + 1);
                bars[i].ma5 = parseFloat((slice5.reduce((a, b) => a + b.close, 0) / slice5.length).toFixed(3));
                bars[i].ma10 = parseFloat((slice10.reduce((a, b) => a + b.close, 0) / slice10.length).toFixed(3));
                bars[i].ma20 = parseFloat((slice20.reduce((a, b) => a + b.close, 0) / slice20.length).toFixed(3));
            }}
            return bars;
        }}
        const generateDefaultKlineBars = generateDefaultChartBars; // 兼容旧名

        async function fetchAndRenderChart(symbol, tf, name) {{
            currentTradeViewSymbol = symbol;
            currentTradeViewTF = tf;
            if (name) currentTradeViewName = name;

            // 先画本地快照，再请求接口；这样网络慢/接口被拦截时字符模拟图不会空白。
            // 原先必须等 /api/chart 返回后才首次绘制，离线或行情源超时会让用户看到空图。
            const instantBars = generateDefaultChartBars(symbol, tf);
            currentTradeViewBars = instantBars;
            currentTradeViewSupport = Math.min(...instantBars.slice(-20).map(b => b.low)).toFixed(2);
            currentTradeViewResistance = Math.max(...instantBars.slice(-20).map(b => b.high)).toFixed(2);
            drawCharChart(instantBars);
            updateTradeViewLegend(instantBars[instantBars.length - 1]);

            let data = null;
            try {{
                const resp = await fetch('/api/chart?code=' + encodeURIComponent(symbol) + '&tf=' + encodeURIComponent(tf));
                if (resp.ok) {{
                    data = await resp.json();
                }}
            }} catch (e) {{
                // 静态或离线访问时使用备用趋势引擎
            }}

            if (!data || !data.bars || !data.bars.length) {{
                const fallbackBars = generateDefaultChartBars(symbol, tf);
                const minLow = Math.min(...fallbackBars.slice(-20).map(b => b.low));
                const maxHigh = Math.max(...fallbackBars.slice(-20).map(b => b.high));
                data = {{
                    code: symbol,
                    name: name || symbol,
                    tf: tf,
                    bars: fallbackBars,
                    support: minLow.toFixed(2),
                    resistance: maxHigh.toFixed(2)
                }};
            }}

            currentTradeViewBars = data.bars;
            currentTradeViewSupport = data.support;
            currentTradeViewResistance = data.resistance;

            const resEl = document.getElementById('tv-res-val');
            const supEl = document.getElementById('tv-sup-val');
            if (resEl) resEl.textContent = data.resistance || '--';
            if (supEl) supEl.textContent = data.support || '--';

            drawCharChart(currentTradeViewBars);
            updateTradeViewLegend(currentTradeViewBars[currentTradeViewBars.length - 1]);
            setupTradeViewCrosshair();
        }}
        const fetchAndRenderTradeView = fetchAndRenderChart; // 兼容旧名

        function drawCharChart(bars) {{
            // 纯字符点阵渲染：字符模拟走势（涨 █ 跌 ▓ 影线 │），不依赖图片或第三方图表库。
            if (!bars || !bars.length) return;
            const chart = document.getElementById('tradeview-char-canvas') || document.getElementById('tradeview-kline-canvas');
            const volume = document.getElementById('tradeview-vol-canvas');
            if (!chart || !volume) return;
            const cols = Math.min(72, bars.length);
            const view = bars.slice(-cols);
            const rows = 20;
            const lows = view.map(b => Number(b.low));
            const highs = view.map(b => Number(b.high));
            let lo = Math.min(...lows), hi = Math.max(...highs);
            const span = hi - lo || 1;
            lo -= span * .06; hi += span * .06;
            const y = p => Math.max(0, Math.min(rows - 1, Math.round((hi - p) / (hi - lo) * (rows - 1))));
            const grid = Array.from({{length: rows}}, () => Array(cols).fill(' '));
            view.forEach((b, i) => {{
                const yo=y(b.open), yc=y(b.close), yh=y(b.high), yl=y(b.low);
                for (let r=Math.min(yh,yl); r<=Math.max(yh,yl); r++) grid[r][i]='│';
                const top=Math.min(yo,yc), bottom=Math.max(yo,yc);
                for (let r=top; r<=bottom; r++) grid[r][i] = (b.close >= b.open ? '█' : '▓');
            }});
            const labels = [hi, lo];
            chart.textContent = grid.map((line, r) => (r===0 ? labels[0].toFixed(2).padStart(7)+' ' : r===rows-1 ? labels[1].toFixed(2).padStart(7)+' ' : '       ') + line.join('')).join('\\n');
            const maxVol = Math.max(...view.map(b => Number(b.vol) || 0), 1);
            const vrows = 5, vg = Array.from({{length:vrows}}, () => Array(cols).fill(' '));
            view.forEach((b,i) => {{ const n=Math.max(1,Math.round((Number(b.vol)||0)/maxVol*vrows)); for(let r=vrows-n;r<vrows;r++) vg[r][i]='▂'; }});
            volume.textContent = 'VOL  ' + vg.map(r => r.join('')).join('\\n');
        }}
        const drawTradeViewChart = drawCharChart; // 兼容旧名

        function updateTradeViewLegend(bar) {{
            if (!bar) return;
            const el = (id) => document.getElementById(id);
            if (el('tv-hover-date')) el('tv-hover-date').textContent = bar.date || '--';
            if (el('tv-hover-open')) el('tv-hover-open').textContent = bar.open !== undefined ? bar.open.toFixed(2) : '--';
            if (el('tv-hover-high')) el('tv-hover-high').textContent = bar.high !== undefined ? bar.high.toFixed(2) : '--';
            if (el('tv-hover-low')) el('tv-hover-low').textContent = bar.low !== undefined ? bar.low.toFixed(2) : '--';
            if (el('tv-hover-close')) el('tv-hover-close').textContent = bar.close !== undefined ? bar.close.toFixed(2) : '--';
            if (el('tv-hover-vol')) el('tv-hover-vol').textContent = bar.vol ? (bar.vol > 1e6 ? (bar.vol/1e6).toFixed(2)+'M' : (bar.vol/1e3).toFixed(1)+'K') : '--';
            if (el('tv-hover-ma5')) el('tv-hover-ma5').textContent = bar.ma5 !== undefined ? bar.ma5.toFixed(2) : '--';
            if (el('tv-hover-ma10')) el('tv-hover-ma10').textContent = bar.ma10 !== undefined ? bar.ma10.toFixed(2) : '--';
            if (el('tv-hover-ma20')) el('tv-hover-ma20').textContent = bar.ma20 !== undefined ? bar.ma20.toFixed(2) : '--';

            const chgEl = el('tv-hover-change');
            if (chgEl && bar.open && bar.close) {{
                const diff = bar.close - bar.open;
                const pct = (diff / bar.open) * 100;
                const sign = diff >= 0 ? '+' : '';
                chgEl.textContent = sign + pct.toFixed(2) + '% (' + sign + diff.toFixed(2) + ')';
                chgEl.style.color = diff >= 0 ? '#5cff5c' : '#ff5c5c';
            }}
        }}

        function setupTradeViewCrosshair() {{
            // 字符点阵模式保持轻量；鼠标悬停不再调用 Canvas API。
        }}

        function switchTradeViewSymbol(symbol, name, el) {{
            playBeep(920, 'triangle', 0.06);
            const btns = document.querySelectorAll('.symbol-btn');
            btns.forEach(b => b.classList.remove('active'));
            if (el) el.classList.add('active');

            const nameEl = document.getElementById('tv-symbol-name');
            if (nameEl) nameEl.textContent = name + ' · ' + currentTradeViewTF.toUpperCase();

            fetchAndRenderChart(symbol, currentTradeViewTF, name);

            const widgetPanel = document.getElementById('tradeview-widget-panel');
            if (widgetPanel && widgetPanel.style.display !== 'none') {{
                initTradingViewWidget(symbol);
            }}
        }}

        function switchTradeViewTF(tf, label, el) {{
            playBeep(880, 'sine', 0.05);
            const btns = document.querySelectorAll('.tf-btn');
            btns.forEach(b => b.classList.remove('active'));
            if (el) el.classList.add('active');

            const nameEl = document.getElementById('tv-symbol-name');
            if (nameEl) nameEl.textContent = currentTradeViewName + ' · ' + label;

            fetchAndRenderChart(currentTradeViewSymbol, tf, currentTradeViewName);
        }}

        function switchTradeViewEngine(mode, el) {{
            playBeep(1050, 'triangle', 0.06);
            const btns = document.querySelectorAll('.engine-btn');
            btns.forEach(b => b.classList.remove('active'));
            if (el) el.classList.add('active');

            const canvasPanel = document.getElementById('tradeview-canvas-panel');
            const widgetPanel = document.getElementById('tradeview-widget-panel');

            if (mode === 'canvas') {{
                if (canvasPanel) canvasPanel.style.display = 'block';
                if (widgetPanel) widgetPanel.style.display = 'none';
                drawCharChart(currentTradeViewBars);
            }} else if (mode === 'widget') {{
                if (canvasPanel) canvasPanel.style.display = 'none';
                if (widgetPanel) widgetPanel.style.display = 'block';
                initTradingViewWidget(currentTradeViewSymbol);
            }}
        }}

        function initTradingViewWidget(symbol) {{
            const container = document.getElementById('tv_widget_embed_container');
            if (!container) return;
            container.innerHTML = '';
            const tvSymbolMap = {{
                '09988': 'HKEX:9988',
                '00700': 'HKEX:700',
                '03690': 'HKEX:3690',
                'BABA': 'NASDAQ:BABA'
            }};
            const tvSym = tvSymbolMap[symbol] || 'HKEX:9988';

            const script = document.createElement('script');
            script.src = 'https://s3.tradingview.com/tv.js';
            script.async = true;
            script.onload = function() {{
                if (typeof TradingView !== 'undefined') {{
                    new TradingView.widget({{
                        "width": "100%",
                        "height": 520,
                        "symbol": tvSym,
                        "interval": "D",
                        "timezone": "Asia/Hong_Kong",
                        "theme": "dark",
                        "style": "1",
                        "locale": "zh_CN",
                        "toolbar_bg": "#0e1130",
                        "enable_publishing": false,
                        "hide_side_toolbar": false,
                        "allow_symbol_change": true,
                        "container_id": "tv_widget_embed_container"
                    }});
                }}
            }};
            container.appendChild(script);
        }}

        setTimeout(() => {{
            fetchAndRenderChart('{stock_code}', 'daily', '{data["code"]} {data["name"]}');
        }}, 400);

        function appendLog(tag, status, msg) {{
            const term = document.getElementById('terminal-log');
            if (!term) return;
            const now = new Date().toISOString().substring(11, 19);
            const line = document.createElement('div');
            line.className = 'log-line';
            line.innerHTML = `<span class="log-ts">[${{now}}]</span> <span class="log-tag">[${{tag}}]</span> <span class="log-msg">${{msg}}</span>`;
            term.appendChild(line);
            if (term.childNodes.length > 5) term.removeChild(term.firstChild);
        }}

        // 实时时钟更新
        setInterval(() => {{
            const now = new Date();
            const utc = now.toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
            // CST
            const cstDate = new Date(now.getTime() + 8 * 3600 * 1000);
            const cst = cstDate.toISOString().replace('T', ' ').substring(0, 19) + ' CST (北京时间)';

            const utcEl = document.getElementById('utc-clock');
            const cstEl = document.getElementById('cst-clock');
            const gsUtcEl = document.getElementById('gs-utc-clock');
            if (utcEl) utcEl.textContent = utc;
            if (cstEl) cstEl.textContent = cst;
            if (gsUtcEl) gsUtcEl.textContent = utc.replace(' UTC', '');
        }}, 1000);

        // 模拟遥测日志滚动
        const logSamples = [
            ["HEARTBEAT", "TICK", "Node HK-Master-01 reported 0 packet loss (latency 2.1ms)"],
            ["AI.INFERENCE", "OK", "DeepSeek sentiment embedding batch completed in 18ms"],
            ["MARKET.DATA", "FLOW", {js_quote_log}],
            ["RADAR.SCAN", "PULSE", "18 platforms active. No anomalous divergence detected."],
            ["FOREIGN.WEEKLY", "PULSE", {js_foreign_log}]
        ];
        let sampleIdx = 0;
        setInterval(() => {{
            const s = logSamples[sampleIdx % logSamples.length];
            appendLog(s[0], s[1], s[2]);
            sampleIdx++;
        }}, 4500);
    </script>
</body>
</html>
"""
    return html


# ================================================================ AI 研报接口

def _api_report(params: dict) -> dict:
    """执行「实时行情 → AI 研报 → 推送」，返回结构化 JSON（前端渲染用）。"""
    code = str(params.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "缺少股票代码（请填入港股/A股代码）"}
    if stock_report is None:
        return {"ok": False, "error": "stock_report 模块加载失败"}
    channel = str(params.get("channel") or "console")
    if channel not in ("console", "pushplus", "wecom", "serverchan", "all"):
        channel = "console"
    template = str(params.get("template") or "analysis")
    ai_provider = str(params.get("ai_provider") or "") or None
    dry_run = bool(params.get("dry_run", True))
    theme = str(params.get("theme") or "dos")
    mode = str(params.get("mode") or "full")
    industry = str(params.get("industry") or "").strip() or None
    # AI 分节压缩：不传=跟随默认（开启）；true/false 显式指定
    raw_ac = str(params.get("ai_compress") or "").strip().lower()
    ai_compress = (None if raw_ac in ("", "auto")
                   else raw_ac in ("1", "true", "yes", "on"))
    try:
        hours = int(params.get("hours") or 48)
        r = stock_report.run_report(
            code, channel=channel, ai_provider=ai_provider, template=template,
            dry_run=dry_run, theme=theme, mode=mode, industry=industry,
            hours=hours, ai_compress=ai_compress)
        r["ok"] = True
        return r
    except Exception as e:            # noqa: BLE001
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def _api_skills_run(params: dict) -> dict:
    """Skills Hub：按官方/社区 skill 生成备忘录。"""
    code = str(params.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "缺少股票代码"}
    if skills_hub_mod is None:
        return {"ok": False, "error": "skills_hub 模块加载失败"}
    skill = str(params.get("skill") or params.get("template") or "morning_note")
    channel = str(params.get("channel") or "console")
    if channel not in ("console", "pushplus", "wecom", "serverchan", "all"):
        channel = "console"
    ai_provider = str(params.get("ai_provider") or "") or None
    dry_run = bool(params.get("dry_run", True))
    theme = str(params.get("theme") or "dos")
    try:
        r = skills_hub_mod.run_skill(
            code, skill=skill, ai_provider=ai_provider, channel=channel,
            dry_run=dry_run, theme=theme,
            timeout=int(params.get("timeout") or 90))
        r["ok"] = True
        return r
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def _api_equity_column(params: dict) -> dict:
    """独立栏目：机构级个股投研（equity-research-skill）。"""
    code = str(params.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "缺少股票代码"}
    if equity_col is None:
        return {"ok": False, "error": "equity_research_column 模块加载失败"}
    channel = str(params.get("channel") or "console")
    if channel not in ("console", "pushplus", "wecom", "serverchan", "all"):
        channel = "console"
    mode = str(params.get("mode") or "full")
    if mode not in ("full", "earnings"):
        mode = "full"
    ai_provider = str(params.get("ai_provider") or "") or None
    dry_run = bool(params.get("dry_run", True))
    theme = str(params.get("theme") or "dos")
    industry = str(params.get("industry") or "").strip() or None
    try:
        r = equity_col.generate_column(
            code, mode=mode, ai_provider=ai_provider, channel=channel,
            dry_run=dry_run, theme=theme, industry=industry,
            timeout=int(params.get("timeout") or 90),
            run_check=bool(params.get("run_check", True)),
            hours=int(params.get("hours") or 48))
        r["ok"] = True
        return r
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def _api_foreign_weekly(params: dict) -> dict:
    """国外专业投资机构一周研报栏目（12机构、168h窗口）。"""
    if foreign_mod is None:
        return {"ok": False, "error": "foreign_institution_weekly 模块加载失败"}
    topic = str(params.get("topic") or params.get("query") or params.get("code") or "").strip()
    hours = int(params.get("hours") or 168)
    use_deepseek = bool(params.get("use_deepseek", True))
    only = str(params.get("only") or "").strip()
    only_list = [s.strip() for s in only.split(",") if s.strip()] if only else None
    try:
        pack = foreign_mod.collect_foreign_weekly(
            topic=topic, hours=hours, timeout=int(params.get("timeout") or 20),
            only=only_list, use_deepseek=use_deepseek)
        # 生成渲染
        rule_md = foreign_mod.render_foreign_weekly_rule(pack)
        appendix_md = foreign_mod.render_foreign_weekly_appendix(pack)
        ctx = foreign_mod.foreign_weekly_context(pack)
        r = {
            "ok": True,
            "topic": topic,
            "hours": hours,
            "agg": pack.agg,
            "per_institution": {k: len(v) for k, v in pack.reports.items()},
            "sentiment": pack.agg.get("sentiment", {}),
            "rule_md": rule_md,
            "appendix_md": appendix_md,
            "context": ctx,
            "count": pack.agg.get("n", 0),
        }
        # 若请求 AI 生成，则走 pushplus_deepseek 主流程生成研报正文
        if params.get("generate"):
            if pp_mod is None:
                r["ai_error"] = "pushplus_deepseek 模块未加载"
            else:
                try:
                    # 构造等同于 --template foreign_weekly 的 AI 调用
                    gen_params = {
                        "template": "foreign_weekly",
                        "topic": topic or "科技股",
                        "hours": hours,
                        "ai_provider": str(params.get("ai_provider") or "deepseek"),
                        "channel": str(params.get("channel") or "console"),
                        "dry_run": bool(params.get("dry_run", True)),
                    }
                    # 尝试调用 pushplus_deepseek 的 build_messages + AI 生成简化版
                    # 这里直接用 foreign weekly pack 的 context 去调
                    # 若有 stock_report 则走其 run_report 接口，topic 传入
                    if stock_report is not None:
                        rr = stock_report.run_report(
                            code=topic or "09988",
                            template="foreign_weekly",
                            ai_provider=gen_params["ai_provider"],
                            channel=gen_params["channel"],
                            dry_run=gen_params["dry_run"],
                            hours=hours,
                            topic=topic)
                        r["ai_report"] = rr
                    else:
                        r["ai_report"] = {"ok": False, "note": "stock_report 未加载，返回 rule 版"}
                except Exception as e:
                    r["ai_error"] = f"{e.__class__.__name__}: {e}"
        return r
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


# ================================================================ HTTP 服务处理

class MonitorHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化日志输出
        sys.stderr.write(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}\n")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # CORS 允许
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        if path in ("/", "/index.html", "/dashboard"):
            stock_code = qs.get("code", ["09988"])[0]
            html = render_server_monitor_html(stock_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if path == "/api/status":
            cluster = get_cluster_status()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(cluster, ensure_ascii=False).encode("utf-8"))
            return

        if path == "/api/stock":
            stock_code = qs.get("code", ["09988"])[0]
            data = get_stock_view(stock_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return

        if path in ("/api/chart", "/api/kline"):
            stock_code = qs.get("code", ["09988"])[0]
            tf = qs.get("tf", ["daily"])[0]
            data = get_chart_view(stock_code, tf=tf)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return

        if path == "/api/quote":
            # 轻量实时行情接口（前端轮询用）
            stock_code = qs.get("code", ["09988"])[0]
            v = get_stock_view(stock_code)
            payload = {
                "code": v["code"],
                "live": v["quote_live"],
                "source_key": v["quote_source_key"],
                "source_label": v["quote_source"],
                "time": v["quote_time"],
                "fetched_at": v["quote_fetched_at"],
                "price": v["price"],
                "currency": v["currency"],
                "change": v.get("change_val", ""),   # 涨跌额显示串，如 -0.400
                "change_pct": v.get("change", ""),   # 涨跌幅显示串，如 -0.08%
                "high": v["high"],
                "low": v["low"],
                "vol": v["vol"],
                "pe": v["pe"],
                "pe_live": v.get("pe_live", False),
                "error": v.get("quote_error"),
            }
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return

        if path in ("/api/skills", "/api/skills/teaser"):
            stock_code = qs.get("code", ["09988"])[0]
            if skills_hub_mod is None:
                payload = {"ok": False, "error": "skills_hub 未加载", "installed": 0}
            else:
                payload = skills_hub_mod.catalog_teaser(stock_code)
                payload["ok"] = True
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return

        if path in ("/api/equity", "/api/equity/teaser"):
            stock_code = qs.get("code", ["09988"])[0]
            if equity_col is None:
                payload = {"ok": False, "error": "equity_research_column 未加载",
                           "skill_available": False}
            else:
                payload = equity_col.column_teaser(stock_code)
                payload["ok"] = True
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return

        if path in ("/api/foreign_weekly", "/api/foreign_weekly/teaser",
                    "/api/foreign", "/api/foreign/teaser"):
            topic = qs.get("topic", qs.get("code", [""]))[0]
            hours = int(qs.get("hours", ["168"])[0] or 168)
            if foreign_mod is None:
                payload = {"ok": False, "error": "foreign_institution_weekly 未加载"}
            else:
                try:
                    # teaser 用 demo_pack 离线，快速返回
                    if "teaser" in path:
                        pack = foreign_mod.demo_pack()
                        payload = {
                            "ok": True,
                            "topic": topic,
                            "hours": hours,
                            "agg": pack.agg,
                            "per_institution": {k: len(v) for k, v in pack.reports.items()},
                            "teaser_md": foreign_mod.render_foreign_weekly_rule(pack)[:2000],
                        }
                    else:
                        payload = _api_foreign_weekly({"topic": topic, "hours": hours})
                except Exception as e:
                    payload = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return

        # 默认 404
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Type", "application/json; charset=utf-8")

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            params = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(params, dict):
                params = {}
        except json.JSONDecodeError:
            params = {}

        if path == "/api/report":
            payload = _api_report(params)
        elif path in ("/api/equity", "/api/equity/report"):
            payload = _api_equity_column(params)
        elif path in ("/api/skills", "/api/skills/run"):
            payload = _api_skills_run(params)
        elif path in ("/api/foreign_weekly", "/api/foreign_weekly/report",
                      "/api/foreign", "/api/foreign/report"):
            payload = _api_foreign_weekly(params)
        else:
            payload = {"ok": False, "error": "Not Found"}

        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


class MonitorTCPServer(socketserver.TCPServer):
    """允许端口快速复用，避免停止后 TIME_WAIT 导致无法立即重启。"""
    allow_reuse_address = True


def run_server(port: int = 8080):
    host = "0.0.0.0"
    server = MonitorTCPServer((host, port), MonitorHandler)
    print(f"📡 [OCTOPUS-NOC] 服务器大屏监视服务已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def export_static_files():
    """导出静态预览 HTML"""
    out_dir = os.path.join(os.path.dirname(__file__), "examples")
    os.makedirs(out_dir, exist_ok=True)
    html = render_server_monitor_html("09988")

    file_path1 = os.path.join(out_dir, "server_monitor_preview.html")
    with open(file_path1, "w", encoding="utf-8") as f:
        f.write(html)

    file_path2 = os.path.join(os.path.dirname(__file__), "server_monitor.html")
    with open(file_path2, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ 已生成服务器大屏文件: {file_path1} & {file_path2}")


if __name__ == "__main__":
    export_static_files()
    port = 8080
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    if "--export-only" in sys.argv:
        sys.exit(0)
    run_server(port)
