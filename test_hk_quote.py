#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_hk_quote.py — 免费港股行情模块测试
  - 离线解析自检：用内置真实抓包样本验证 腾讯/东财/Yahoo 解析
  - normalize_code 各种输入写法
  - fetch_quote 降级逻辑（断网时返回 None 不抛异常）
运行：python test_hk_quote.py   （或 pytest test_hk_quote.py）
"""
import json
import unittest

import hk_quote


class TestNormalize(unittest.TestCase):
    def test_variants(self):
        cases = {
            "00700": "00700", "0700": "00700", "700": "00700",
            "0700.HK": "00700", "hk00700": "00700", "HK:0700": "00700",
            "9988": "09988", "03690": "03690", "3690": "03690",
        }
        for raw, want in cases.items():
            self.assertEqual(hk_quote.normalize_code(raw), want, raw)


class TestDetectMarket(unittest.TestCase):
    def test_market_detection(self):
        """港股 5 位 / A 股 6 位自动识别（含显式前缀写法）"""
        cases = {
            "09988": ("hk", "09988"),
            "9988.HK": ("hk", "09988"),
            "hk00700": ("hk", "00700"),
            "600519": ("sh", "600519"),
            "600519.SH": ("sh", "600519"),
            "sh600519": ("sh", "600519"),
            "688981": ("sh", "688981"),
            "000001": ("sz", "000001"),
            "000001.SZ": ("sz", "000001"),
            "sz000001": ("sz", "000001"),
            "300750": ("sz", "300750"),
        }
        for raw, (want_m, want_c) in cases.items():
            m, c, _ = hk_quote.detect_market(raw)
            self.assertEqual((m, c), (want_m, want_c), raw)

    def test_market_em_secid(self):
        self.assertEqual(hk_quote.detect_market("00700")[2], "116.00700")
        self.assertEqual(hk_quote.detect_market("600519")[2], "1.600519")
        self.assertEqual(hk_quote.detect_market("000001")[2], "0.000001")


class TestAShareParsersOffline(unittest.TestCase):
    def test_tencent_sh600519(self):
        q = hk_quote._parse_tencent(hk_quote.FIXTURES["tencent_sh600519"], "600519", "sh")
        self.assertIsNotNone(q)
        self.assertEqual(q["market"], "sh")
        self.assertEqual(q["price"], 1720.0)
        self.assertEqual(q["pe"], 25.6)
        self.assertEqual(q["pb"], 8.9)
        self.assertEqual(q["currency"], "CNY")
        self.assertEqual(q["name"], "贵州茅台")
        self.assertEqual(q["change_pct"], 0.29)
        self.assertEqual(q["high"], 1725.5)
        self.assertEqual(q["low"], 1705.0)
        self.assertEqual(q["turnover_rate"], 0.25)

    def test_tencent_sz000001(self):
        q = hk_quote._parse_tencent(hk_quote.FIXTURES["tencent_sz000001"], "000001", "sz")
        self.assertIsNotNone(q)
        self.assertEqual(q["market"], "sz")
        self.assertEqual(q["price"], 11.9)
        self.assertEqual(q["currency"], "CNY")
        self.assertEqual(q["name"], "平安银行")

    def test_eastmoney_sz000001(self):
        q = hk_quote.parse_eastmoney_a(
            json.loads(hk_quote.FIXTURES["eastmoney_sz000001"]), "000001", "sz")
        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 11.9)
        self.assertEqual(q["pe"], 5.5)       # 东财 A 股接口带 PE（区别于港股）
        self.assertEqual(q["pb"], 0.55)
        self.assertEqual(q["turnover_rate"], 0.63)
        self.assertEqual(q["currency"], "CNY")


class TestParsersOffline(unittest.TestCase):
    def test_tencent_00700(self):
        q = hk_quote.parse_tencent(hk_quote.FIXTURES["tencent_00700"], "00700")
        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 478.8)
        self.assertEqual(q["high"], 483.2)
        self.assertEqual(q["low"], 475.4)
        self.assertEqual(q["change"], -0.4)
        self.assertEqual(q["change_pct"], -0.08)
        self.assertEqual(q["pe"], 17.47)
        self.assertEqual(q["pb"], 3.46)
        self.assertEqual(q["turnover_rate"], 0.18)
        self.assertEqual(q["amplitude"], 1.63)
        self.assertEqual(q["currency"], "HKD")
        self.assertEqual(q["name"], "腾讯控股")
        self.assertEqual(q["time"], "2026/08/07 16:08:23")
        self.assertEqual(q["high_52w"], 677.7)
        self.assertEqual(q["low_52w"], 411.0)
        self.assertEqual(q["market_cap"], 4348807140000.0)

    def test_tencent_09988(self):
        q = hk_quote.parse_tencent(hk_quote.FIXTURES["tencent_09988"], "09988")
        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 123.8)
        self.assertEqual(q["pe"], 20.23)
        self.assertEqual(q["change_pct"], -0.48)
        self.assertEqual(q["name"], "阿里巴巴-W")

    def test_eastmoney_00700(self):
        q = hk_quote.parse_eastmoney(json.loads(hk_quote.FIXTURES["eastmoney_00700"]), "00700")
        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 478.8)
        self.assertEqual(q["high"], 483.2)
        self.assertEqual(q["pb"], 3.42)
        self.assertEqual(q["turnover_rate"], 0.18)
        self.assertEqual(q["amplitude"], 1.63)
        self.assertIsNone(q["pe"])   # 东财 HK 接口无 PE

    def test_yahoo_00700(self):
        q = hk_quote.parse_yahoo(json.loads(hk_quote.FIXTURES["yahoo_00700"]), "00700")
        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 478.8)
        self.assertEqual(q["currency"], "HKD")
        self.assertEqual(q["volume"], 16320039)
        self.assertEqual(q["high_52w"], 683.0)

    def test_cross_source_consistency(self):
        """三源解析结果应互相一致（同一抓包日期的收盘行情）"""
        t = hk_quote.parse_tencent(hk_quote.FIXTURES["tencent_00700"], "00700")
        e = hk_quote.parse_eastmoney(json.loads(hk_quote.FIXTURES["eastmoney_00700"]), "00700")
        y = hk_quote.parse_yahoo(json.loads(hk_quote.FIXTURES["yahoo_00700"]), "00700")
        for src in (e, y):
            self.assertAlmostEqual(t["price"], src["price"], places=1)
            self.assertAlmostEqual(t["high"], src["high"], places=1)
            self.assertAlmostEqual(t["low"], src["low"], places=1)


class TestFetchDegradation(unittest.TestCase):
    def test_fetch_quote_no_crash(self):
        """断网/被限制环境下 fetch_quote 返回 None 而非抛异常（视图降级静态）"""
        try:
            q = hk_quote.fetch_quote("00700")
            self.assertTrue(q is None or q.get("price"))
        except Exception:  # noqa: BLE001
            self.fail("fetch_quote 不应在数据源失败时抛异常")

    def test_fetch_quote_invalid_code(self):
        self.assertIsNone(hk_quote.fetch_quote("!!!"))
        self.assertIsNone(hk_quote.fetch_quote(""))


class TestTradeViewIntegration(unittest.TestCase):
    def test_kline_view_data(self):
        """测试服务器端 /api/chart 对应视图函数能否生成正确的 60 根字符模拟图数据与指标（兼容旧 /api/kline）"""
        import server_dashboard
        # 新名 get_chart_view，旧名 get_kline_view 仍保留兼容
        fn = getattr(server_dashboard, "get_chart_view", None) or getattr(server_dashboard, "get_kline_view")
        d = fn("09988", tf="daily", count=60)
        self.assertEqual(d["code"], "09988")
        self.assertEqual(len(d["bars"]), 60)
        self.assertIn("open", d["bars"][0])
        self.assertIn("close", d["bars"][-1])
        self.assertIn("ma5", d["bars"][-1])
        self.assertIn("ma10", d["bars"][-1])
        self.assertIn("ma20", d["bars"][-1])
        self.assertIsNotNone(d["support"])
        self.assertIsNotNone(d["resistance"])
        # 兼容旧名
        if hasattr(server_dashboard, "get_kline_view"):
            d2 = server_dashboard.get_kline_view("09988", tf="daily", count=60)
            self.assertEqual(len(d2["bars"]), 60)

    def test_tradeview_html_present(self):
        """测试 render_server_monitor_html 渲染出的 HTML 是否包含字符模拟图交互组件"""
        import server_dashboard
        html = server_dashboard.render_server_monitor_html("09988")
        self.assertIn("tradeview-section", html)
        # 新版为 CHAR SIMULATION，兼容旧版 TRADEVIEW 关键字
        self.assertTrue("CHAR SIMULATION" in html or "TRADEVIEW" in html)
        self.assertIn("tradeview-char-canvas", html)
        self.assertIn("tradeview-vol-canvas", html)
        self.assertTrue("generateDefaultChartBars" in html or "generateDefaultKlineBars" in html)

    def test_feed_source_hidden_toggle(self):
        """源头信息默认隐藏：快讯卡片不渲染来源标签；SHOW_SOURCE_INFO=1 时恢复"""
        import server_dashboard as sd
        from unittest import mock
        with mock.patch.object(sd, "SHOW_SOURCE_INFO", False):
            hidden = sd.render_server_monitor_html("09988")
        with mock.patch.object(sd, "SHOW_SOURCE_INFO", True):
            shown = sd.render_server_monitor_html("09988")
        self.assertNotIn('<span class="feed-src">', hidden)
        self.assertNotIn("Google 新闻", hidden)
        self.assertIn('<span class="feed-src">Google 新闻</span>', shown)
        # 数据层仍保留 source 字段（供情绪归因 / 去重 / 后台统计），只是前端不展示
        self.assertTrue(sd.STOCKS["09988"]["feeds"][0]["source"])


class TestAIReportIntegration(unittest.TestCase):
    def test_report_endpoint_payload(self):
        """测试 AI 研报接口 _api_report 离线（rule 降级）返回完整结构"""
        import server_dashboard
        r = server_dashboard._api_report({"code": "600519", "channel": "console", "dry_run": True})
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["market"], "sh")
        self.assertEqual(r["code"], "600519")
        self.assertIn("report_md", r)
        self.assertIn("report_html", r)
        self.assertIn("push", r)

    def test_report_endpoint_missing_code(self):
        import server_dashboard
        r = server_dashboard._api_report({})
        self.assertFalse(r.get("ok"))
        self.assertIn("缺少股票代码", r.get("error", ""))

    def test_generic_profile_for_ashare(self):
        import server_dashboard
        v = server_dashboard.get_stock_view("600519")
        self.assertEqual(v.get("market"), "sh")
        self.assertEqual(v.get("code"), "600519")
        self.assertIn("factors", v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
