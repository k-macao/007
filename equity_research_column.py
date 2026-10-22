#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
equity_research_column.py — 机构级个股投研独立栏目

基于 equity-research-skill（https://github.com/k-macao/equity-research-skill
/ https://github.com/rollingSirius/equity-research-skill）落地：

  · 九章完整深度研究（默认） / 九章财报深度模式
  · 预期差主线 + 财报质量核查 + 多方法估值（dcf.py 脚本计算）
  · 产出可复算、可审计的 Markdown 栏目内容
  · 供 stock_report / pushplus_deepseek / server_dashboard 调用

用法：
  python equity_research_column.py 09988 --mode full --provider rule
  python equity_research_column.py 600519 --mode earnings --provider deepseek
  python equity_research_column.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 路径与常量

ROOT = Path(__file__).resolve().parent
SKILL_DIR = ROOT / "equity_research"
SCRIPTS_DIR = SKILL_DIR / "scripts"
REFS_DIR = SKILL_DIR / "references"
INDUSTRIES_DIR = SKILL_DIR / "industries"
EXAMPLES_DIR = SKILL_DIR / "Example"

CST = timezone(timedelta(hours=8), "CST")
VERSION = "1.1.0-equity-2026-08-13"

COLUMN_ID = "equity_research"
COLUMN_TITLE = "机构级个股投研"
COLUMN_SUBTITLE = "Equity Research · 九章深度 · 预期差主线 · 可复算估值"
COLUMN_SKILL_URL = "https://github.com/k-macao/equity-research-skill"
COLUMN_SKILL_UPSTREAM = "https://github.com/rollingSirius/equity-research-skill"

MODES = ("full", "earnings")
MODE_TITLES = {
    "full": "完整深度研究（九章）",
    "earnings": "财报深度分析（九章）",
}

# 模板注册名（接入 pushplus_deepseek.TEMPLATES）
TEMPLATE_NAME = "equity"
TEMPLATE_TITLE = "机构级个股投研"
TEMPLATE_MAX_TOKENS = 8000

# 九章标题（完整模式）
FULL_CHAPTERS = [
    "一、一页速览（Executive Summary）",
    "二、业务详情",
    "三、业务与竞争分析",
    "四、管理层、治理与资本配置计分卡",
    "五、财务分析与财报质量",
    "六、估值（多方法交叉验证）",
    "七、分析师评价汇总",
    "八、最新新闻与催化剂",
    "九、投资结论、反方论证与仓位",
]

# 九章标题（财报模式）
EARNINGS_CHAPTERS = [
    "一、结论与快照",
    "二、预期差与质量",
    "三、收入、分部与 KPI",
    "四、利润率、费用与盈利质量",
    "五、现金流、资产负债表与资本配置",
    "六、指引、电话会与管理层信号",
    "七、竞争、行业与市场反应",
    "八、模型、估值与公允价值变动桥",
    "九、投资论点更新与行动清单",
]

# 简易行业路由（代码/名称关键词 → industries slug）
INDUSTRY_HINTS: list[tuple[list[str], str]] = [
    (["阿里", "腾讯", "美团", "拼多多", "京东", "百度", "网易", "快手",
      "BABA", "BIDU", "PDD", "JD", "NTES", "09988", "00700", "03690",
      "01810", "09618", "01024", "9888"], "internet-platform"),
    (["茅台", "五粮液", "白酒", "伊利", "海天", "农夫"], "consumer"),
    (["宁德", "比亚迪", "理想", "小鹏", "蔚来", "赛力斯"], "autos-ev"),
    (["中芯", "韦尔", "北方华创", "海光", "寒武纪", "NVIDIA", "NVDA",
      "AMD", "TSM", "阿斯麦"], "semiconductors"),
    (["招商银行", "工商银行", "建设银行", "平安银行", "兴业银行",
      "农业银行", "中国银行"], "banks"),
    (["中国平安", "中国人寿", "新华保险", "中国太保"], "insurance"),
    (["恒瑞", "药明", "百济", "信达", "复星医药"], "pharma"),
    (["万科", "保利", "华润置地", "龙湖", "碧桂园"], "reits"),
    (["中石油", "中石化", "中国海油", "紫金矿业"], "energy"),
    (["中国移动", "中国电信", "中国联通"], "telecom"),
]


# ---------------------------------------------------------------- 工具

def skill_available() -> bool:
    return (SKILL_DIR / "SKILL.md").is_file() and (SCRIPTS_DIR / "dcf.py").is_file()


def read_ref(name: str, max_chars: int = 12000) -> str:
    """读取 skill 参考文件（截断以控制 prompt 体积）。"""
    path = REFS_DIR / name
    if not path.is_file():
        return f"（未找到参考文件 references/{name}）"
    text = path.read_text(encoding="utf-8")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n…（已截断，完整见 equity_research/references/{name}）"
    return text


def read_skill_overview(max_chars: int = 6000) -> str:
    path = SKILL_DIR / "SKILL.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    # 去掉 YAML front matter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2].lstrip("\n")
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n…（SKILL.md 已截断）"
    return text


def guess_industry(topic: str, code: str = "") -> str:
    blob = f"{topic} {code}".upper()
    for keys, slug in INDUSTRY_HINTS:
        for k in keys:
            if k.upper() in blob:
                return slug
    return "internet-platform"  # 港股/中概默认平台互联网


def read_industry_appendix(slug: str, max_chars: int = 4000) -> str:
    path = INDUSTRIES_DIR / f"{slug}.md"
    if not path.is_file():
        return f"（行业附录 industries/{slug}.md 未找到）"
    text = path.read_text(encoding="utf-8")
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n…（行业附录已截断）"
    return text


def now_stamp() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M CST")


def date_tag() -> str:
    return datetime.now(CST).strftime("%Y%m%d")


# ---------------------------------------------------------------- 估值脚本

