#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_report.py — 输入港股 / A 股代码 → 实时行情 + AI 研报 + 多通道推送

复用既有模块，不重复造轮子：
  hk_quote            —— 实时行情（港股 + A 股，腾讯财经 / 东方财富，免 Key）
  pushplus_deepseek   —— AI 分析（DeepSeek / OpenAI / rule 规则模板）+ 多通道推送
                        （PushPlus / 企业微信 / Server酱 / 控制台）

支持代码写法（自动识别市场）：
  港股  09988 / 9988.HK / hk00700
  沪A  600519 / 600519.SH / sh600519
  深A  000001 / 000001.SZ / sz000001

用法：
  python stock_report.py 600519                        # 沪A 贵州茅台（打印研报，不推送）
  python stock_report.py sz000001 --template analysis  # 深A 平安银行
  python stock_report.py 09988 --ai-provider deepseek --channel pushplus --push
  python stock_report.py 600519 --ai-provider auto --channel all --push
  python stock_report.py --selftest                    # 离线自检（不联网）
  python stock_report.py --check-only                  # 只检查 Secrets

Secrets（keyless 行情无需配置）：
  DEEPSEEK_API_KEY（AI=deepseek）/ OPENAI_API_KEY（AI=openai）
  PUSHPLUS_TOKEN / WECOM_KEY / SERVERCHAN_SENDKEY（对应推送通道）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import hk_quote
import pushplus_deepseek as pp

# 市场中文名映射（展示用）
MARKET_LABELS = {"hk": "港股", "sh": "沪A", "sz": "深A", "us": "美股"}
MARKET_FULL = {"hk": "香港交易所", "sh": "上海证券交易所",
               "sz": "深圳证券交易所", "us": "美国市场（NASDAQ/NYSE/AMEX）"}
MARKET_TICKER = {"hk": "HK", "sh": "SH", "sz": "SZ", "us": ""}

VERSION = "1.2.0-us-2026-08-14"   # 境外（美股）4 源交叉验证 + 输出页排版整理

# CLI / Actions 可选值。auto（及空串）= 有 DEEPSEEK_API_KEY 走 deepseek，否则 rule。
AI_PROVIDERS = ("", "auto", "deepseek", "openai", "rule")


def resolve_ai_provider(ai_provider: str | None) -> str:
    """把 auto / 空串解析成实际提供方，其它值原样返回。"""
    p = (ai_provider or "").strip().lower()
    if p in ("", "auto"):
        # 统一推理 API（INFERENCE_*）或旧 DEEPSEEK_API_KEY 任一可用即可
        return pp.ai_auto_provider()
    return p


# ================================================================ 工具

def _fmt_vol(shares) -> str:
    if shares is None:
        return "—"
    if shares >= 1e8:
        return f"{shares / 1e8:.2f}亿"
    if shares >= 1e4:
        return f"{shares / 1e4:.2f}万"
    return f"{shares:.0f}"


def _fmt_amount(v) -> str:
    if v is None:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.0f}"


def quote_to_context(market: str, code: str, q: dict) -> str:
    """把标准化行情 dict 转成注入 AI 的文本上下文。"""
    label = MARKET_LABELS.get(market, market)
    cur = q.get("currency") or ("USD" if market == "us"
                                else "HKD" if market == "hk" else "CNY")
    lines = [
        f"标的：{code} {q.get('name') or ''}（{label} · {MARKET_FULL.get(market, '')}）",
        f"最新价 {q['price']:.3f} {cur}"
        + (f"（{q.get('change_pct'):+.2f}%，涨跌 {q.get('change'):+.3f}）"
           if q.get('change_pct') is not None else ""),
    ]
    if q.get("open") or q.get("prev_close"):
        lines.append(f"今开 {q.get('open') or '—'} / 昨收 {q.get('prev_close') or '—'}")
    if q.get("high") or q.get("low"):
        lines.append(f"最高 {q.get('high') or '—'} / 最低 {q.get('low') or '—'}")
    lines.append(
        f"市盈率(PE) {q.get('pe') or '—'} · 市净率(PB) {q.get('pb') or '—'} · "
        f"换手率 {q.get('turnover_rate') or '—'}% · 振幅 {q.get('amplitude') or '—'}%")
    if q.get("market_cap") or q.get("float_cap"):
        lines.append(f"总市值约 {_fmt_amount(q.get('market_cap'))} {cur} · "
                     f"流通市值约 {_fmt_amount(q.get('float_cap'))} {cur}")
    if q.get("high_52w") or q.get("low_52w"):
        lines.append(f"52周区间 {q.get('high_52w') or '—'} / {q.get('low_52w') or '—'}")
    lines.append(f"成交量 {_fmt_vol(q.get('volume'))} 股 · 成交额约 {_fmt_amount(q.get('amount'))} {cur}")
    from source_names import generalize_source
    src_disp = generalize_source(q.get("source_label") or q.get("source"))
    lines.append(f"数据源：{src_disp}"
                 f" · 行情时间 {q.get('time') or q.get('fetched_at') or '—'}")
    return "\n".join(lines)


