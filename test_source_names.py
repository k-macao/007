#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端来源泛化名回归门禁（test_source_names.py）。

覆盖两层契约：
  1. 映射层契约（source_names.py）：
     - 行情源 / 十二源快讯 / 全网 10 热榜 / 十八平台扫描各管线泛化名无冲突；
     - 电报导语前缀（财联社电 / 格隆汇电 / 【金十数据】等）自动剥离且不伤上市公司名；
     - 异常信息自动脱敏 URL / 域名 / 原始平台品牌名 / 密钥变量名；
     - 缓存后缀「（文件缓存）」保留。
  2. 全链路零品牌泄露契约：
     - 港/A/美股行情交叉核验表与 AI 上下文；
     - 十二源快讯 rule 正文、附录、AI 上下文与提示词；
     - 量价舆情动量 rule 正文、附录、AI 上下文与提示词；
     - 十八平台股票扫描 Markdown、AI 上下文与金融情绪复核；
     - 全网 10 热榜 rule 正文、附录与 AI 上下文；
     - 香港券商 / 外资机构周报（保留研究对象机构名，隐藏抓取渠道品牌名）；
     - 数据源校验数据库报告；
     - 个股研报（analysis / equity / morning_note）完整 Markdown + HTML；
     - 全部 25 套模板提示词与 rule 正文；
     - 服务器监控大屏 HTML（SHOW_SOURCE_INFO=False 与 True 双态）。
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

import source_names as sn
from source_names import (
    clean_wire_prefix,
    find_brand_leaks,
    generalize_markdown_sources,
    generalize_source,
    sanitize_error,
)