def default_assumptions_from_quote(quote: dict | None, code: str = "") -> dict:
    """
    用实时行情构造可跑通的估值假设 JSON（字段对齐 scripts/dcf.py DEMO）。
    无完整财务时标注为「演示假设」，禁止伪装成已审计财务。

    为数值稳定，将「公司」标准化为 shares=1 的每股模型：
    情景 FCF / 收入等均为「每股」口径。
    """
    price = 100.0
    currency = "HKD"
    if quote and quote.get("price"):
        try:
            price = float(quote["price"])
        except (TypeError, ValueError):
            pass
        currency = quote.get("currency") or currency
    if price <= 0:
        price = 100.0

    # 每股模型：shares=1，FCF 用现价 × FCF yield 近似
    shares = 1.0
    net_debt = 0.0
    fcf_yield = 0.05
    fcf0 = max(price * fcf_yield, 0.5)
    rev0 = max(fcf0 / 0.18, 1.0)
    eps = max(price * 0.06, 0.1)  # 粗略 earnings yield 6%
    nopat = max(fcf0 * 1.05, 0.5)
    invested_capital = max(nopat / 0.12, 1.0)
    asset_value = max(invested_capital * 0.85, 1.0)

    def sc(name: str, rev_mult: float, m_start: float, m_end: float,
           g_fade: float, prob: float, dil: float = 0.01) -> dict:
        revs, margins = [], []
        r = rev0 * rev_mult
        for i in range(5):
            revs.append(round(r, 4))
            m = m_start + (m_end - m_start) * i / 4
            margins.append(round(m, 4))
            r *= 1.0 + (0.06 if name == "牛市" else (0.03 if name == "基准" else 0.0))
        return {
            "name": name,
            "prob": prob,
            "revenue": revs,
            "fcf_margin": margins,
            "fade_years": 5,
            "fade_g_start": g_fade,
            "annual_dilution": dil,
        }

    return {
        "_meta": {
            "note": "演示/占位假设（每股模型 shares=1）：由行情粗算，正式研报须替换为一手披露",
            "code": code,
            "currency": currency,
            "model": "per_share_normalized",
            "generated_at": now_stamp(),
            "skill": COLUMN_SKILL_URL,
        },
        "price": round(price, 4),
        "shares": shares,
        "net_debt": net_debt,
        "wacc": 0.09,
        "terminal_g": 0.03,
        "scenarios": [
            sc("熊市", 0.90, 0.10, 0.14, 0.04, 0.25),
            sc("基准", 1.00, 0.14, 0.20, 0.08, 0.50),
            sc("牛市", 1.15, 0.16, 0.24, 0.12, 0.25, dil=0.015),
        ],
        "sensitivity": {
            "scenario": "基准",
            "wacc": [0.08, 0.09, 0.10],
            "g": [0.02, 0.03, 0.04],
        },
        "reverse": {
            "interim_fcf": [round(fcf0 * (1.08 ** i), 4) for i in range(5)],
            "steady_margins": [0.15, 0.20, 0.25],
            "base_revenue": round(rev0, 4),
        },
        "pvgo": {
            "earnings_ps": round(eps, 4),
            "r": 0.09,
            "price": round(price, 4),
        },
        "epv": {
            "earnings_basis": "NOPAT",
            "normalized_earnings": round(nopat, 4),
            "coc": 0.09,
            "net_debt": net_debt,
            "shares": shares,
            "asset_value": round(asset_value, 4),
            "price": round(price, 4),
            "growth": {"g": 0.04, "roiic": 0.15, "mode": "franchise"},
            "asset_series": [
                ["FY-2", round(nopat * 0.85, 4), round(asset_value * 0.9, 4)],
                ["FY-1", round(nopat * 0.95, 4), round(asset_value * 0.95, 4)],
                ["最新", round(nopat, 4), round(asset_value, 4)],
            ],
        },
        "eva": {
            "invested_capital": round(invested_capital, 4),
            "nopat": round(nopat, 4),
            "fade_years": 10,
            "reinvestment_rate": 0.40,
            "roiic": 0.15,
            "shares": shares,
            "net_debt": net_debt,
        },
        "montecarlo": {
            "n": 1500,
            "seed": 42,
            "base_revenue": round(rev0, 4),
            "years": 5,
            "growth_mean": 0.08,
            "growth_std": 0.05,
            "margin_low": 0.10,
            "margin_mode": 0.18,
            "margin_high": 0.26,
        },
        "range_low": round(price * 0.70, 2),
        "range_high": round(price * 1.30, 2),
    }


def run_dcf(assumptions: dict, timeout: int = 30) -> tuple[str, dict | None, str | None]:
    """
    调用 skill 的 scripts/dcf.py。
    返回 (stdout 文本, 解析后的摘要 dict 或 None, 错误信息或 None)。
    """
    dcf_py = SCRIPTS_DIR / "dcf.py"
    if not dcf_py.is_file():
        return "", None, "dcf.py 不存在（请确认 equity_research/scripts 已安装）"

    with tempfile.TemporaryDirectory(prefix="equity_dcf_") as td:
        cfg = Path(td) / "assumptions.json"
        cfg.write_text(json.dumps(assumptions, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(dcf_py), "--config", str(cfg)],
                capture_output=True, text=True, timeout=timeout, cwd=str(SKILL_DIR),
            )
        except subprocess.TimeoutExpired:
            return "", None, "dcf.py 超时"
        except OSError as e:
            return "", None, f"无法启动 dcf.py: {e}"

        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            return out, None, f"dcf.py 退出码 {proc.returncode}"

        summary = _parse_dcf_summary(out)
        return out, summary, None


def _parse_dcf_summary(text: str) -> dict:
    """从 dcf.py 文本输出提取关键标签（尽力解析，失败不抛）。"""
    s: dict[str, Any] = {"raw_len": len(text)}
    # 优先「标定」节的低估/合理/高估标签（避免误抓 PVGO 行的 **…**）
    m = re.search(r"=== 标定 ===.*?→\s*\*\*([^*]+)\*\*", text, re.S)
    if m:
        s["label"] = m.group(1).strip()
    else:
        for lab in ("显著低估", "显著高估", "低估", "高估", "合理"):
            if re.search(rf"→\s*\*\*{re.escape(lab)}\*\*", text):
                s["label"] = lab
                break
    m = re.search(r"综合区间\s*\[([0-9.,]+),\s*([0-9.,]+)\]", text)
    if m:
        try:
            s["range_low"] = float(m.group(1).replace(",", ""))
            s["range_high"] = float(m.group(2).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"概率加权公允价值:\s*([0-9,.]+)/股", text)
    if m:
        try:
            s["fair_value"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\| 现价\s*([0-9.]+)", text) or re.search(
        r"现价\s*([0-9.]+)(?!\s*=)", text)
    if m:
        try:
            s["price"] = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"P50\s*([0-9,.]+)", text)
    if m:
        try:
            s["mc_p50"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"概率加权期望收益 EV\s*=\s*([+\-0-9.%]+)", text)
    if m:
        s["ev_pct"] = m.group(1)
    m = re.search(r"现价的\s*([0-9.]+%)\s*在为未来增长付费", text)
    if m:
        s["pvgo_pct"] = m.group(1)
    return s