def quote_to_md(market: str, code: str, q: dict) -> str:
    """把标准化行情 dict 转成研报正文顶部的行情核验块（Markdown）。"""
    from source_names import generalize_source
    src_disp = generalize_source(q.get("source_label") or q.get("source"))
    label = MARKET_LABELS.get(market, market)
    cur = q.get("currency") or ("USD" if market == "us"
                                else "HKD" if market == "hk" else "CNY")
    chg = (f"{q.get('change_pct'):+.2f}%" if q.get('change_pct') is not None else "—")
    lines = [
        "---",
        f"📊 **实时行情（{MARKET_TICKER.get(market, market)} {code} · {label}）**",
        "",
        f"| 项目 | 数值 | 项目 | 数值 |",
        f"|---|---|---|---|",
        f"| 最新价 | **{q['price']:.3f} {cur}** | 涨跌幅 | {chg} |",
        f"| 今开 / 昨收 | {q.get('open') or '—'} / {q.get('prev_close') or '—'} | 最高 / 最低 | {q.get('high') or '—'} / {q.get('low') or '—'} |",
        f"| PE / PB | {q.get('pe') or '—'} / {q.get('pb') or '—'} | 换手 / 振幅 | {q.get('turnover_rate') or '—'}% / {q.get('amplitude') or '—'}% |",
        f"| 总市值 | {_fmt_amount(q.get('market_cap'))} {cur} | 流通市值 | {_fmt_amount(q.get('float_cap'))} {cur} |",
        f"| 成交量 | {_fmt_vol(q.get('volume'))} 股 | 成交额 | {_fmt_amount(q.get('amount'))} {cur} |",
        "",
        f"> 数据源：{src_disp} · "
        f"行情时间 {q.get('time') or q.get('fetched_at') or '—'}",
    ]
    return "\n".join(lines)


def quote_to_pp_quotes(quote: dict | None) -> list:
    """把 hk_quote 标准化行情转成 pushplus_deepseek.Quote 列表（供字符图/新鲜度复用）。"""
    from source_names import generalize_source
    if not quote or quote.get("price") is None:
        return []
    return [pp.Quote(
        source=generalize_source(quote.get("source_label") or quote.get("source") or "行情"),
        ok=True,
        name=quote.get("name") or "",
        price=quote.get("price"),
        prev_close=quote.get("prev_close"),
        change_pct=quote.get("change_pct"),
        time_str=str(quote.get("time") or quote.get("fetched_at") or ""),
    )]


def fetch_verified_quote(market: str, code: str, raw_code: str):
    """港/A 走 hk_quote 主备链路；美股走 us_quote 四源 + 交叉验证。

    返回 (quote, cross_pack)：cross_pack 仅美股存在，可直接传给
    us_quote.us_cross_md / us_context_line。
    """
    if market == "us":
        try:
            import us_quote
            return us_quote.fetch_us_verified(code)
        except ImportError:
            return hk_quote.fetch_quote(raw_code), None
    return hk_quote.fetch_quote(raw_code), None


def quote_with_context(market: str, code: str, q: dict,
                       cross_pack: dict | None = None) -> tuple[str, str]:
    """(AI 上下文, 研报行情块)。境外标的在两者尾部附四源交叉验证。"""
    context, md = quote_to_context(market, code, q), quote_to_md(market, code, q)
    if cross_pack:
        try:
            import us_quote
            context = context.rstrip() + "\n" + us_quote.us_context_line(cross_pack)
            md = md.rstrip() + "\n\n" + us_quote.us_cross_md(cross_pack)
        except Exception:  # noqa: BLE001 —— 交叉验证失败只丢附加段
            pass
    return context, md


def tidy_report_md(md: str) -> str:
    """研报输出页排版整理：合并相邻 "---" 分隔线、压缩 3+ 连续换行、去首尾空块。"""
    blocks = [b.strip() for b in re.split(r"\n{2,}", md) if b.strip()]
    out: list[str] = []
    prev_hr = False
    for b in blocks:
        hr = (b == "---")
        if hr and prev_hr:
            continue
        prev_hr = hr
        out.append(b)
    return "\n\n".join(out)


def collect_latest_pack(topic: str, raw_code: str, hours: int,
                        timeout: int = 12) -> object | None:
    """采集量价舆情 + 十七平台扫描。单源失败只记缺口，绝不中断研报。"""
    try:
        return pp.collect_sentiment(topic, raw_code, hours, timeout)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 最新因子采集失败（不影响研报）：{e}", flush=True)
        return None


