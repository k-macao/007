#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skills_hub.py — 投研 Skills 目录与运行器

把 Agent Skill（SKILL.md 包）接到现有研报管线：
  · rollingSirius equity-research（九章 + dcf.py）仍走 equity_research_column
  · Anthropic financial-services 官方 9 个 equity-research skills
    作为可执行模板（晨会 / 首次覆盖 / 财报前瞻 / 季报更新 / 模型修订 /
    催化剂日历 / 论点记分卡 / 行业格局 / 选股扫描）

用法：
  python skills_hub.py --list
  python skills_hub.py --selftest
  python skills_hub.py 09988 --skill morning_note --ai-provider rule
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SKILLS_DIR = ROOT / "skills"
ANTH_DIR = SKILLS_DIR / "anthropic-equity-research"
CATALOG_PATH = SKILLS_DIR / "catalog.json"

CST = timezone(timedelta(hours=8), "CST")
VERSION = "1.1.0-skills-2026-08-13"

# template 名（对接 stock_report / pushplus / Actions）→ skill 元数据
SKILL_TEMPLATES: dict[str, dict[str, Any]] = {
    "initiate": {
        "id": "initiating-coverage",
        "title": "首次覆盖 · 公司研究",
        "dir": ANTH_DIR / "initiating-coverage",
        "refs": ["references/task1-company-research.md"],
        "max_tokens": 6000,
        "outline": [
            "公司概览与历史",
            "管理层与治理",
            "产品与服务",
            "行业与竞争",
            "TAM 与增长引擎",
            "风险清单（8–12 条）",
            "下一步（Task 2 财务建模）",
        ],
        "note": "自动化入口执行 Task 1（公司研究）。完整 5-task 流水线见 SKILL.md。",
    },
    "earnings_preview": {
        "id": "earnings-preview",
        "title": "财报前瞻",
        "dir": ANTH_DIR / "earnings-preview",
        "refs": [],
        "max_tokens": 3500,
        "outline": [
            "一致预期表",
            "关键观察指标（按重要性）",
            "牛/基/熊情景与股价含义",
            "催化剂清单",
            "交易设置（隐含波动 / 历史反应）",
        ],
    },
    "earnings_update": {
        "id": "earnings-analysis",
        "title": "季报更新",
        "dir": ANTH_DIR / "earnings-analysis",
        "refs": ["references/report-structure.md"],
        "max_tokens": 5000,
        "outline": [
            "结论框（评级 / 目标价 / beat-miss）",
            "关键指标 vs 预期",
            "分部与质量",
            "指引变化",
            "论点与估值修订",
            "来源清单",
        ],
        "note": "交付 Markdown 季报更新（非 8–12 页 DOCX）。缺最新财报写「未获取到」。",
    },
    "model_update": {
        "id": "model-update",
        "title": "模型修订",
        "dir": ANTH_DIR / "model-update",
        "refs": [],
        "max_tokens": 3500,
        "outline": [
            "触发事项",
            "实际 vs 原估计",
            "前瞻假设修订",
            "估值影响",
            "动作（维持/调整评级与目标价）",
        ],
    },
    "morning_note": {
        "id": "morning-note",
        "title": "晨会纪要",
        "dir": ANTH_DIR / "morning-note",
        "refs": [],
        "max_tokens": 2500,
        "outline": [
            "Top Call（一句话）",
            "隔夜/盘前要点",
            "今日关键事件",
            "交易想法（含证伪）",
        ],
    },
    "catalysts": {
        "id": "catalyst-calendar",
        "title": "催化剂日历",
        "dir": ANTH_DIR / "catalyst-calendar",
        "refs": [],
        "max_tokens": 3000,
        "outline": [
            "覆盖范围与时间窗",
            "日历表（日期/事件/影响/仓位）",
            "本周重点",
            "下周预告",
            "仓位含义",
        ],
    },
    "thesis": {
        "id": "thesis-tracker",
        "title": "论点记分卡",
        "dir": ANTH_DIR / "thesis-tracker",
        "refs": [],
        "max_tokens": 3000,
        "outline": [
            "论点陈述（可证伪）",
            "支柱记分卡",
            "更新日志",
            "催化剂",
            "当前信念与动作",
        ],
    },
    "sector": {
        "id": "sector-overview",
        "title": "行业格局",
        "dir": ANTH_DIR / "sector-overview",
        "refs": [],
        "max_tokens": 4500,
        "outline": [
            "市场体量与结构",
            "趋势与驱动",
            "竞争格局（5–10 家）",
            "估值中枢",
            "投资含义",
        ],
    },
    "ideas": {
        "id": "idea-generation",
        "title": "选股/主题扫描",
        "dir": ANTH_DIR / "idea-generation",
        "refs": [],
        "max_tokens": 4000,
        "outline": [
            "筛选标准",
            "主题价值链",
            "短名单（5–10）",
            "对比表",
            "优先研究顺序",
        ],
    },
}