class TestSourceNamesCore(unittest.TestCase):
    def test_no_brand_residue_in_canonical_targets(self):
        for raw, mapped in sn.SOURCE_NAME_MAP.items():
            leaks = find_brand_leaks(mapped)
            self.assertEqual(leaks, [], f"{raw!r} -> {mapped!r} leaked {leaks}")

    def test_pipeline_keys_stay_distinct(self):
        import newsnow_sources as nn
        import pushplus_deepseek as pp
        import stock_news_scan as sns

        feed_names = [s.name for s in pp.FEED_SPECS]
        self.assertEqual(len(feed_names), len(set(feed_names)), feed_names)
        self.assertEqual(len(nn.TARGET_SOURCES), 10)
        self.assertEqual(len(set(nn.TARGET_SOURCES)), 10)
        self.assertEqual(len(sns.FINANCE_FETCHERS), 7)
        all_18 = set(sns.FINANCE_FETCHERS) | {"投资社区热榜"} | set(nn.TARGET_SOURCES)
        self.assertEqual(len(all_18), 18, all_18)

    def test_cache_suffix_and_google_subsource(self):
        self.assertEqual(generalize_source("腾讯财经(文件缓存)"), "实时行情主源(文件缓存)")
        self.assertEqual(generalize_source("东方财富（文件缓存）"), "实时行情备源（文件缓存）")
        self.assertEqual(generalize_source("Google·信报财经"), "公开新闻检索")
        self.assertEqual(generalize_source("新闻"), "公开新闻检索")

    def test_clean_wire_prefix_preserves_company_names(self):
        self.assertEqual(
            clean_wire_prefix("财联社10月22日电，腾讯控股今日斥资10亿港元回购股份"),
            "腾讯控股今日斥资10亿港元回购股份",
        )
        self.assertEqual(
            clean_wire_prefix("格隆汇10月22日电｜百度集团智能云订单增长提速"),
            "百度集团智能云订单增长提速",
        )
        self.assertEqual(
            clean_wire_prefix("【金十数据】离岸人民币窄幅震荡"),
            "离岸人民币窄幅震荡",
        )
        self.assertEqual(
            clean_wire_prefix("腾讯控股 Q3 营收稳健增长"),
            "腾讯控股 Q3 营收稳健增长",
        )

    def test_sanitize_error_scrubs_urls_hosts_brands_and_keys(self):
        dirty = (
            "网络请求失败 https://query1.finance.yahoo.com/v8/finance/chart/9988.HK: "
            "EastMoney fallback & DEEPSEEK_API_KEY missing"
        )
        cleaned = sanitize_error(dirty)
        self.assertEqual(find_brand_leaks(cleaned), [], cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("DEEPSEEK_API_KEY", cleaned)

    def test_generalize_markdown_sources_preserves_subject_stocks(self):
        table = (
            "| 标的 | 腾讯控股 |\n"
            "| 百度集团 | 买入 |\n"
            "| Yahoo财经 | 16.80 |\n"
            "| 东方财富 | 16.82 |\n"
        )
        out = generalize_markdown_sources(table)
        self.assertIn("腾讯控股", out)
        self.assertIn("百度集团", out)
        self.assertIn("国际行情源", out)
        self.assertIn("实时行情备源", out)
        self.assertEqual(find_brand_leaks(out), [], out)


class TestEndToEndZeroSourceLeaks(unittest.TestCase):
    def assertZeroLeaks(self, text: str, context_label: str = ""):
        leaks = find_brand_leaks(text)
        self.assertEqual(
            leaks,
            [],
            f"[{context_label}] leaked raw brand names {leaks} in:\n{text[:1200]}",
        )

    def test_quote_verification_blocks_zero_leaks(self):
        import pushplus_deepseek as pp
        import us_quote as uq

        qa = pp.parse_yahoo(pp.YAHOO_SAMPLE)
        qb = pp.parse_eastmoney(pp.EASTMONEY_SAMPLE)
        qc = pp.parse_tencent(pp.TENCENT_SAMPLE)
        outlier = pp.Quote(
            source="东方财富", ok=True, name="阿里巴巴", price=19.20,
            prev_close=16.61, change_pct=15.6,
        )
        failed = pp.Quote(
            source="Yahoo财经", ok=False,
            error="HTTPSConnectionPool(host='query1.finance.yahoo.com'): timeout",
        )
        for qs in ([qa, qb, qc], [qa, outlier, qc], [qa, failed, qc]):
            v = pp.verify_quotes(qs)
            self.assertZeroLeaks(pp.market_block_md("09988", qs, v), "hk_market_block")
            self.assertZeroLeaks(pp.market_context_line("09988", qs, v), "hk_market_context")

        us_quotes = [
            uq.parse_tencent_us(uq.FIXTURES["tencent_nvda"], "NVDA"),
            uq.parse_eastmoney_us(uq.FIXTURES["eastmoney_nvda"], "NVDA"),
            uq.parse_yahoo_us(uq.FIXTURES["yahoo_nvda"], "NVDA"),
            uq.parse_stooq(uq.FIXTURES["stooq_nvda"], "NVDA"),
        ]
        uv = uq.cross_validate(us_quotes)
        self.assertZeroLeaks(uq.us_cross_md(uv), "us_cross_md")
        self.assertZeroLeaks(uq.us_context_line(uv), "us_context_line")

    def test_feeds_sentiment_scan_and_newsnow_zero_leaks(self):
        import newsnow_sources as nn
        import pushplus_deepseek as pp
        import stock_news_scan as sns

        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

        # 1) FeedPack
        fp = pp.FeedPack(hours=48)
        fp.items["实时快讯主源"] = [
            pp._mk_item("财联社 电报", "财联社8月6日电，阿里巴巴云计算收入增长提速", "https://cls.cn/1", now, now),
        ]
        fp.items["AI 智能检索"] = [
            pp._mk_item("DeepSeek 搜索", "港股科技板块获南向资金持续增持", "https://ex.com/2", now, now),
        ]
        fp.errors.append("MKTNews 快讯：https://api.mktnews.net HTTP 502")
        fp.agg = {"n": 2, "pos": 2, "neg": 0, "neu": 0, "mean": 1.0,
                  "n_sources": 2, "综合多头概率锚点": 78}
        self.assertZeroLeaks(pp.render_feed_rule("全球市场", fp), "feed_rule")
        self.assertZeroLeaks(pp.render_feed_appendix(fp), "feed_appendix")
        self.assertZeroLeaks(pp.feed_context("全球市场", fp), "feed_context")

        # 2) ScanPack + SentimentPack
        scan_pack = sns.demo_scan_pack(now)
        self.assertZeroLeaks(sns.render_scan_md(scan_pack), "scan_md")
        self.assertZeroLeaks(sns.scan_context(scan_pack), "scan_context")
        hf_res = sns.hf_sentiment_block(scan_pack, engine="lexicon")
        self.assertZeroLeaks(sns.render_hf_sentiment_md(hf_res), "scan_hf_md")

        sp = pp.SentPack(hours=48)
        sp.items["公开新闻检索"] = [
            pp._mk_item("Google·财经网", "阿里巴巴回购规模创新高", "https://news.google.com/1", now, now),
        ]
        sp.items["海外财经频道"] = [
            pp._mk_item("YouTube·investtalk", "Alibaba Cloud growth outlook", "https://youtu.be/abc", now, now),
        ]
        sp.scan = scan_pack
        sp.agg = {"news": {"n": 1, "pos": 1, "neg": 0, "neu": 0, "mean": 1.0},
                  "social": {"n": 1, "pos": 1, "neg": 0, "neu": 0, "mean": 0.5},
                  "scan_direct": {"n": 5, "pos": 4, "neg": 0, "neu": 1, "mean": 0.8},
                  "综合情绪分": 0.8, "综合多头概率锚点": 74, "分歧度": 0.2}
        self.assertZeroLeaks(pp.render_sentiment_rule("阿里巴巴", "09988", sp), "sentiment_rule")
        self.assertZeroLeaks(pp.render_sentiment_appendix(sp), "sentiment_appendix")
        self.assertZeroLeaks(pp.sentiment_context(sp), "sentiment_context")

        # 3) NewsNow 10 sources
        npk = nn.NewsNowPack(
            items={
                src: [nn.HotItem(source=src, id="1", title="阿里巴巴云计算增长提速",
                                 url="https://zhihu.com/question/1", extra_info="100万热度")]
                for src in nn.TARGET_SOURCES[:4]
            },
            agg={"total": 4, "sources_ok": 4, "sources_total": 10},
        )
        self.assertZeroLeaks(nn.render_newsnow_rule("阿里巴巴", npk), "newsnow_rule")
        self.assertZeroLeaks(nn.render_newsnow_appendix(npk), "newsnow_appendix")
        self.assertZeroLeaks(nn.newsnow_context(npk), "newsnow_context")

        # 4) All 25 templates messages + rule bodies
        full_ctx = "\n\n".join([
            pp.sentiment_context(sp),
            pp.feed_context("阿里巴巴", fp),
            nn.newsnow_context(npk),
        ])
        for tpl in pp.TEMPLATES:
            msgs = pp.build_messages(tpl, "阿里巴巴", full_ctx)
            joined = "\n".join(m["content"] for m in msgs)
            self.assertZeroLeaks(joined, f"prompt:{tpl}")
            rule_md = pp.gen_by_rule("阿里巴巴", tpl, sent_pack=sp, newsnow_pack=npk)
            self.assertZeroLeaks(rule_md, f"rule:{tpl}")

    def test_institutional_columns_preserve_firms_and_hide_channels(self):
        import foreign_institution_weekly as fiw
        import hk_brokers as hkb
        import source_check_db as scdb

        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        bp = hkb.demo_pack(now)
        md_b = hkb.render_hk_brokers_rule("香港券商", bp)
        app_b = hkb.render_hk_brokers_appendix(bp)
        ctx_b = hkb.hk_brokers_context(bp)
        self.assertIn("中金", md_b)
        self.assertZeroLeaks(md_b, "hk_brokers_rule")
        self.assertZeroLeaks(app_b, "hk_brokers_appendix")
        self.assertZeroLeaks(ctx_b, "hk_brokers_context")

        wp = fiw.demo_pack(now)
        md_w = fiw.render_foreign_weekly_rule("外资机构", wp)
        app_w = fiw.render_foreign_weekly_appendix(wp)
        ctx_w = fiw.foreign_weekly_context(wp)
        self.assertTrue("高盛" in md_w or "Goldman" in md_w)
        self.assertZeroLeaks(md_w, "foreign_weekly_rule")
        self.assertZeroLeaks(app_w, "foreign_weekly_appendix")
        self.assertZeroLeaks(ctx_w, "foreign_weekly_context")

        conn = scdb.connect(":memory:")
        scdb.run_checks(conn)
        rep = scdb.latest_report(conn)
        self.assertZeroLeaks(scdb.render_report_md(rep), "source_check_report")
        conn.close()

    def test_dashboard_and_stock_report_zero_leaks(self):
        import server_dashboard as sd
        import stock_report as sr

        for flag in (False, True):
            with mock.patch.object(sd, "SHOW_SOURCE_INFO", flag), \
                 mock.patch.object(sd.hk_quote, "fetch_quote", return_value={"ok": False, "error": "offline"}):
                for code in ("09988", "00700", "03690"):
                    html = sd.render_server_monitor_html(code)
                    # Strip harmless CSS Google Fonts stylesheet link before checking text
                    visible = html.replace("fonts.googleapis.com", "").replace("fonts.gstatic.com", "")
                    self.assertZeroLeaks(visible, f"dashboard:{code}:show={flag}")

        # Offline mock quote + offline scan so run_report runs instantaneously without network
        fake_quote = {
            "ok": True, "code": "600519", "market": "sh", "name": "贵州茅台",
            "price": 1720.0, "prev_close": 1715.0, "open": 1716.0,
            "high": 1725.5, "low": 1705.0, "change": 5.0, "change_pct": 0.29,
            "volume": 1234500, "amount": 2123456000.0, "pe": 25.6, "pb": 8.9,
            "amplitude": 1.19, "turnover_rate": 0.25, "high_52w": 1900.0,
            "low_52w": 1400.0, "market_cap": 2160000000000.0,
            "currency": "CNY", "time": "2026-10-22 15:00:00",
            "source": "实时行情主源", "fetched_at": "2026-10-22 15:00:01 CST",
        }
        import stock_news_scan as sns
        with mock.patch.object(sr.hk_quote, "fetch_quote", return_value=fake_quote), \
             mock.patch("stock_news_scan.scan_stock", return_value=sns.demo_scan_pack()):
            for tpl in ("analysis", "equity", "morning_note"):
                res = sr.run_report(
                    "600519", template=tpl, ai_provider="rule",
                    channel="console", dry_run=True, no_chart=True,
                )
                self.assertZeroLeaks(res["report_md"], f"stock_report_md:{tpl}")
                self.assertZeroLeaks(res["report_html"], f"stock_report_html:{tpl}")

    def test_generated_preview_html_files_zero_leaks(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent
        html_files = [root / "server_monitor.html", *sorted((root / "examples").glob("*.html"))]
        self.assertGreaterEqual(len(html_files), 6)
        for p in html_files:
            content = p.read_text(encoding="utf-8")
            visible = content.replace("fonts.googleapis.com", "").replace("fonts.gstatic.com", "")
            self.assertZeroLeaks(visible, f"preview:{p.name}")

    def test_zero_undefined_globals_across_repo_modules(self):
        import builtins
        import symtable
        from pathlib import Path

        builtin_names = set(dir(builtins)) | {
            "__file__", "__name__", "__doc__", "__package__", "__spec__", "__annotations__",
        }
        root = Path(__file__).resolve().parent
        for py in sorted(root.glob("*.py")):
            st = symtable.symtable(py.read_text(encoding="utf-8"), str(py), "exec")
            top_defs = {
                s.get_name() for s in st.get_symbols()
                if s.is_imported() or s.is_assigned() or s.is_namespace()
            } | builtin_names

            def walk_st(tbl, enclosing):
                local_defs = {
                    s.get_name() for s in tbl.get_symbols()
                    if s.is_parameter() or s.is_imported() or s.is_assigned() or s.is_namespace()
                }
                visible = enclosing | local_defs | top_defs
                bad = [
                    (tbl.get_lineno(), tbl.get_name(), s.get_name())
                    for s in tbl.get_symbols()
                    if s.is_global() and s.get_name() not in visible
                ]
                for child in tbl.get_children():
                    next_enc = enclosing if tbl.get_type() == "class" else visible
                    bad.extend(walk_st(child, next_enc))
                return bad

            self.assertEqual(walk_st(st, set()), [], f"Undefined globals in {py.name}")


if __name__ == "__main__":
    unittest.main()