def wrap_with_latest_features(
    body_md: str, *,
    raw_code: str,
    template: str,
    topic: str,
    hours: int = 48,
    no_chart: bool = False,
    quote: dict | None = None,
    sent_pack=None,
    persist_state: bool = False,
) -> tuple[str, dict]:
    """把「最新功能」装进推送正文：新鲜度看板 + 字符模拟图 + 十七平台附录。"""
    quotes = quote_to_pp_quotes(quote)
    qb = pp._quote_brief(quotes)
    item_keys = pp.gather_item_keys(sent_pack)
    extra_points: dict = {}
    anchor_parts: list[str] = []
    if sent_pack is not None:
        anchor = sent_pack.agg.get("综合多头概率锚点")
        extra_points["anchor"] = anchor
        extra_points["mom"] = (sent_pack.momentum or {}).get("score")
        anchor_parts.append(
            f"综合多头概率锚点 ≈ {anchor if anchor is not None else '—'}%")
        if (sent_pack.momentum or {}).get("ok"):
            anchor_parts.append(
                f"量价动量 {sent_pack.momentum['score']:+.2f}")
        scan = getattr(sent_pack, "scan", None)
        if scan is not None:
            sa = scan.agg
            extra_points["scan_hit"] = sa.get("platforms_hit")
            anchor_parts.append(
                f"十七平台命中 {sa.get('platforms_hit', 0)}"
                f"/{sa.get('platforms_total', 17)}")
    anchor_line = (" · ".join(anchor_parts)
                   or "无本地预聚合数据（本模板未采集窗口样本）")

    st = pp.load_state(pp.state_path())
    skey = pp.run_state_key(template, topic, raw_code)
    prev = st["runs"].get(skey) or {}
    new_items = pp.diff_item_keys(item_keys, prev.get("item_keys", {})) if prev else {}
    new_key_set = {k for keys in new_items.values() for k in keys}
    fp = pp.content_fingerprint(template, topic, body_md, qb, extra_points, item_keys)
    dup = bool(prev) and prev.get("fingerprint") == fp
    freshness_md = pp.render_freshness_md(
        now_cst=pp.datetime.now(pp.CST).strftime("%m-%d %H:%M"),
        template=template, hours=hours, fingerprint=fp,
        dup=dup if prev else None,
        quote_line=pp._quote_freshness_line(qb, prev.get("price")),
        new_items=new_items, first_run=not prev,
        anchor_line=anchor_line, state_enabled=True)

    appendix_md = ""
    if sent_pack is not None:
        try:
            appendix_md = pp.render_sentiment_appendix(sent_pack, new_keys=new_key_set)
        except Exception:  # noqa: BLE001
            appendix_md = ""

    chart_md = pp.make_chart_block(raw_code, quotes, no_chart)
    # 输出页排版：字符走势图 → 行情快照+正文 → 十七平台附录 → 数据新鲜度·本次运行指纹（固定放最后）；
    # 分隔线与空行统一由 tidy_report_md 去重压缩，页面更简洁
    pieces = [chart_md, body_md.rstrip()]
    if appendix_md:
        pieces.append(appendix_md)
    if freshness_md:
        pieces.append(freshness_md)
    content = pp.add_branding(tidy_report_md("\n\n".join(p for p in pieces if p)))

    if persist_state:
        st["version"] = pp.STATE_VERSION
        st["runs"][skey] = {
            "fingerprint": fp,
            "ts": pp.datetime.now(pp.CST).isoformat(timespec="seconds"),
            "price": qb.get("price"),
            "anchor": extra_points.get("anchor"),
            "item_keys": {s: ks[:pp.STATE_MAX_KEYS_PER_SRC]
                          for s, ks in item_keys.items()},
        }
        if len(st["runs"]) > pp.STATE_MAX_RUN_KEYS:
            ordered = sorted(st["runs"], key=lambda k: st["runs"][k].get("ts", ""))
            for old_key in ordered[:-pp.STATE_MAX_RUN_KEYS]:
                del st["runs"][old_key]
        pp.save_state(pp.state_path(), st)

    return content, {
        "fingerprint": fp,
        "dup": dup,
        "has_chart": bool(chart_md),
        "has_scan": bool(sent_pack and getattr(sent_pack, "scan", None)),
        "hours": hours,
    }


# ================================================================ 核心：行情 → AI 研报 → 推送