# 兼容别名
ALIASES = {
    "initiating-coverage": "initiate",
    "initiating_coverage": "initiate",
    "earnings-preview": "earnings_preview",
    "earnings-analysis": "earnings_update",
    "earnings_analysis": "earnings_update",
    "model-update": "model_update",
    "morning-note": "morning_note",
    "catalyst-calendar": "catalysts",
    "catalyst_calendar": "catalysts",
    "thesis-tracker": "thesis",
    "thesis_tracker": "thesis",
    "sector-overview": "sector",
    "sector_overview": "sector",
    "idea-generation": "ideas",
    "idea_generation": "ideas",
}

TEMPLATE_TITLES = {k: v["title"] for k, v in SKILL_TEMPLATES.items()}
TEMPLATE_MAX_TOKENS = {k: v["max_tokens"] for k, v in SKILL_TEMPLATES.items()}


def now_stamp() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M CST")


def normalize_skill(name: str) -> str:
    n = (name or "").strip().lower().replace(" ", "_")
    return ALIASES.get(n, n)


def is_skill_template(name: str) -> bool:
    return normalize_skill(name) in SKILL_TEMPLATES


def skill_dir_ok(meta: dict) -> bool:
    d = meta.get("dir")
    return bool(d) and (Path(d) / "SKILL.md").is_file()


def parse_frontmatter(text: str) -> tuple[dict, str]:
    meta: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].lstrip("\n")
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
    return meta, body


def read_skill_md(meta: dict, max_chars: int = 8000) -> str:
    path = Path(meta["dir"]) / "SKILL.md"
    if not path.is_file():
        return f"（未找到 {path}）"
    raw = path.read_text(encoding="utf-8")
    _fm, body = parse_frontmatter(raw)
    if len(body) > max_chars:
        return body[:max_chars] + "\n\n…（SKILL.md 已截断）"
    return body


def read_refs(meta: dict, max_chars: int = 4000) -> str:
    chunks: list[str] = []
    base = Path(meta["dir"])
    for rel in meta.get("refs") or []:
        p = base / rel
        if not p.is_file():
            chunks.append(f"（未找到 {rel}）")
            continue
        text = p.read_text(encoding="utf-8")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n…（{rel} 已截断）"
        chunks.append(f"### {rel}\n\n{text}")
    return "\n\n".join(chunks)


def list_skills() -> list[dict]:
    items: list[dict] = []
    # rollingSirius
    eq_ok = (ROOT / "equity_research" / "SKILL.md").is_file()
    items.append({
        "id": "equity-research",
        "template": "equity",
        "title": "机构级个股投研",
        "family": "rollingSirius",
        "version": "v3.0.0",
        "installed": eq_ok,
        "path": "equity_research",
        "upstream": "https://github.com/rollingSirius/equity-research-skill",
    })
    for tmpl, meta in SKILL_TEMPLATES.items():
        items.append({
            "id": meta["id"],
            "template": tmpl,
            "title": meta["title"],
            "family": "anthropic",
            "version": "2026-08-04",
            "installed": skill_dir_ok(meta),
            "path": str(Path(meta["dir"]).relative_to(ROOT)),
            "upstream": "https://github.com/anthropics/financial-services",
            "outline": meta["outline"],
            "note": meta.get("note") or "",
        })
    return items


def catalog_teaser(stock_code: str = "09988") -> dict:
    items = list_skills()
    n_ok = sum(1 for s in items if s["installed"])
    return {
        "ok": True,
        "version": VERSION,
        "updated": "2026-08-13",
        "code": stock_code,
        "total": len(items),
        "installed": n_ok,
        "missing": len(items) - n_ok,
        "skills": items,
        "families": {
            "rollingSirius": "equity-research-skill v3.0.0（九章 + dcf.py）",
            "anthropic": "financial-services equity-research 官方 9 技能（Apache-2.0）",
        },
    }