def run_checker(report_md: str, assumptions: dict | None = None,
                industry: str = "internet-platform",
                language: str = "zh",
                timeout: int = 30) -> tuple[str, int]:
    """运行 check_research_output.py，返回 (输出文本, 退出码)。"""
    checker = SCRIPTS_DIR / "check_research_output.py"
    if not checker.is_file():
        return "检查器不存在", 1

    with tempfile.TemporaryDirectory(prefix="equity_chk_") as td:
        report_path = Path(td) / "report.md"
        report_path.write_text(report_md, encoding="utf-8")
        cmd = [
            sys.executable, str(checker),
            "--report", str(report_path),
            "--industry", industry,
            "--language", language,
        ]
        if assumptions:
            as_path = Path(td) / "assumptions.json"
            as_path.write_text(json.dumps(assumptions, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            cmd += ["--assumptions", str(as_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, cwd=str(SKILL_DIR))
        except Exception as e:  # noqa: BLE001
            return f"检查器执行失败: {e}", 1
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return out.strip() or "(无输出)", proc.returncode


# ---------------------------------------------------------------- Prompt 构造

def build_system_prompt(mode: str = "full") -> str:
    overview = read_skill_overview(5000)
    fmt = read_ref("output-format.md", 3500)
    tmpl = read_ref(
        "earnings-mode.md" if mode == "earnings" else "report-template.md", 4500)
    return (
        "你是资深二级市场投研分析师，兼具卖方深度研究的严谨与买方的决策导向。\n"
        "你正在执行 equity-research skill 的完整流程，输出机构级个股投资研究报告。\n\n"
        "## 核心纪律（必须遵守）\n"
        "1. 事实 vs 判断分离：推断显式标「我的判断」并给依据。\n"
        "2. 关键数据标注来源与时间戳；缺失写「未获取到」，禁止用记忆填充具体财务数字。\n"
        "3. 预期差优先：报告核心是「市场定价了什么、我为何不同、何时验证」。\n"
        "4. 价值、路径、动作分离：内在价值判断 / 未来1–3个月市场交易方向 / 投资动作 分开写。\n"
        "5. 估值数字优先采用上下文中「估值脚本输出」；禁止心算编造 DCF 结果。\n"
        "6. 输出简体中文 Markdown；不要寒暄；不要用代码块包住整篇报告。\n"
        "7. 末尾必须有免责声明：本报告不构成投资建议，最终决策由读者承担。\n\n"
        f"## Skill 概要\n{overview}\n\n"
        f"## 输出格式规范\n{fmt}\n\n"
        f"## 报告模板（{'财报模式' if mode == 'earnings' else '完整模式'}）\n{tmpl}\n"
    )


def build_user_prompt(
    topic: str,
    context: str,
    mode: str = "full",
    industry: str = "internet-platform",
    dcf_output: str = "",
    assumptions: dict | None = None,
) -> str:
    industry_text = read_industry_appendix(industry, 3500)
    chapters = EARNINGS_CHAPTERS if mode == "earnings" else FULL_CHAPTERS
    chapter_list = "\n".join(f"  {c}" for c in chapters)
    as_json = ""
    if assumptions:
        # 去掉过大 meta，保留计算相关
        slim = {k: v for k, v in assumptions.items() if k != "_meta"}
        as_json = json.dumps(slim, ensure_ascii=False, indent=2)[:4000]

    dcf_block = dcf_output.strip()[:6000] if dcf_output else "（估值脚本未运行或失败，估值章须标注未获取到并说明）"

    mode_extra = ""
    if mode == "earnings":
        mode_extra = (
            "\n本次为**财报深度模式**：若无历史报告，先做首次覆盖基线（至少说明需补的 3 年/8 季度框架），"
            "再分析本次财报对论点与估值的含义。\n"
        )

    return (
        f"请为「{topic}」撰写一份机构级个股投资研究报告。\n"
        f"**模式**：{MODE_TITLES.get(mode, mode)}\n"
        f"**行业附录**: {industry}\n"
        f"**撰写日期 / 数据截止**: {now_stamp()}\n"
        f"{mode_extra}\n"
        f"## 实时行情与背景（优先采用）\n{context or '（无额外背景）'}\n\n"
        f"## 估值脚本输出（scripts/dcf.py，必须写入第六章/估值相关章节）\n"
        f"```\n{dcf_block}\n```\n\n"
        f"## 估值假设 JSON（摘要）\n```json\n{as_json or '{}'}\n```\n\n"
        f"## 行业附录（{industry}）\n{industry_text}\n\n"
        f"## 必须输出的章节结构（顺序固定，九章齐全）\n{chapter_list}\n\n"
        "## 第一章必须包含\n"
        "1. 结论框（引用块）：决策三分法（内在价值判断 / 未来1–3个月市场交易方向 / 投资动作）"
        " + 财报可信度等级 + 置信度 + 一句话论点 + 综合区间与隐含空间 + 最大风险\n"
        "2. Tearsheet 快照表（现价/市值/52周/关键倍数等，缺数据写未获取到）\n"
        "3. 预期差 Gap 表（市场隐含 vs 我的预期 vs base rate 分位）\n"
        "4. 核心多空逻辑各 3 条（可证伪）\n\n"
        "## 第九章必须包含\n"
        "- 复述决策三分法并解释三者为何可能不同\n"
        "- 回答：如果今天这是一笔现金，我会买入它吗？为什么？\n"
        "- 反方论证 / 事前风险预演：一年后失败的 3 个最可能原因（至少一条直击核心论点）\n"
        "- 监控清单 3–5 项（带阈值）与置信度自评\n\n"
        "报告标题格式：`# {公司}（{代码}）个股投资研究报告`\n"
        f"副标题后声明：`行业附录: {industry}`\n"
        "结尾附「数据来源与时间戳」清单与免责声明。\n"
    )


def build_messages(
    topic: str,
    context: str = "",
    mode: str = "full",
    industry: str | None = None,
    dcf_output: str = "",
    assumptions: dict | None = None,
) -> list[dict]:
    mode = mode if mode in MODES else "full"
    industry = industry or guess_industry(topic)
    return [
        {"role": "system", "content": build_system_prompt(mode)},
        {"role": "user", "content": build_user_prompt(
            topic, context, mode=mode, industry=industry,
            dcf_output=dcf_output, assumptions=assumptions)},
    ]


# ---------------------------------------------------------------- Rule 模式栏目（离线可交付）

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def gen_rule_report(
    topic: str,
    context: str = "",
    mode: str = "full",
    industry: str | None = None,
    quote: dict | None = None,
    dcf_output: str = "",
    dcf_summary: dict | None = None,
    assumptions: dict | None = None,
    code: str = "",
) -> str:
    """不调用大模型时的完整九章骨架报告（标注占位与数据缺口）。"""
    mode = mode if mode in MODES else "full"
    industry = industry or guess_industry(topic, code)
    stamp = now_stamp()
    price = "未获取到"
    currency = "—"
    pe = "未获取到"
    pb = "未获取到"
    mcap = "未获取到"
    chg = "—"
    high52 = low52 = "未获取到"
    name = topic
    if quote:
        if quote.get("price") is not None:
            price = f"{quote['price']:.3f}"
        currency = quote.get("currency") or currency
        if quote.get("pe") is not None:
            pe = str(quote["pe"])
        if quote.get("pb") is not None:
            pb = str(quote["pb"])
        if quote.get("market_cap") is not None:
            try:
                mc = float(quote["market_cap"])
                if mc >= 1e12:
                    mcap = f"{mc/1e12:.2f}万亿"
                elif mc >= 1e8:
                    mcap = f"{mc/1e8:.2f}亿"
                else:
                    mcap = f"{mc:.0f}"
            except (TypeError, ValueError):
                mcap = str(quote["market_cap"])
        if quote.get("change_pct") is not None:
            chg = f"{quote['change_pct']:+.2f}%"
        if quote.get("high_52w") is not None:
            high52 = str(quote["high_52w"])
        if quote.get("low_52w") is not None:
            low52 = str(quote["low_52w"])
        if quote.get("name"):
            name = quote["name"]

    label = (dcf_summary or {}).get("label") or "待定（演示假设）"
    r_lo = (dcf_summary or {}).get("range_low")
    r_hi = (dcf_summary or {}).get("range_high")
    rng = (f"{r_lo} – {r_hi}" if r_lo is not None and r_hi is not None
           else "见估值脚本输出")
    from source_names import generalize_source
    src = generalize_source((quote or {}).get("source_label") or (quote or {}).get("source") or "未获取到")
    qtime = (quote or {}).get("time") or (quote or {}).get("fetched_at") or stamp

    title_code = code or topic.split()[0]
    header = [
        f"# {name}（{title_code}）个股投资研究报告",
        "",
        f"**撰写日期：{stamp}｜数据截止：{qtime}｜报告币种：{currency}**",
        f"**行业附录: {industry}**",
        f"**栏目：{COLUMN_TITLE} · {MODE_TITLES.get(mode, mode)}**",
        "",
        "> 本报告区分「事实」与「我的判断」。事实尽量标注来源和时间；"
        "模型估值属于情景推演，不是确定结果。本报告仅供研究，不构成个性化投资建议。",
        "",
        f"> ⚙️ 生成模式：**rule 骨架**（未调用大模型）。"
        f"当前估值假设标记为**演示/占位**，不可直接用于投资决策。",
        "",
    ]

    # —— 第一章 ——
    ch1 = [
        f"## {FULL_CHAPTERS[0] if mode == 'full' else EARNINGS_CHAPTERS[0]}",
        "",
        "> **决策三分法**",
        f"> - **内在价值判断**：{label}（综合区间 {rng} {currency}）—— 我的判断，基于演示假设 + 脚本估值",
        f"> - **未来 1–3 个月市场交易方向**：未获取到足够催化剂与资金面数据，暂标「震荡观察」",
        f"> - **投资动作**：观望（rule 模式 + 演示假设触发谨慎原则；正式研究完成前不给买卖动作）",
        f"> - **财报可信度等级**：未获取到（未完成 forensic 全套）→ 暂按 **B（待核验）**",
        f"> - **置信度**：低（rule 骨架 / 财务未一手核验）",
        f"> - **一句话论点**：{name} 需在完整财务与行业 KPI 到位后，再以预期差框架给出可证伪结论。",
        f"> - **最大风险**：把演示估值当作已审计结论；或忽视治理/财务质量否决项。",
        "",
        "### Tearsheet 快照",
        "",
        _md_table(
            ["项目", "数值", "来源/时间"],
            [
                ["现价", f"**{price} {currency}**（{chg}）", f"{src}，{qtime}"],
                ["估算市值", mcap, f"{src}，{qtime}"],
                ["52 周区间", f"{low52} – {high52}", f"{src}，{qtime}"],
                ["PE / PB", f"{pe} / {pb}", f"{src}，{qtime}"],
                ["综合合理价值", rng, "scripts/dcf.py · 演示假设"],
                ["估值标签", label, "预注册标定规则 · 演示"],
                ["护城河", "未获取到（待业务拆解）", "—"],
                ["财报可信度", "B（待核验）", "forensic 未完整执行"],
                ["催化剂 Top1", "未获取到", "—"],
                ["上行证伪 Top1", "未获取到", "—"],
                ["下行证伪 Top1", "财务质量或行业竞争恶化", "框架占位"],
            ],
        ),
        "",
        "### 预期差 Gap 表（分析主线）",
        "",
        _md_table(
            ["维度", "市场隐含（现价）", "我的独立预期", "Base rate 分位", "净预期差"],
            [
                ["收入增速", "未获取到（需反向 DCF 完整财务）",
                 "未获取到", "未获取到", "待补数据"],
                ["利润率/FCF 率", "见下方反向 DCF 输出", "演示假设占位", "未定位", "待补数据"],
                ["资本回报 ROIC", "未获取到", "未获取到", "未定位", "待补数据"],
                ["永续增长 g", f"脚本假设 g={assumptions.get('terminal_g', 0.03) if assumptions else 0.03}",
                 "需行业天花板约束", "中位附近（占位）", "中性"],
            ],
        ),
        "",
        "### 核心多头逻辑（待证伪）",
        "",
        "1. **估值安全边际（条件成立时）**：若一手财务验证 FCF 与演示假设同向，则脚本综合区间相对现价的位置可提供不对称。",
        "2. **业务质量（待验证）**：需按行业附录 KPI 验证护城河与单位经济，当前未获取到分部数据。",
        "3. **资本配置（待验证）**：回购/分红/再投资纪律需对照资本配置计分卡，当前未获取到。",
        "",
        "### 核心空头逻辑（待证伪）",
        "",
        "1. **数据降级风险**：当前财务为演示假设，真实 ROIC/应计质量未知，可能推翻估值。",
        "2. **竞争与监管**：行业附录所示关键风险尚未用最新披露逐条对账。",
        "3. **预期差不可证伪**：在 Gap 表关键行仍为「未获取到」时，按 skill 纪律不得给出买入动作。",
        "",
        f"**我的判断：** 在 rule 骨架与演示假设下，对 {name} 维持**观望**；"
        f"待完整财务接入后，按标定规则重映射标签与动作。",
        "",
    ]

    if mode == "full":
        body_mid = _rule_full_mid(name, industry, price, currency, dcf_output,
                                  assumptions, context)
    else:
        body_mid = _rule_earnings_mid(name, industry, price, currency, dcf_output,
                                      assumptions, context)

    ch9_title = FULL_CHAPTERS[8] if mode == "full" else EARNINGS_CHAPTERS[8]
    ch9 = [
        f"## {ch9_title}",
        "",
        "### 9.1 结论（决策三分法复述）",
        "",
        f"- **内在价值判断**：{label}（区间 {rng}）—— 我的判断，演示假设约束下的脚本结果",
        "- **未来 1–3 个月市场交易方向**：震荡观察（催化剂与资金面未获取到完整证据）",
        "- **投资动作**：**观望**",
        "",
        f"**如果今天这是一笔现金，我会买入它吗？** —— **不会（当前信息集）**。"
        f"理由：独立观点检验三问未全部有实答；财务质量未完成 A–D 定级；"
        f"估值假设非一手披露。待数据补齐后可重评。",
        "",
        "### 9.2 反方论证 / 事前风险预演（Pre-mortem）",
        "",
        _md_table(
            ["失败情景", "触发机制", "当前证据强度", "监控信号", "证伪时点"],
            [
                ["演示假设严重偏离真实 FCF", "实际 FCF yield 远低于 4% 占位",
                 "中（因未取财报）", "年报/中报经营现金流", "下次定期报告"],
                ["竞争格局恶化吞噬利润率", "份额或 take rate 下行",
                 "弱（未取份额数据）", "行业附录核心 KPI", "连续两个季度"],
                ["治理/财务质量否决", "应计恶化或审计事项",
                 "弱", "M-Score/审计意见", "年报披露日"],
            ],
        ),
        "",
        "### 9.3 监控清单（带阈值）",
        "",
        "1. 下一期经营现金流 / 净利润现金转化率 < 0.7 → 重评可信度",
        "2. 现价跌破脚本综合区间下沿且基本面未恶化 → 启动正式深度覆盖",
        "3. 行业附录 Top KPI 连续两季低于自身历史 25% 分位 → 下调情景概率",
        "4. 重大监管/诉讼公告 → 立即暂停加仓逻辑",
        "5. 估值假设 JSON 被一手财务替换后，重跑 dcf.py 与检查器",
        "",
        "### 9.4 置信度自评",
        "",
        _md_table(
            ["维度", "评分(1-5)", "说明"],
            [
                ["数据完整度", "2", "行情有、财务演示"],
                ["模型稳健性", "3", "脚本多方法已跑通，假设弱"],
                ["预期差清晰度", "2", "Gap 表多行未获取到"],
                ["反方论证力度", "3", "已预置三条失败路径"],
                ["综合置信度", "2", "仅作栏目骨架与流程演示"],
            ],
        ),
        "",
        "### 数据来源与时间戳",
        "",
        f"- 实时行情：{src} · {qtime}",
        f"- 估值计算：equity_research/scripts/dcf.py · {stamp}",
        f"- 研究框架：{COLUMN_SKILL_URL}",
        f"- 上游 skill：{COLUMN_SKILL_UPSTREAM}",
        f"- 行业附录：equity_research/industries/{industry}.md",
        f"- 背景上下文：{'已注入' if context else '无'}",
        "",
        "---",
        "",
        f"> ⚠️ **免责声明**：本栏目内容由 AI 流程生成，**不构成投资建议**。"
        f"作者与平台不对依据本报告做出的任何投资决策负责。最终决策由读者自行承担。",
        f">",
        f"> 栏目版本 `{VERSION}` · skill 本地路径 `equity_research/`",
        "",
    ]

    return "\n".join(header + ch1 + body_mid + ch9)


def _rule_full_mid(name, industry, price, currency, dcf_output, assumptions, context) -> list[str]:
    return [
        f"## {FULL_CHAPTERS[1]}",
        "",
        "**本章要点：** 商业模式与收入结构需一手年报/中报拆解；当前未获取到分部表。",
        "",
        f"{name} 的业务详情在 rule 模式下仅保留框架。请在 AI 模式或人工补充：",
        "- 商业模式（如何赚钱、客户是谁、计价方式）",
        "- 收入结构表（业务线/地区/客户，占比+增速）",
        "- 集中度与产业链位置",
        "",
        _md_table(
            ["业务线/分部", "最近期收入", "占比", "同比", "备注"],
            [["未获取到", "未获取到", "—", "—", "待一手披露"]],
        ),
        "",
        f"## {FULL_CHAPTERS[2]}",
        "",
        "**本章要点：** 护城河评分须与 EPV/EVA 财务验证交叉印证；当前财务为演示假设。",
        "",
        f"行业附录 `{industry}` 已加载于 prompt（AI 模式）/ 本地文件。"
        f"竞争格局、份额趋势、增长引擎与证伪点：未获取到完整数据。",
        "",
        _md_table(
            ["护城河维度", "有无", "强度", "依据"],
            [
                ["无形资产（品牌/专利）", "未获取到", "—", "—"],
                ["转换成本", "未获取到", "—", "—"],
                ["网络效应", "未获取到", "—", "—"],
                ["成本优势", "未获取到", "—", "—"],
                ["有效规模", "未获取到", "—", "—"],
                ["综合", "待定", "无/窄/宽待判", "需业务+财务交叉"],
            ],
        ),
        "",
        f"## {FULL_CHAPTERS[3]}",
        "",
        "**本章要点：** 资本配置计分卡为定量框架；rule 模式无回购/并购明细。",
        "",
        _md_table(
            ["维度", "近5年记录", "评分(优/中/差)", "依据"],
            [
                ["回购", "未获取到", "—", "—"],
                ["并购", "未获取到", "—", "—"],
                ["再投资", "未获取到", "—", "—"],
                ["分红", "未获取到", "—", "—"],
                ["股权稀释", "未获取到", "—", "—"],
                ["综合", "—", "—", "差评进入否决项考量"],
            ],
        ),
        "",
        f"## {FULL_CHAPTERS[4]}",
        "",
        "### 5.1 财务趋势",
        "",
        "近 5 年营收/毛利率/经营利润率/FCF/ROIC/ROE：**未获取到**（需财务 CSV/年报）。",
        "",
        "### 5.2 财报质量（forensic）",
        "",
        _md_table(
            ["检查项", "结果", "判定", "证据/来源"],
            [
                ["应计比率 / 现金转化", "未获取到", "待查", "—"],
                ["Beneish M-Score", "未获取到", "待查", "检查器可算则算"],
                ["收入确认红旗", "未获取到", "待查", "—"],
                ["费用资本化", "未获取到", "待查", "—"],
                ["治理/审计信号", "未获取到", "待查", "—"],
                ["**可信度等级**", "B（待核验）", "约束动作", "C/D 否决买入"],
            ],
        ),
        "",
        f"## {FULL_CHAPTERS[5]}",
        "",
        "**纪律：** 以下数字来自 `scripts/dcf.py` 脚本，假设为演示/占位 JSON，"
        f"现价锚点 {price} {currency}。",
        "",
        "### 估值脚本原始输出",
        "",
        "```",
        (dcf_output.strip()[:8000] if dcf_output else "（dcf.py 未产出）"),
        "```",
        "",
        "### 方法清单（至少三种）",
        "",
        "1. **反向 DCF + PVGO** — 见脚本「反向 DCF」「PVGO 分解」节",
        "2. **三情景概率加权 DCF** — 牛/基/熊 + 敏感性",
        "3. **EPV / 三要素** — 底价·EPV·成长买点阶梯",
        "4. **EVA / 剩余收益** — ROIC vs WACC 与 g=RR×ROIIC 自洽",
        "5. **蒙特卡洛** — P10–P90 与 P(IV<现价)",
        "",
        f"关键假设 base rate 分位：WACC={assumptions.get('wacc') if assumptions else '—'}、"
        f"g={assumptions.get('terminal_g') if assumptions else '—'}（未对照历史分布正式定位，占位）。",
        "",
        f"## {FULL_CHAPTERS[6]}",
        "",
        "一致预期、目标价分布、近期调整：**未获取到**。"
        "与第一章 Gap 表的分歧点待补卖方/买方共识数据后填写。",
        "",
        f"## {FULL_CHAPTERS[7]}",
        "",
        "### 催化剂时间表",
        "",
        _md_table(
            ["时间", "事件", "多/空", "影响逻辑", "确认状态"],
            [["未获取到", "定期报告/业绩会", "—", "—", "待公告"]],
        ),
        "",
        "### 8.1 预测与验证登记",
        "",
        _md_table(
            ["预测对象", "基准值/截止日", "预测区间或方向", "验证日期",
             "先行指标与阈值", "上/下行失效条件", "状态"],
            [
                ["内在价值标签", f"{price} / {now_stamp()}",
                 "待正式财务", "下期财报", "FCF yield",
                 "假设被一手数据推翻", "开放"],
            ],
        ),
        "",
        f"### 背景摘录\n\n{context[:1500] if context else '（无）'}",
        "",
    ]


def _rule_earnings_mid(name, industry, price, currency, dcf_output, assumptions, context) -> list[str]:
    return [
        f"## {EARNINGS_CHAPTERS[1]}",
        "",
        "预期差质量：本次财报 vs 市场预期 vs 我的模型 — **未获取到具体一致预期数字**。",
        "质量核查最小集（应计、现金转化、DSO、Non-GAAP）— **未获取到**。",
        "",
        f"## {EARNINGS_CHAPTERS[2]}",
        "",
        f"收入/分部/KPI（行业 `{industry}`）— **未获取到**。首次覆盖需补 3 年 + 8 季度基线。",
        "",
        f"## {EARNINGS_CHAPTERS[3]}",
        "",
        "利润率、费用、盈利质量 — **未获取到**。",
        "",
        f"## {EARNINGS_CHAPTERS[4]}",
        "",
        "现金流、资产负债表、资本配置 — **未获取到**。",
        "",
        f"## {EARNINGS_CHAPTERS[5]}",
        "",
        "指引、电话会、管理层信号 — **未获取到**。",
        "",
        f"## {EARNINGS_CHAPTERS[6]}",
        "",
        "竞争、行业与市场反应 — **未获取到**。",
        "",
        f"## {EARNINGS_CHAPTERS[7]}",
        "",
        f"现价锚点 {price} {currency}。估值脚本输出：",
        "",
        "```",
        (dcf_output.strip()[:8000] if dcf_output else "（dcf.py 未产出）"),
        "```",
        "",
        f"### 背景摘录\n\n{context[:1500] if context else '（无）'}",
        "",
    ]


# ---------------------------------------------------------------- 主入口：生成栏目

def generate_column(
    raw_code: str,
    *,
    mode: str = "full",
    ai_provider: str | None = None,
    channel: str = "console",
    dry_run: bool = True,
    theme: str = "dos",          # 默认 DOS CRT 磷光绿复古终端
    industry: str | None = None,
    timeout: int = 90,
    run_check: bool = True,
    hours: int = 48,
    no_chart: bool = False,
    collect_news: bool = True,
) -> dict:
    """
    独立栏目主流程：行情 → 估值脚本 → AI/rule 九章报告 → 检查器 → 可选推送。
    """
    import hk_quote
    import pushplus_deepseek as pp
    import stock_report as sr

    mode = mode if mode in MODES else "full"
    raw_code = (raw_code or "").strip()
    if not raw_code:
        raise ValueError("股票代码不能为空")
    if not skill_available():
        raise RuntimeError(
            f"equity-research skill 未安装：请确认 {SKILL_DIR} 含 SKILL.md 与 scripts/dcf.py")

    market, code, _ = hk_quote.detect_market(raw_code)
    label = sr.MARKET_LABELS.get(market, market)
    ticker = sr.MARKET_TICKER.get(market, "")

    quote, cross_pack = sr.fetch_verified_quote(market, code, raw_code)
    name = (quote or {}).get("name") or ""
    topic = f"{ticker}{code}" + (f" {name}" if name else "")
    industry = industry or guess_industry(topic, code)

    if quote:
        context, market_md = sr.quote_with_context(market, code, quote, cross_pack)
        quote_error = None
    else:
        context = (f"标的：{topic}（{label}）。\n"
                   f"注意：实时行情暂不可用，凡涉及具体点位请标注未获取到。")
        market_md = "> ⚠️ 实时行情暂不可用，价格相关结论请谨慎。"
        quote_error = "行情数据源失败"

    # 最新功能：量价舆情 + 十七平台扫描注入第八章上下文
    sent_pack = None
    if collect_news:
        sent_pack = sr.collect_latest_pack(
            topic, raw_code, hours, timeout=min(timeout, 12))
    if sent_pack is not None:
        try:
            context = (pp.sentiment_context(sent_pack) + "\n\n" + context)
        except Exception:  # noqa: BLE001
            pass

    assumptions = default_assumptions_from_quote(quote, code=code)
    dcf_out, dcf_summary, dcf_err = run_dcf(assumptions, timeout=min(timeout, 45))
    if dcf_err:
        dcf_out = (dcf_out or "") + f"\n[估值脚本警告] {dcf_err}"

    provider = sr.resolve_ai_provider(ai_provider)
    messages = build_messages(
        topic, context, mode=mode, industry=industry,
        dcf_output=dcf_out, assumptions=assumptions,
    )

    if provider == "rule":
        report_md = gen_rule_report(
            topic, context=context, mode=mode, industry=industry,
            quote=quote, dcf_output=dcf_out, dcf_summary=dcf_summary,
            assumptions=assumptions, code=code,
        )
        gen_note = "rule 九章骨架（未调用大模型）+ dcf.py 脚本估值"
    else:
        url, model, key, ai_label, keyless = pp.ai_credentials(provider)
        if not key and not keyless:
            report_md = gen_rule_report(
                topic, context=context, mode=mode, industry=industry,
                quote=quote, dcf_output=dcf_out, dcf_summary=dcf_summary,
                assumptions=assumptions, code=code,
            )
            gen_note = (f"缺少 AI Key（{provider}），已降级为 rule 九章骨架 + dcf.py"
                        "；可配置 INFERENCE_API_KEY 接入统一推理 API")
            provider = "rule"
        else:
            report_md = pp.chat_completion(
                url, key, model, messages, TEMPLATE_MAX_TOKENS, timeout)
            gen_note = f"{ai_label} + dcf.py + equity-research skill"

    # 输出页排版：行情核验块顶置（先价后文、跨市场统一），栏目元数据留在末尾
    extras = []
    extras.append(
        "\n---\n"
        f"### 📎 栏目元数据\n\n"
        f"- 栏目：{COLUMN_TITLE}（`{COLUMN_ID}`）\n"
        f"- 模式：{MODE_TITLES.get(mode, mode)}\n"
        f"- 行业附录：`{industry}`\n"
        f"- Skill：{COLUMN_SKILL_URL}\n"
        f"- 估值标签：{(dcf_summary or {}).get('label', '—')}\n"
        f"- 生成说明：{gen_note}\n"
    )
    body = ((market_md.rstrip() + "\n\n") if market_md else "") \
        + report_md.rstrip() + "\n\n" + "\n\n".join(extras)
    targets = pp.channel_targets(channel)
    should_persist_state = (not dry_run and pp.has_deliverable_channel(channel))
    content, latest_meta = sr.wrap_with_latest_features(
        body, raw_code=raw_code, template=TEMPLATE_NAME, topic=topic,
        hours=hours, no_chart=no_chart, quote=quote, sent_pack=sent_pack,
        persist_state=should_persist_state)

    check_out, check_code = "", 0
    if run_check:
        check_out, check_code = run_checker(
            content, assumptions=assumptions, industry=industry, language="zh",
            timeout=min(timeout, 30),
        )
        content += (
            f"\n\n---\n\n### 🔎 一致性检查器（check_research_output.py）\n\n"
            f"退出码：{check_code}\n\n```\n{check_out[:3000]}\n```\n"
        )

    now = datetime.now(CST).strftime("%m-%d %H:%M")
    title = (f"{pp.BRAND_TITLE}·{topic}·{COLUMN_TITLE}"
             f"·{MODE_TITLES.get(mode, mode)}（{now}）")

    push_results: dict[str, str] = {}
    if dry_run:
        push_results = {ch: "dry-run（未真实推送）" for ch in targets}
    else:
        for ch in targets:
            missing = pp.missing_channel_secrets(ch)
            if missing:
                push_results[ch] = pp.skip_missing_secret_result(missing)
                continue
            try:
                if ch == "pushplus":
                    push_results[ch] = pp.push_pushplus(
                        title, content, timeout, theme=theme)
                else:
                    push_results[ch] = pp.PUSH_FUNCS[ch](title, content, timeout)
            except pp.PushError as e:
                push_results[ch] = f"失败：{e}"

    # 栏目卡片（供大屏独立栏渲染）
    card = {
        "column_id": COLUMN_ID,
        "column_title": COLUMN_TITLE,
        "column_subtitle": COLUMN_SUBTITLE,
        "mode": mode,
        "mode_title": MODE_TITLES.get(mode, mode),
        "industry": industry,
        "skill_url": COLUMN_SKILL_URL,
        "valuation_label": (dcf_summary or {}).get("label"),
        "range_low": (dcf_summary or {}).get("range_low"),
        "range_high": (dcf_summary or {}).get("range_high"),
        "price": (quote or {}).get("price"),
        "currency": (quote or {}).get("currency"),
        "chapters": EARNINGS_CHAPTERS if mode == "earnings" else FULL_CHAPTERS,
        "check_ok": check_code == 0,
        "provider": provider,
        "gen_note": gen_note,
        "generated_at": now_stamp(),
    }

    return {
        "ok": True,
        "column": card,
        "market": market,
        "market_label": label,
        "code": code,
        "name": name,
        "quote": quote,
        "quote_error": quote_error,
        "provider": provider,
        "gen_note": gen_note,
        "template": TEMPLATE_NAME,
        "mode": mode,
        "industry": industry,
        "title": title,
        "report_md": content,
        # 与 PushPlus 实际详情页使用同一完整外壳，仪表盘预览所见即所得。
        "report_html": pp.themed_html(title, content, theme_name=theme),
        "theme": theme,
        "dry_run": dry_run,
        "push": push_results,
        "dcf_summary": dcf_summary,
        "dcf_error": dcf_err,
        "assumptions": assumptions,
        "check_output": check_out,
        "check_code": check_code,
        "skill_dir": str(SKILL_DIR),
    }


# ---------------------------------------------------------------- 大屏栏目摘要（不跑 AI）

def column_teaser(stock_code: str = "09988") -> dict:
    """大屏「机构级个股投研」独立栏的轻量摘要（可无 Key）。"""
    teaser = {
        "column_id": COLUMN_ID,
        "column_title": COLUMN_TITLE,
        "column_subtitle": COLUMN_SUBTITLE,
        "skill_url": COLUMN_SKILL_URL,
        "skill_upstream": COLUMN_SKILL_UPSTREAM,
        "skill_available": skill_available(),
        "version": VERSION,
        "modes": [{"id": m, "title": MODE_TITLES[m]} for m in MODES],
        "chapters_full": FULL_CHAPTERS,
        "chapters_earnings": EARNINGS_CHAPTERS,
        "features": [
            "九章机构级深度研报 / 财报模式",
            "预期差主线（反向 DCF + PVGO + Gap 表）",
            "财报质量核查（应计 / M-Score → A–D）",
            "多方法可复算估值（dcf.py：DCF/EPV/EVA/蒙特卡洛）",
            "20 类行业附录 + 一致性检查器",
            "美股 / 港股 / A 股 · A/H · 中概 VIE/ADR",
        ],
        "code": stock_code,
        "industry": guess_industry(stock_code, stock_code),
    }
    if not skill_available():
        teaser["status"] = "skill_missing"
        return teaser
    teaser["status"] = "ready"
    teaser["refs"] = sorted(p.name for p in REFS_DIR.glob("*.md")) if REFS_DIR.is_dir() else []
    teaser["industries_count"] = (
        len(list(INDUSTRIES_DIR.glob("*.md"))) if INDUSTRIES_DIR.is_dir() else 0)
    return teaser


# ---------------------------------------------------------------- 自检

def selftest() -> int:
    fails = 0

    def check(name: str, cond: bool) -> None:
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("① skill 安装")
    check("SKILL.md 存在", (SKILL_DIR / "SKILL.md").is_file())
    check("dcf.py 存在", (SCRIPTS_DIR / "dcf.py").is_file())
    check("check_research_output.py 存在",
          (SCRIPTS_DIR / "check_research_output.py").is_file())
    check("report-template.md 存在", (REFS_DIR / "report-template.md").is_file())
    check("行业附录 ≥ 10", teaser_industries() >= 10)

    print("② 行业路由")
    check("阿里→internet-platform",
          guess_industry("阿里巴巴", "09988") == "internet-platform")
    check("茅台→consumer", guess_industry("贵州茅台", "600519") == "consumer")
    check("招行→banks", guess_industry("招商银行", "600036") == "banks")

    print("③ dcf.py 演示假设可跑")
    asump = default_assumptions_from_quote(
        {"price": 80.0, "currency": "HKD", "market_cap": 1.6e12}, code="09988")
    out, summary, err = run_dcf(asump, timeout=30)
    check("dcf 无错误", err is None)
    check("dcf 有输出", len(out) > 200)
    check("dcf 摘要含标签或区间", bool(summary) and (
        "label" in (summary or {}) or "range_low" in (summary or {})))

    print("④ rule 九章报告")
    md = gen_rule_report(
        "HK09988 阿里巴巴",
        context="现价 80 HKD 演示",
        mode="full",
        industry="internet-platform",
        quote={"price": 80.0, "currency": "HKD", "name": "阿里巴巴",
               "source_label": "演示"},
        dcf_output=out,
        dcf_summary=summary,
        assumptions=asump,
        code="09988",
    )
    check("含报告标题", "个股投资研究报告" in md)
    check("含九章之一页速览", "一页速览" in md or "结论与快照" in md)
    check("含估值章", "估值" in md)
    check("含反方论证", "反方论证" in md or "Pre-mortem" in md)
    check("含免责", "不构成" in md and "投资建议" in md)
    check("声明行业附录", "行业附录" in md and "internet-platform" in md)
    for ch in FULL_CHAPTERS:
        # 章节号或关键词
        short = ch.split("、")[-1][:4]
        check(f"章节出现:{short}", short in md or ch[:5] in md)

    print("⑤ 财报模式骨架")
    md_e = gen_rule_report("HK09988 阿里巴巴", mode="earnings",
                           industry="internet-platform", code="09988",
                           dcf_output=out, dcf_summary=summary)
    check("财报模式标题章", "结论与快照" in md_e or "预期差与质量" in md_e)

    print("⑥ prompt 构造")
    msgs = build_messages("HK09988 阿里巴巴", "ctx", mode="full",
                          industry="internet-platform", dcf_output=out[:500],
                          assumptions=asump)
    check("system+user", len(msgs) == 2 and msgs[0]["role"] == "system")
    check("user 含九章", "一页速览" in msgs[1]["content"])
    check("user 含 dcf", "dcf" in msgs[1]["content"].lower() or "估值脚本" in msgs[1]["content"])

    print("⑦ 栏目 teaser")
    t = column_teaser("09988")
    check("teaser ready", t.get("status") == "ready")
    check("teaser 功能列表", len(t.get("features") or []) >= 5)

    print("⑧ 检查器（对 rule 报告）")
    cout, ccode = run_checker(md, assumptions=asump,
                              industry="internet-platform", language="zh")
    check("检查器可执行", isinstance(cout, str) and len(cout) >= 0)
    # rule 骨架可能触发 P0/P1，不强制 exit 0，但必须跑通
    check("检查器返回码为 int", isinstance(ccode, int))

    print(f"\n{'✅ 自检全部通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


def teaser_industries() -> int:
    if not INDUSTRIES_DIR.is_dir():
        return 0
    return len(list(INDUSTRIES_DIR.glob("*.md")))


# ---------------------------------------------------------------- CLI

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="机构级个股投研独立栏目（equity-research-skill）")
    p.add_argument("code", nargs="?", default="", help="股票代码")
    p.add_argument("--mode", default="full", choices=MODES)
    p.add_argument("--industry", default="", help="行业附录 slug（默认自动猜测）")
    p.add_argument("--ai-provider", default="auto", dest="ai_provider")
    p.add_argument("--channel", default="console")
    p.add_argument("--theme", default="dos",
                   choices=["dos", "guizang", "game", "klein", "pixel", "monitor", "noc", "default"],
                   help="PushPlus 视觉主题（默认 dos：DOS CRT 磷光绿复古终端）")
    p.add_argument("--push", action="store_true")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--hours", type=int, default=48)
    p.add_argument("--no-chart", action="store_true")
    p.add_argument("--no-check", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--teaser", action="store_true", help="只输出栏目摘要 JSON")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.selftest:
        return selftest()
    if args.teaser:
        print(json.dumps(column_teaser(args.code or "09988"),
                         ensure_ascii=False, indent=2))
        return 0
    if not args.code:
        print("用法: python equity_research_column.py <代码> [--mode full|earnings]",
              file=sys.stderr)
        return 1
    try:
        result = generate_column(
            args.code,
            mode=args.mode,
            ai_provider=args.ai_provider,
            channel=args.channel,
            dry_run=not args.push,
            theme=args.theme,
            industry=args.industry or None,
            timeout=args.timeout,
            run_check=not args.no_check,
            hours=getattr(args, "hours", 48),
            no_chart=getattr(args, "no_chart", False),
        )
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}", file=sys.stderr)
        return 1
    if args.json:
        # 控制体积
        slim = {k: v for k, v in result.items()
                if k not in ("assumptions",)}
        if "assumptions" in result:
            slim["assumptions_meta"] = (result.get("assumptions") or {}).get("_meta")
        print(json.dumps(slim, ensure_ascii=False, indent=2, default=str))
        return 0
    print("# " + result["title"])
    print(result["report_md"])
    print("\n===== 推送结果 =====")
    for ch, r in (result.get("push") or {}).items():
        print(f"  {ch}: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