def run_report(raw_code: str, *, channel: str = "console",
               ai_provider: str | None = None, template: str = "analysis",
               dry_run: bool = True, theme: str = "dos",
               push_timeout: int = 30, no_chart: bool = False,
               risk: str = "mid", mode: str = "full",
               industry: str | None = None, hours: int = 48,
               collect_news: bool = True,
               ai_compress: bool | None = None) -> dict:
    """输入股票代码，实时取行情 → AI/rule 生成研报 → 推送。返回结构化结果。

    一律附带最新功能：🧭 数据新鲜度看板、📊 字符模拟图、🛰 十七平台扫描。
    template=equity 时走独立栏目 equity_research_column（equity-research-skill
    九章深度 + dcf.py 可复算估值），同样注入上述最新能力。
    ai_compress：None=跟随默认（开启，AI_COMPRESS=0 可关）；True/False 显式
    指定是否在拿到正文后按章节逐节调用 AI 简化 + 压缩。
    """
    raw_code = (raw_code or "").strip()
    if not raw_code:
        raise ValueError("股票代码不能为空")
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = 48
    if hours not in (24, 48, 72, 156):
        hours = 48

    # —— 官方 / 社区 Skills Hub（Anthropic 9 技能等）——
    try:
        import skills_hub as sh
        if sh.is_skill_template(template):
            return sh.run_skill(
                raw_code, skill=template, ai_provider=ai_provider,
                channel=channel, dry_run=dry_run, theme=theme,
                timeout=max(push_timeout, 60))
    except ImportError:
        pass

    # —— 机构级个股投研独立栏目（equity-research-skill）——
    if template in ("equity", "equity_research", "deep_research"):
        try:
            import equity_research_column as erc
        except ImportError as e:
            raise RuntimeError(
                f"equity 栏目模块不可用：{e}（需 equity_research_column.py "
                f"+ equity_research/ skill 资源）") from e
        eq_mode = mode if mode in ("full", "earnings") else "full"
        # earnings 模板名也可映射到财报模式
        if template == "equity" and mode == "earnings":
            eq_mode = "earnings"
        return erc.generate_column(
            raw_code, mode=eq_mode, ai_provider=ai_provider, channel=channel,
            dry_run=dry_run, theme=theme, industry=industry,
            timeout=max(push_timeout, 60), run_check=True,
            hours=hours, no_chart=no_chart, collect_news=collect_news)

    market, code, _em_secid = hk_quote.detect_market(raw_code)
    label = MARKET_LABELS.get(market, market)
    ticker = MARKET_TICKER.get(market, "")

    # ---- ① 实时行情（失败不抛，记录数据缺口）----
    #     境外（美股）：腾讯美股/东财美股/Yahoo/Stooq 四源采集 + 交叉验证
    quote, cross_pack = fetch_verified_quote(market, code, raw_code)
    name = (quote or {}).get("name") or ""
    topic = f"{ticker}{code}" + (f" {name}" if name else "")

    if quote:
        context, market_md = quote_with_context(market, code, quote, cross_pack)
        quote_error = None
    else:
        src_hint = ("境外四路行情源"
                    if market == "us" else "双路实时行情源")
        context = (f"标的：{topic}（{label}）。\n"
                   f"注意：实时行情暂不可用（网络受限或数据源失败），"
                   f"凡涉及具体点位/估值的地方请标注 *（推断）。")
        market_md = (f"> ⚠️ 实时行情暂不可用（数据源：{src_hint}均失败），"
                     f"以下研报基于模型既有知识推断，价格相关结论请谨慎参考。")
        quote_error = "行情数据源失败，研报基于模型知识推断"

    # ---- ①b 最新功能：量价舆情动量 + 十七平台扫描 ----
    sent_pack = None
    if collect_news and template in ("analysis", "sentiment", "scan", "feedscan", "newsnow"):
        sent_pack = collect_latest_pack(
            topic, raw_code, hours, timeout=min(push_timeout, 12))
        if sent_pack is not None:
            try:
                context = (pp.sentiment_context(sent_pack) + "\n\n" + context)
            except Exception:  # noqa: BLE001
                pass

    # ---- ② AI / rule 生成研报 ----
    provider = resolve_ai_provider(ai_provider)
    messages = pp.build_messages(template, topic, context, risk)
    if provider == "rule":
        report_md = pp.gen_by_rule(topic, template, sent_pack=sent_pack)
        gen_note = "rule 规则模板（未调用大模型）"
    else:
        url, model, key, ai_label, keyless = pp.ai_credentials(provider)
        if not key and not keyless:
            report_md = pp.gen_by_rule(topic, template, sent_pack=None)
            gen_note = (f"缺少 AI Key（{provider}），已降级为 rule 规则模板"
                        "；可配置 INFERENCE_API_KEY 接入统一推理 API")
        else:
            report_md = pp.chat_completion(
                url, key, model, messages,
                pp.TEMPLATE_MAX_TOKENS.get(template, 3000), push_timeout)
            gen_note = ai_label

    # ---- ②b AI 分节压缩：拿到正文后，每部分（章节）分别调用 AI 简化 + 压缩 ----
    #     失败自动回退该节原文；无 Key / 总开关关闭时跳过（见 pp.ai_compress_md）。
    if ai_compress is None:
        ai_compress = pp.ai_compress_enabled()
    if ai_compress:
        report_md, comp_note = pp.ai_compress_md(
            report_md, provider=provider, template=template,
            timeout=push_timeout)
        if comp_note:
            gen_note = f"{gen_note}；{comp_note}"

    # ---- ③ 组装研报正文（行情快照 → 研报正文 → 新鲜度看板/字符图/附录由 wrap 包装）----
    targets = pp.channel_targets(channel)
    # 真实推送但所有目标通道均缺少推送 Secret 时，只生成并完整打印正文，
    # 不写入“已推送”状态，避免下次新鲜度对比误以为已送达。
    should_persist_state = (not dry_run and pp.has_deliverable_channel(channel))
    body = report_md.rstrip()
    if market_md:
        body = market_md.rstrip() + "\n\n" + body  # 行情块顶置：先价后文，界面更顺
    content, latest_meta = wrap_with_latest_features(
        body, raw_code=raw_code, template=template, topic=topic,
        hours=hours, no_chart=no_chart, quote=quote, sent_pack=sent_pack,
        persist_state=should_persist_state)

    now = pp.datetime.now(pp.CST).strftime("%m-%d %H:%M")
    title = (f"{pp.BRAND_TITLE}·{topic}"
             f"·{pp.TEMPLATE_TITLES.get(template, template)}（{now}）")

    # ---- ④ 推送 ----
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
                    push_results[ch] = pp.push_pushplus(title, content, push_timeout, theme=theme)
                else:
                    push_results[ch] = pp.PUSH_FUNCS[ch](title, content, push_timeout)
            except pp.PushError as e:
                push_results[ch] = f"失败：{e}"

    return {
        "market": market,
        "market_label": label,
        "code": code,
        "name": name,
        "quote": quote,
        "quote_error": quote_error,
        "provider": provider,
        "gen_note": gen_note,
        "template": template,
        "title": title,
        "report_md": content,
        # 与 PushPlus 实际详情页使用同一完整外壳，仪表盘预览不再只显示裸正文。
        "report_html": pp.themed_html(title, content, theme_name=theme),
        "theme": theme,
        "dry_run": dry_run,
        "push": push_results,
        "hours": hours,
        "latest": latest_meta,
    }