def build_system_prompt(tmpl: str) -> str:
    import pushplus_deepseek as pp
    meta = SKILL_TEMPLATES[tmpl]
    skill_body = read_skill_md(meta, 7000)
    refs = read_refs(meta, 3500)
    extra = f"\n\n## 参考文件\n{refs}" if refs else ""
    return (
        "你是一个投资顾问，也是一位卖方/买方结合、经验丰富的二级市场投研分析师，正在执行一个机构级 Agent Skill。\n"
        f"{pp.DEAI_ADVISOR_STYLE_GUIDE}\n\n"
        "遵守 skill 的工作流、输出结构和纪律。\n"
        "关键数据必须标注来源与时间；缺失写「未获取到」，禁止用记忆填充具体财务数字。\n"
        "事实与「我的判断」分离。输出简体中文 Markdown，不要寒暄，不要用代码块包整篇。\n"
        "末尾必须有免责声明：本报告不构成投资建议。\n"
        f"交付物是一份 Markdown 纪要/备忘录（不要假装已生成 DOCX/XLSX）。\n\n"
        f"## Skill：{meta['id']}（{meta['title']}）\n\n{skill_body}{extra}\n"
    )


def build_user_prompt(tmpl: str, topic: str, context: str) -> str:
    meta = SKILL_TEMPLATES[tmpl]
    outline = "\n".join(f"  {i+1}. {x}" for i, x in enumerate(meta["outline"]))
    note = meta.get("note") or ""
    return (
        f"请按上述 skill 为「{topic}」产出一份「{meta['title']}」。\n"
        f"**撰写时间**：{now_stamp()}\n"
        f"{note}\n\n"
        f"## 实时行情与背景（优先采用）\n{context or '（无额外背景）'}\n\n"
        f"## 必须覆盖的结构\n{outline}\n\n"
        "表格用 Markdown。数字缺失写「未获取到」。给出明确观点，不要只复述新闻。\n"
        f"标题格式：`# {topic} · {meta['title']}`\n"
        "结尾附来源清单与免责声明。\n"
    )


def build_messages(tmpl: str, topic: str, context: str = "") -> list[dict]:
    tmpl = normalize_skill(tmpl)
    if tmpl not in SKILL_TEMPLATES:
        raise ValueError(f"未知 skill 模板: {tmpl}")
    return [
        {"role": "system", "content": build_system_prompt(tmpl)},
        {"role": "user", "content": build_user_prompt(tmpl, topic, context)},
    ]


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def gen_rule_skill(
    tmpl: str,
    topic: str,
    context: str = "",
    quote: dict | None = None,
    code: str = "",
) -> str:
    """离线可交付的 skill 骨架（标注数据缺口，不伪装成已完成尽调）。"""
    tmpl = normalize_skill(tmpl)
    meta = SKILL_TEMPLATES[tmpl]
    stamp = now_stamp()
    price = currency = chg = pe = src = qtime = "未获取到"
    name = topic
    if quote:
        if quote.get("price") is not None:
            price = f"{quote['price']:.3f}"
        currency = quote.get("currency") or "—"
        if quote.get("change_pct") is not None:
            chg = f"{quote['change_pct']:+.2f}%"
        if quote.get("pe") is not None:
            pe = str(quote["pe"])
        if quote.get("name"):
            name = quote["name"]
        from source_names import generalize_source
        src = generalize_source(quote.get("source_label") or quote.get("source") or "未获取到")
        qtime = quote.get("time") or quote.get("fetched_at") or stamp

    head = [
        f"# {name}（{code or topic}）· {meta['title']}",
        "",
        f"**撰写：{stamp}｜行情：{price} {currency}（{chg}）PE {pe}**",
        f"**Skill：`{meta['id']}` · 模板 `{tmpl}` · 机构级投研框架**",
        "",
        "> 本篇为 **rule 骨架**（未调用大模型）。缺失数据一律写「未获取到」，不把演示假设当作一手披露。",
        "",
        f"> {meta.get('note') or '按 skill 工作流输出结构化备忘录。'}",
        "",
        "### 行情快照",
        "",
        _md_table(
            ["项目", "数值", "来源/时间"],
            [
                ["现价", f"**{price} {currency}**（{chg}）", f"{src}，{qtime}"],
                ["PE", pe, f"{src}，{qtime}"],
                ["Skill 状态", "已安装" if skill_dir_ok(meta) else "缺失", meta["id"]],
            ],
        ),
        "",
    ]

    sections: list[str] = []
    for i, title in enumerate(meta["outline"], 1):
        sections += [
            f"## {i}. {title}",
            "",
            f"**本章要点：** {title} 需一手披露 / 卖方一致预期 / 公司 IR 交叉验证。",
            "",
            "当前 rule 模式未联网抓取申报与电话会，以下为待填框架：",
            "",
            _md_table(
                ["字段", "状态", "备注"],
                [
                    ["关键数字", "未获取到", "待一手来源"],
                    ["我的判断", "观望", "数据不足不得给买卖动作"],
                    ["证伪条件", "未获取到", "正式稿必须可证伪"],
                ],
            ),
            "",
        ]

    if context:
        sections += ["## 背景摘录", "", context[:1500], ""]

    tail = [
        "## 来源与时间戳",
        "",
        f"- 实时行情：{src} · {qtime}",
        f"- 研究模板：{meta['id']}",
        f"- 栏目版本 `{VERSION}`",
        "",
        "---",
        "",
        "> ⚠️ **免责声明**：本栏目由 AI 流程生成，**不构成投资建议**。"
        "最终决策由读者自行承担。",
        "",
    ]
    return "\n".join(head + sections + tail)