# ================================================================ 离线自检

# 自检期间需要屏蔽的 AI Key —— 自检是「离线自检」，不得因为 CI 环境里配了
# DEEPSEEK_API_KEY / OPENAI_API_KEY 就真的联网调用大模型（既慢又会让
# 「无 Key 时优雅跳过」这类断言失败）。
_AI_KEY_ENVS = (
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "INFERENCE_PROVIDER", "INFERENCE_BASE_URL",
    "INFERENCE_API_KEY", "INFERENCE_MODEL",
    "INFERENCE_TIMEOUT", "INFERENCE_MAX_RETRIES", "INFERENCE_GATEWAY_TOKEN",
    "HF_TOKEN", "HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN",
    "HF_ENDPOINT_URL", "HF_MODEL",
    "SILICONFLOW_API_KEY", "MOONSHOT_API_KEY", "ZHIPU_API_KEY", "DASHSCOPE_API_KEY",
    "SENTIMENT_ENGINE",
)


class _NoAIKeys:
    """上下文管理器：临时清空 AI Key，退出时原样恢复。"""

    def __init__(self, *names: str):
        self.names = names or _AI_KEY_ENVS
        self._old: dict[str, str] = {}

    def __enter__(self):
        for n in self.names:
            if n in os.environ:
                self._old[n] = os.environ.pop(n)
        return self

    def __exit__(self, *exc):
        for n, v in self._old.items():
            os.environ[n] = v
        self._old.clear()
        return False


def selftest() -> int:
    """离线自检入口：全程屏蔽 AI Key，保证结果与环境无关、不联网调用大模型。"""
    with _NoAIKeys():
        return _selftest_impl()


def _selftest_impl() -> int:
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            fails += 1

    print("① 市场识别 detect_market")
    cases = [
        ("09988", "hk", "09988"),
        ("9988.HK", "hk", "09988"),
        ("hk00700", "hk", "00700"),
        ("600519", "sh", "600519"),
        ("600519.SH", "sh", "600519"),
        ("sh600519", "sh", "600519"),
        ("688981", "sh", "688981"),
        ("000001", "sz", "000001"),
        ("000001.SZ", "sz", "000001"),
        ("sz000001", "sz", "000001"),
        ("300750", "sz", "300750"),
    ]
    for raw, want_m, want_c in cases:
        m, c, _ = hk_quote.detect_market(raw)
        check(f"{raw!r} → {m}/{c}", m == want_m and c == want_c)

    print("② 行情上下文（离线 fixture）")
    q = hk_quote._parse_tencent(hk_quote.FIXTURES["tencent_sh600519"], "600519", "sh")
    ctx = quote_to_context("sh", "600519", q)
    check("沪A 上下文含价格/PE/币种", ("1720.000" in ctx and "25.6" in ctx and "CNY" in ctx))
    md = quote_to_md("sh", "600519", q)
    check("沪A 行情块含价格与泛化源", ("1720.000" in md and "实时行情主源" in md))
    q2 = hk_quote._parse_tencent(hk_quote.FIXTURES["tencent_00700"], "00700", "hk")
    check("港股上下文币种 HKD", "HKD" in quote_to_context("hk", "00700", q2))

    print("③ 研报组装（rule 离线）")
    r = run_report("600519", channel="console", ai_provider="rule", dry_run=True,
                   collect_news=False, no_chart=True)
    check("返回结构完整", r["market"] == "sh" and r["code"] == "600519"
          and r["provider"] == "rule" and "report_md" in r and "report_html" in r)
    check("品牌头尾齐全", pp.BRAND_TITLE in r["report_md"]
          and pp.BRAND_DISCLAIMER in r["report_md"]
          and r["report_md"].rstrip().endswith(
              f"**{pp.BRAND_AUTHOR} · {pp.BRAND_DISCLAIMER}**"))
    check("研报含行情核验块", "实时行情" in r["report_md"])
    check("研报含最新功能新鲜度看板", "数据新鲜度" in r["report_md"] and "内容指纹" in r["report_md"])
    check("HTML 渲染非空", "<div" in r["report_html"] or "<pre" in r["report_html"])
    check("console dry-run 标记", "dry-run" in r["push"]["console"])

    r2 = run_report("sz000001", channel="console", ai_provider="rule", dry_run=True,
                    collect_news=False, no_chart=True)
    # 注意：不要断言 name == "" / quote_error is not None ——
    # 那只在「拉不到行情」的离线沙箱成立；CI（如 GitHub Actions）有公网时
    # sz000001 会取到真实行情（name="平安银行"、quote_error=None），断言会误报失败。
    # 这里只校验与网络无关的稳定不变量：市场识别、代码归一化、标题前缀、正文组装。
    check("深A 识别与研报", r2["market"] == "sz" and r2["code"] == "000001"
          and "SZ000001" in r2["title"]
          and bool(r2.get("report_md"))
          # 有行情则 name 非空且出现在标题里；无行情则 name 为空且给出降级说明
          and ((r2["quote_error"] is None and r2["name"]
                and r2["name"] in r2["title"])
               or (r2["quote_error"] is not None and r2["name"] == "")))

    print("④ 空代码与非法输入")
    try:
        run_report("")
        check("空代码抛异常", False)
    except ValueError:
        check("空代码抛异常", True)

    print("⑤ AI 消息构造（离线）")
    msgs = pp.build_messages("analysis", "SH600519 贵州茅台", "示例背景", "mid")
    check("analysis 消息可构造", bool(msgs[1]["content"]) and "因子" in msgs[1]["content"])

    print("⑤b 机构级个股投研栏目（equity template）")
    check("equity 在 TEMPLATES", "equity" in pp.TEMPLATES)
    check("equity 标题", pp.TEMPLATE_TITLES.get("equity") == "机构级个股投研")
    try:
        import equity_research_column as erc
        check("equity_research_column 可导入", True)
        check("skill 可用", erc.skill_available())
        r_eq = run_report("09988", channel="console", ai_provider="rule",
                          template="equity", dry_run=True, theme="game",
                          collect_news=False, no_chart=True)
        check("equity 返回 ok/结构", r_eq.get("template") == "equity"
              or r_eq.get("column", {}).get("column_id") == "equity_research")
        check("equity 九章正文", "个股投资研究报告" in (r_eq.get("report_md") or "")
              and ("一页速览" in (r_eq.get("report_md") or "")
                   or "决策三分法" in (r_eq.get("report_md") or "")))
        check("equity 含估值/免责",
              "估值" in (r_eq.get("report_md") or "")
              and "投资建议" in (r_eq.get("report_md") or ""))
    except Exception as e:  # noqa: BLE001
        check(f"equity 栏目异常: {e}", False)

    print("⑤c 最新功能包装（新鲜度看板 + 字符图 + 十七平台附录）")
    wrapped, meta = wrap_with_latest_features(
        "**演示正文**\n\n- 综合判断：中性",
        raw_code="09988", template="analysis", topic="HK09988 阿里巴巴",
        hours=48, no_chart=True, quote={
            "price": 16.8, "name": "阿里巴巴", "source_label": "演示",
            "prev_close": 16.6, "change_pct": 1.2, "time": "2026-08-13 15:00",
        }, sent_pack=None, persist_state=False)
    check("包装含新鲜度看板", "数据新鲜度" in wrapped and "内容指纹" in wrapped)
    check("新鲜度看板固定在正文最后", wrapped.index("演示正文") < wrapped.index("数据新鲜度"))
    check("包装含品牌头尾", pp.BRAND_TITLE in wrapped
          and wrapped.rstrip().endswith(
              f"**{pp.BRAND_AUTHOR} · {pp.BRAND_DISCLAIMER}**"))
    check("包装返回指纹", bool(meta.get("fingerprint")))
    check("no_chart 不强制出图", meta.get("has_chart") is False)
    args_h = parse_args(["09988", "--hours", "156"])
    check("argparse 接受 --hours 156", args_h.hours == 156)

    print("⑤d Skills Hub（Anthropic 官方 9 技能）")
    try:
        import skills_hub as sh
        check("skills_hub 可导入", True)
        t = sh.catalog_teaser("09988")
        check("catalog ≥10 且已安装", t.get("installed", 0) >= 10)
        check("morning_note 是 skill 模板", sh.is_skill_template("morning_note"))
        r_sk = run_report("09988", channel="console", ai_provider="rule",
                          template="morning_note", dry_run=True, theme="game",
                          collect_news=False, no_chart=True)
        check("morning_note 返回结构", r_sk.get("template") == "morning_note"
              and "晨会纪要" in (r_sk.get("report_md") or r_sk.get("skill_title") or ""))
        check("morning_note 含免责", "投资建议" in (r_sk.get("report_md") or ""))
    except Exception as e:  # noqa: BLE001
        check(f"skills hub 异常: {e}", False)

    print("⑥ --ai-provider auto 合法且等价于空串")
    args_auto = parse_args(["600519", "--ai-provider", "auto"])
    check("argparse 接受 auto", args_auto.ai_provider == "auto")
    args_empty = parse_args(["600519"])
    check("默认即为 auto", args_empty.ai_provider == "auto")
    with _NoAIKeys():
        check("auto 无 Key 时解析为 rule",
              resolve_ai_provider("auto") == "rule"
              and resolve_ai_provider("") == resolve_ai_provider("auto")
              and resolve_ai_provider(None) == resolve_ai_provider("auto"))
    os.environ["DEEPSEEK_API_KEY"] = "sk-selftest"
    try:
        check("auto 有 Key 时解析为 deepseek",
              resolve_ai_provider("auto") == "deepseek")
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    r_auto = run_report("600519", channel="console", ai_provider="auto", dry_run=True,
                        collect_news=False, no_chart=True, ai_compress=False)
    check("run_report(auto) 落到具体提供方", r_auto["provider"] in ("rule", "deepseek"))

    print("⑥a 缺少推送 Key 时真实推送优雅跳过")
    old_pushplus = os.environ.get("PUSHPLUS_TOKEN")
    try:
        os.environ.pop("PUSHPLUS_TOKEN", None)
        r_skip = run_report("600519", channel="pushplus", ai_provider="rule",
                            dry_run=False, collect_news=False, no_chart=True,
                            ai_compress=False)
        check("无 PushPlus Key 时跳过且正文完整",
              pp.is_skip_result(r_skip["push"].get("pushplus"))
              and len(r_skip.get("report_md") or "") > 0)
    finally:
        if old_pushplus is not None:
            os.environ["PUSHPLUS_TOKEN"] = old_pushplus

    print("⑥b AI 分节压缩（拿到正文后逐节调用 AI 简化+压缩）")
    args_c = parse_args(["600519", "--ai-compress", "false"])
    check("--ai-compress 参数可解析", args_c.ai_compress == "false")
    check("默认不传=跟随环境开关", parse_args(["600519"]).ai_compress == "")
    old_ac = os.environ.get("AI_COMPRESS")
    try:
        os.environ["AI_COMPRESS"] = "0"
        check("AI_COMPRESS=0 读为关闭", pp.ai_compress_enabled() is False)
        r0 = run_report("600519", channel="console", ai_provider="rule",
                        dry_run=True, collect_news=False, no_chart=True)
        check("关闭时 gen_note 不含压缩信息",
              "压缩" not in (r0.get("gen_note") or ""))
        os.environ.pop("AI_COMPRESS")
        with _NoAIKeys():           # 显式屏蔽 Key：断言与 CI 是否配置 Secret 无关
            r1 = run_report("600519", channel="console", ai_provider="rule",
                            dry_run=True, collect_news=False, no_chart=True)
        check("无 Key 时优雅跳过（正文完整）",
              "跳过分节压缩" in (r1.get("gen_note") or "")
              and len(r1.get("report_md") or "") > 0)
        check("跳过压缩时正文与未压缩版一致（不丢内容）",
              (r1.get("report_md") or "x") == (r0.get("report_md") or "y"))
    finally:
        if old_ac is None:
            os.environ.pop("AI_COMPRESS", None)
        else:
            os.environ["AI_COMPRESS"] = old_ac

    print(f"\n{'✅ 自检全部通过' if fails == 0 else f'❌ {fails} 项失败'}")
    return 1 if fails else 0