def run_skill(
    raw_code: str,
    *,
    skill: str,
    ai_provider: str | None = None,
    channel: str = "console",
    dry_run: bool = True,
    theme: str = "dos",
    timeout: int = 90,
) -> dict:
    """行情 → skill prompt / rule 骨架 → 可选推送。"""
    import hk_quote
    import pushplus_deepseek as pp
    import stock_report as sr

    tmpl = normalize_skill(skill)
    if tmpl == "equity" or tmpl in ("equity_research", "deep_research"):
        import equity_research_column as erc
        return erc.generate_column(
            raw_code, mode="full", ai_provider=ai_provider, channel=channel,
            dry_run=dry_run, theme=theme, timeout=timeout)

    if tmpl not in SKILL_TEMPLATES:
        raise ValueError(f"未知 skill：{skill}（可选：{', '.join(SKILL_TEMPLATES)}）")
    meta = SKILL_TEMPLATES[tmpl]
    if not skill_dir_ok(meta):
        raise RuntimeError(f"skill 未安装：{meta['dir']}/SKILL.md")

    raw_code = (raw_code or "").strip()
    if not raw_code:
        raise ValueError("股票代码不能为空")

    market, code, _ = hk_quote.detect_market(raw_code)
    label = sr.MARKET_LABELS.get(market, market)
    ticker = sr.MARKET_TICKER.get(market, "")

    quote = hk_quote.fetch_quote(raw_code)
    name = (quote or {}).get("name") or ""
    topic = f"{ticker}{code}" + (f" {name}" if name else "")

    if quote:
        context = sr.quote_to_context(market, code, quote)
        market_md = sr.quote_to_md(market, code, quote)
        quote_error = None
    else:
        context = (f"标的：{topic}（{label}）。\n"
                   f"注意：实时行情暂不可用，凡涉及具体点位请标注未获取到。")
        market_md = "> ⚠️ 实时行情暂不可用，价格相关结论请谨慎。"
        quote_error = "行情数据源失败"

    provider = sr.resolve_ai_provider(ai_provider)
    messages = build_messages(tmpl, topic, context)

    if provider == "rule":
        report_md = gen_rule_skill(tmpl, topic, context=context, quote=quote, code=code)
        gen_note = f"rule 骨架 · skill `{meta['id']}`（未调用大模型）"
    else:
        url, model, key, ai_label, keyless = pp.ai_credentials(provider)
        if not key and not keyless:
            report_md = gen_rule_skill(tmpl, topic, context=context, quote=quote, code=code)
            gen_note = (f"缺少 AI Key（{provider}），已降级 rule · skill `{meta['id']}`"
                        "；可配置 INFERENCE_API_KEY 接入统一推理 API")
            provider = "rule"
        else:
            report_md = pp.chat_completion(
                url, key, model, messages, meta["max_tokens"], timeout)
            gen_note = f"{ai_label} + skill `{meta['id']}`"
            if pp.ai_deai_polish_enabled():
                report_md, polish_note = pp.ai_deai_polish_md(
                    report_md, provider=provider, template=tmpl,
                    timeout=timeout)
                if polish_note:
                    gen_note = f"{gen_note}；{polish_note}"

    extras = []
    if market_md:
        extras.append(market_md)
    extras.append(
        "\n---\n"
        f"### 📎 Skill 元数据\n\n"
        f"- skill：`{meta['id']}`（{meta['title']}）\n"
        f"- 模板：`{tmpl}`\n"
        f"- 生成说明：{gen_note}\n"
    )
    body = report_md.rstrip() + "\n\n" + "\n\n".join(extras)
    content = pp.add_branding(body)

    now = datetime.now(CST).strftime("%m-%d %H:%M")
    title = f"{pp.BRAND_TITLE}·{topic}·{meta['title']}（{now}）"

    targets = pp.channel_targets(channel)
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
                    push_results[ch] = pp.push_pushplus(title, content, timeout, theme=theme)
                else:
                    push_results[ch] = pp.PUSH_FUNCS[ch](title, content, timeout)
            except pp.PushError as e:
                push_results[ch] = f"失败：{e}"

    return {
        "ok": True,
        "skill": meta["id"],
        "template": tmpl,
        "skill_title": meta["title"],
        "market": market,
        "market_label": label,
        "code": code,
        "name": name,
        "quote": quote,
        "quote_error": quote_error,
        "provider": provider,
        "gen_note": gen_note,
        "title": title,
        "report_md": content,
        # 与 PushPlus 实际详情页使用同一完整外壳，仪表盘预览所见即所得。
        "report_html": pp.themed_html(title, content, theme_name=theme),
        "theme": theme,
        "dry_run": dry_run,
        "push": push_results,
        "version": VERSION,
    }