def _check_only(channel: str, provider: str) -> int:
    print("🔎 Secrets 检查（只显示是否配置，不打印内容）：")
    provider = resolve_ai_provider(provider)
    names = pp.required_secrets(channel, provider)
    missing = [n for n in names if not pp.env(n)]
    for n in names:
        print(f"  {'✅' if n not in missing else '❌'} {n} {'已配置' if n not in missing else '缺失'}")
    if not names:
        print("  （所选 通道+AI 组合无需 Secret，行情数据免 Key）")
    elif missing:
        print("  ⚠️ 缺失项只会导致对应推送通道被跳过；正文仍会完整生成并打印，检查步骤不再返回失败。")
    return 0


# ================================================================ CLI

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="输入港股/A股代码 → 实时行情 + AI 研报 + 多通道推送",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("code", nargs="?", default="",
                   help="股票代码（港股 09988 / A股 600519 / 000001.SZ 等）")
    p.add_argument("--channel", default="console", choices=["pushplus", "wecom", "serverchan", "console", "all"])
    p.add_argument("--ai-provider", default="auto", choices=list(AI_PROVIDERS),
                   dest="ai_provider",
                   help="AI 提供方（auto=自动：有 DEEPSEEK_API_KEY 用 deepseek，否则 rule）")
    p.add_argument("--template", default="analysis", choices=pp.TEMPLATES,
                   help="分析框架：" + "/".join(pp.TEMPLATES)
                        + "（equity=机构级个股投研独立栏目）")
    p.add_argument("--mode", default="full", choices=["full", "earnings"],
                   help="equity 栏目模式：full=九章深度 / earnings=财报深度")
    p.add_argument("--industry", default="",
                   help="equity 栏目行业附录 slug（默认自动猜测）")
    p.add_argument("--risk", default="mid", choices=pp.RISKS, help="风险偏好（portfolio 模板）")
    p.add_argument("--theme", default="dos",
                   choices=["dos", "guizang", "game", "klein", "pixel", "monitor", "noc", "default"],
                   help="PushPlus 视觉主题（默认 dos：DOS CRT 磷光绿复古终端）")
    p.add_argument("--push", action="store_true", help="真实推送（默认 dry-run 只打印不推送）")
    p.add_argument("--no-chart", action="store_true", dest="no_chart")
    p.add_argument("--ai-compress", default="", dest="ai_compress",
                   help="AI 分节压缩：正文按章节逐节调用 AI 简化+压缩"
                        "（true=开启[默认] / false=关闭；不传则跟随默认，"
                        "环境变量 AI_COMPRESS=0 亦可关闭）")
    p.add_argument("--hours", type=int, default=48,
                   help="量价舆情/十七平台扫描数据窗口（24/48/72/156）")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument("--selftest", action="store_true", help="离线自检")
    p.add_argument("--check-only", action="store_true", dest="check_only", help="只检查 Secrets")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.selftest:
        return selftest()
    if args.check_only:
        return _check_only(args.channel, args.ai_provider)

    if not args.code:
        print("❌ 请提供股票代码，例如：python stock_report.py 600519", file=sys.stderr)
        return 1

    provider = resolve_ai_provider(args.ai_provider)
    raw_ac = (getattr(args, "ai_compress", "") or "").strip().lower()
    ai_compress = (None if raw_ac in ("", "auto")
                   else raw_ac in ("1", "true", "yes", "on"))
    try:
        result = run_report(
            args.code, channel=args.channel, ai_provider=provider,
            template=args.template, dry_run=not args.push, theme=args.theme,
            push_timeout=args.timeout, no_chart=args.no_chart, risk=args.risk,
            mode=getattr(args, "mode", "full"),
            industry=(getattr(args, "industry", None) or None) or None,
            hours=getattr(args, "hours", 48),
            ai_compress=ai_compress)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    print("# " + result["title"])
    print(result["report_md"])
    md = result.get("report_md") or ""
    print(f"\n===== 推送结果 =====")
    print(f"  正文 {len(md)} 字 / {len(md.encode('utf-8'))} 字节"
          f" · dry_run={result.get('dry_run')} · 通道={list(result.get('push') or {})}")
    failed = 0
    skipped = 0
    for ch, r in result["push"].items():
        text = str(r)
        ok = not pp.is_failure_result(text)
        is_skip = pp.is_skip_result(text)
        mark = "⚠️" if is_skip else "✅" if ok else "❌"
        if ch == "console" and not result.get("dry_run"):
            print(f"  ⚠️ {ch}: {text}（console 不会发到微信，请把 channel 改成 pushplus）")
        else:
            print(f"  {mark} {ch}: {text}")
        if is_skip:
            skipped += 1
        if not ok:
            failed += 1
    if result.get("dry_run"):
        print("  ℹ️ 本次是 dry-run，没有真实推送到微信。")
    elif skipped and skipped == len(result.get("push") or {}):
        print("  ℹ️ 所选推送通道均缺少 Secret，已优雅跳过；正文已完整打印到日志，不视为失败。")
    elif skipped:
        print("  ℹ️ 部分通道缺少 Secret 已跳过；其余通道已按上方结果处理。")
    if failed:
        print(f"\n❌ {failed} 个通道推送失败，微信不会收到。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