def selftest() -> int:
    fails = 0

    def check(name: str, cond: bool) -> None:
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("① 目录与 catalog")
    check("catalog.json", CATALOG_PATH.is_file())
    check("NOTICE.md", (SKILLS_DIR / "NOTICE.md").is_file())
    check("equity-research 可用", (ROOT / "equity_research" / "SKILL.md").is_file())
    items = list_skills()
    check("至少 10 个 skill", len(items) >= 10)
    check("全部已安装", all(s["installed"] for s in items))

    print("② Anthropic 9 技能 SKILL.md")
    for tmpl, meta in SKILL_TEMPLATES.items():
        check(f"{tmpl} SKILL.md", skill_dir_ok(meta))
        raw = (Path(meta["dir"]) / "SKILL.md").read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(raw)
        check(f"{tmpl} frontmatter name", fm.get("name") == meta["id"])

    print("③ 别名与 prompt")
    check("别名 morning-note", normalize_skill("morning-note") == "morning_note")
    check("别名 initiating-coverage", normalize_skill("initiating-coverage") == "initiate")
    msgs = build_messages("morning_note", "HK09988 阿里巴巴", "现价 80 HKD")
    check("system+user", len(msgs) == 2 and "Morning Note" in msgs[0]["content"])
    check("user 含结构", "Top Call" in msgs[1]["content"])

    print("④ rule 骨架")
    md = gen_rule_skill(
        "morning_note", "HK09988 阿里巴巴",
        context="现价演示",
        quote={"price": 80.0, "currency": "HKD", "name": "阿里巴巴",
               "source_label": "演示", "change_pct": 1.2},
        code="09988",
    )
    check("含标题", "晨会纪要" in md)
    check("含免责", "不构成" in md and "投资建议" in md)
    check("含 skill id", "morning-note" in md)

    print("⑤ teaser")
    t = catalog_teaser("09988")
    check("teaser ok", t["ok"] and t["installed"] >= 10)

    print(f"\n{'✅ 自检全部通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="投研 Skills Hub")
    p.add_argument("code", nargs="?", default="")
    p.add_argument("--skill", default="morning_note",
                   help="skill 模板名或 id（如 morning_note / initiate）")
    p.add_argument("--ai-provider", default="auto", dest="ai_provider")
    p.add_argument("--channel", default="console")
    p.add_argument("--theme", default="dos",
                   choices=["dos", "guizang", "game", "klein", "pixel", "monitor", "noc", "default"],
                   help="PushPlus 视觉主题（默认 dos：DOS CRT 磷光绿复古终端）")
    p.add_argument("--push", action="store_true")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--json", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--selftest", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import sys
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.selftest:
        return selftest()
    if args.list:
        print(json.dumps(catalog_teaser(), ensure_ascii=False, indent=2))
        return 0
    if not args.code:
        print("用法: python skills_hub.py <代码> --skill <morning_note|initiate|...>",
              file=sys.stderr)
        return 1
    try:
        result = run_skill(
            args.code, skill=args.skill, ai_provider=args.ai_provider,
            channel=args.channel, dry_run=not args.push, theme=args.theme,
            timeout=args.timeout)
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}", file=sys.stderr)
        return 1
    if args.json:
        slim = {k: v for k, v in result.items() if k != "quote"}
        print(json.dumps(slim, ensure_ascii=False, indent=2, default=str))
        return 0
    print("# " + result["title"])
    print(result["report_md"])
    print("\n===== 推送结果 =====")
    for ch, r in (result.get("push") or {}).items():
        print(f"  {ch}: {r}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
