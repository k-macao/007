import unittest
from types import SimpleNamespace
from unittest.mock import patch

import hf_hub
import pushplus_deepseek as pp


class PushSentimentTest(unittest.TestCase):
    def setUp(self):
        # 美银牛熊指标默认会自动刷新（会联网）：单测里固定关掉，
        # 保证断言只依赖内置快照，与运行环境的网络/最新新闻无关。
        env_patch = patch.dict('os.environ', {'BOFA_BB_AUTO': '0'})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_empty(self):
        md = pp.render_sentiment_appendix(pp.SentPack(hours=48))
        self.assertIn('金融情绪复核', md)
        self.assertIn('暂无可分析', md)

    def test_result_cached_and_deduplicated(self):
        item = SimpleNamespace(title='净利润大幅增长')
        pack = pp.SentPack(hours=48, items={'news': [item, item]})
        pack.scan = SimpleNamespace(direct={'news': [item]}, sector_hits={})
        result = hf_hub.score_texts([item.title], engine='lexicon')
        with patch.object(pp, 'env', return_value='auto'), patch.object(
                hf_hub, 'score_texts', return_value=result) as score:
            md = pp.render_financial_sentiment_push(pack)
            self.assertEqual(md, pp.render_financial_sentiment_push(pack))
            score.assert_called_once_with([item.title], engine='auto')
        self.assertIn('情绪判定', md)
        self.assertIn('本地金融词典', md)
        # 读者侧不再出现引擎模式/请求模式这类元说明
        self.assertNotIn('非四路并行', md)
        self.assertNotIn('请求模式', md)
        self.assertNotIn('备注', md)

    def test_daily_wechat_feed_review(self):
        item = SimpleNamespace(title='净利润大幅增长')
        feed = SimpleNamespace(hours=48, items={'feed': [item]})
        with patch.object(pp, 'env', return_value='lexicon'):
            md = pp.render_feed_financial_sentiment_push(feed)
        self.assertIn('金融情绪复核', md)
        self.assertIn('情绪判定', md)
        self.assertIn('本地金融词典', md)

    def test_failure_is_visible_and_cached(self):
        pack = pp.SentPack(hours=48, items={'news': [SimpleNamespace(title='新闻')]})
        with patch.object(hf_hub, 'score_texts', side_effect=RuntimeError('secret')):
            md = pp.render_financial_sentiment_push(pack)
        # 失败时只留一句合规声明，不向读者解释「情绪复核不可用」
        self.assertIn('非投资建议', md)
        self.assertNotIn('暂不可用', md)
        self.assertNotIn('secret', md)
        self.assertEqual(md, pack.financial_sentiment_md)

    def test_scheduled_push_title_default_and_delivery(self):
        from pathlib import Path

        expected = '章鱼 AI.券商研报 007'
        self.assertEqual(pp.BRAND_TITLE, expected)
        self.assertEqual(pp.SCHEDULED_PUSH_TITLE, expected)
        self.assertIn(f'## 🐙 {expected}', pp.brand_header_md())

        wf = (Path(__file__).resolve().parent / '.github' / 'workflows' / 'market-brief.yml').read_text(encoding='utf-8')
        self.assertIn("PUSH_TITLE: '章鱼 AI.券商研报 007'", wf)
        self.assertEqual(wf.count('--title "$PUSH_TITLE"'), 2)

        fake_feed = pp.FeedPack(hours=24)
        fake_feed.agg = {
            'n': 0, 'pos': 0, 'neg': 0, 'neu': 0,
            'mean': 0.0, 'n_sources': 0, '综合多头概率锚点': 50,
        }
        captured = []

        def fake_post_form(url, fields, timeout=30):
            captured.append((url, dict(fields)))
            return 200, '{"code": 200, "msg": "ok"}'

        with patch.dict('os.environ', {'PUSHPLUS_TOKEN': 'tok-test', 'BRIEF_COLUMNS': '0'}, clear=False), \
             patch.object(pp, 'collect_feeds', return_value=fake_feed), \
             patch.object(pp, 'http_post_form', side_effect=fake_post_form):
            rc = pp.main([
                '--template', 'feedscan',
                '--topic', '全球市场早报',
                '--channel', 'pushplus',
                '--ai-provider', 'rule',
                '--theme', 'dos',
                '--hours', '24',
                '--no-chart',
                '--no-ai-compress',
                '--no-state',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1]['title'], expected)
        self.assertIn(expected, captured[0][1]['content'])

    def test_feedscan_rule_section_order(self):
        import foreign_institution_weekly as fiw
        import hk_brokers as hb

        fake_feed = pp.FeedPack(
            hours=24,
            items={
                '综合财经快讯': [
                    pp.SentItem(
                        source='综合财经快讯',
                        title='海外通胀数据超预期引发降息推迟担忧',
                        score=-0.6,
                        label='利空',
                    )
                ]
            },
            agg={
                'n': 1, 'pos': 0, 'neg': 1, 'neu': 0,
                'mean': -0.6, 'n_sources': 1, '综合多头概率锚点': 38,
            },
        )
        captured = []

        def fake_post_form(url, fields, timeout=30):
            captured.append((url, dict(fields)))
            return 200, '{"code": 200, "msg": "ok"}'

        with patch.dict('os.environ', {'PUSHPLUS_TOKEN': 'tok-test', 'BRIEF_COLUMNS': '1'}, clear=False), \
             patch.object(pp, 'collect_feeds', return_value=fake_feed), \
             patch.object(fiw, 'collect_foreign_weekly', return_value=fiw.demo_pack()), \
             patch.object(hb, 'collect_hk_brokers', return_value=hb.demo_pack()), \
             patch.object(pp, 'http_post_form', side_effect=fake_post_form):
            rc = pp.main([
                '--template', 'feedscan',
                '--topic', '全球市场早报',
                '--channel', 'pushplus',
                '--ai-provider', 'rule',
                '--theme', 'dos',
                '--hours', '24',
                '--no-chart',
                '--no-ai-compress',
                '--no-state',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        html = captured[0][1]['content']
        idx_bofa = html.index('BofA Bull &amp; Bear Indicator (B&amp;B)')
        idx_risk = html.index('研报 AI 风险点提示')
        idx_foreign = html.index('🏛 国外研报')
        idx_foreign_detail = html.index('国外机构一周研报明细')
        idx_other_feed = html.index('全市场快讯与机构动态')
        idx_hk = html.index('香港券商新闻与数据')
        idx_sentiment = html.index('金融情绪复核')
        idx_freshness = html.index('数据新鲜度 · 本次运行指纹')
        idx_footer = html.rindex('仅供参考，分析研究')
        self.assertLess(idx_bofa, idx_risk)
        self.assertLess(idx_risk, idx_foreign)
        self.assertLess(idx_foreign, idx_foreign_detail)
        self.assertLess(idx_foreign_detail, idx_other_feed)
        self.assertLess(idx_other_feed, idx_hk)
        self.assertLess(idx_hk, idx_sentiment)
        self.assertLess(idx_sentiment, idx_freshness)
        self.assertLess(idx_freshness, idx_footer)

    def test_feedscan_ai_scrambles_sections_still_reordered(self):
        import foreign_institution_weekly as fiw
        import hk_brokers as hb

        fake_feed = pp.FeedPack(
            hours=24,
            items={},
            agg={
                'n': 0, 'pos': 0, 'neg': 0, 'neu': 0,
                'mean': 0.0, 'n_sources': 0, '综合多头概率锚点': 50,
            },
        )
        scrambled_ai_md = (
            '## 📊 全市场快讯与机构动态\n\n- 三大主线与香港券商动态\n\n'
            '## 🏛 国外研报\n\n- 高盛与摩根士丹利最新研报汇总\n\n'
            '## ⚠️ 研报 AI 风险点提示\n\n- 警惕高估值科技股回调与利率路径反复'
        )
        captured = []

        def fake_post_form(url, fields, timeout=30):
            captured.append((url, dict(fields)))
            return 200, '{"code": 200, "msg": "ok"}'

        with patch.dict(
            'os.environ',
            {'PUSHPLUS_TOKEN': 'tok-test', 'DEEPSEEK_API_KEY': 'sk-test', 'BRIEF_COLUMNS': '1'},
            clear=False,
        ), \
             patch.object(pp, 'collect_feeds', return_value=fake_feed), \
             patch.object(fiw, 'collect_foreign_weekly', return_value=fiw.demo_pack()), \
             patch.object(hb, 'collect_hk_brokers', return_value=hb.demo_pack()), \
             patch.object(pp, 'chat_completion', return_value=scrambled_ai_md), \
             patch.object(pp, 'http_post_form', side_effect=fake_post_form):
            rc = pp.main([
                '--template', 'feedscan',
                '--topic', '全球市场早报',
                '--channel', 'pushplus',
                '--ai-provider', 'deepseek',
                '--theme', 'dos',
                '--hours', '24',
                '--no-chart',
                '--no-ai-compress',
                '--no-state',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        html = captured[0][1]['content']
        self.assertLess(html.index('BofA Bull &amp; Bear Indicator (B&amp;B)'), html.index('研报 AI 风险点提示'))
        self.assertLess(html.index('研报 AI 风险点提示'), html.index('🏛 国外研报'))
        self.assertLess(html.index('🏛 国外研报'), html.index('国外机构一周研报明细'))
        self.assertLess(html.index('国外机构一周研报明细'), html.index('全市场快讯与机构动态'))
        self.assertLess(html.index('全市场快讯与机构动态'), html.index('香港券商新闻与数据'))
        self.assertLess(html.index('香港券商新闻与数据'), html.index('金融情绪复核'))
        self.assertLess(html.index('金融情绪复核'), html.index('数据新鲜度 · 本次运行指纹'))

    def test_foreign_weekly_section_order(self):
        import foreign_institution_weekly as fiw

        captured = []

        def fake_post_form(url, fields, timeout=30):
            captured.append((url, dict(fields)))
            return 200, '{"code": 200, "msg": "ok"}'

        with patch.dict('os.environ', {'PUSHPLUS_TOKEN': 'tok-test'}, clear=False), \
             patch.object(fiw, 'collect_foreign_weekly', return_value=fiw.demo_pack()), \
             patch.object(pp, 'http_post_form', side_effect=fake_post_form):
            rc = pp.main([
                '--template', 'foreign_weekly',
                '--topic', '全球市场',
                '--channel', 'pushplus',
                '--ai-provider', 'rule',
                '--theme', 'dos',
                '--hours', '168',
                '--no-chart',
                '--no-ai-compress',
                '--no-state',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        html = captured[0][1]['content']
        self.assertLess(html.index('BofA Bull &amp; Bear Indicator (B&amp;B)'), html.index('研报 AI 风险点提示'))
        self.assertLess(html.index('研报 AI 风险点提示'), html.index('🏛 国外研报'))
        self.assertLess(html.index('🏛 国外研报'), html.index('国外机构一周研报明细'))
        self.assertLess(html.index('国外机构一周研报明细'), html.index('机构情绪汇总与补充说明'))
        self.assertLess(html.index('机构情绪汇总与补充说明'), html.index('数据新鲜度 · 本次运行指纹'))

    def test_bofa_bb_char_table_and_freshness_last_across_templates(self):
        import stock_report as sr
        from source_names import find_brand_leaks

        tbl = pp.render_bofa_bb_char_table()
        blk = pp.render_bofa_bb_char_block()
        widths = {len(ln) for ln in tbl.splitlines()}
        self.assertEqual(widths, {66})
        self.assertTrue(all(ord(ch) < 128 for ch in tbl))
        self.assertEqual(find_brand_leaks(blk), [])
        self.assertIn('## 📊 美银牛熊指标 · BofA Bull & Bear Indicator (B&B)', blk)
        self.assertNotIn('## 🧭 美银牛熊指标', blk)
        self.assertNotIn('.---~~~~---.', tbl)
        for token in (
            'BofA Bull & Bear (B&B)',
            '8.8 SELL', 'prev 9.3 (-0.5)',
            'Data as of 2026-10-01',
            '* now 8.8', '^ prev 9.3',
            '<2 Buy | 2-8 Neutral | >8 Sell',
        ):
            self.assertIn(token, tbl)
        # 字符卡要够简洁：整块不超过 8 行
        self.assertLessEqual(len(tbl.splitlines()), 8)
        # 六大分项下沉到正文行，不再画表格
        for token in ('对冲基金仓位', '64%', '股票资金流向', '89%',
                      '债券资金流向', '信贷市场技术面', '全球股指广度',
                      '基金经理调查仓位', '100%'):
            self.assertIn(token, blk)

        captured = []

        def fake_post_form(url, fields, timeout=30):
            captured.append((url, dict(fields)))
            return 200, '{"code": 200, "msg": "ok"}'

        with patch.dict('os.environ', {'PUSHPLUS_TOKEN': 'tok-test'}, clear=False), \
             patch.object(pp, 'http_post_form', side_effect=fake_post_form):
            rc = pp.main([
                '--template', 'brief',
                '--topic', '港股市场',
                '--channel', 'pushplus',
                '--ai-provider', 'rule',
                '--theme', 'dos',
                '--no-chart',
                '--no-ai-compress',
                '--no-state',
            ])
        self.assertEqual(rc, 0)
        html = captured[0][1]['content']
        self.assertIn('<pre', html)
        self.assertLess(html.index('BofA Bull &amp; Bear Indicator (B&amp;B)'), html.index('港股市场'))
        self.assertLess(html.index('港股市场'), html.index('数据新鲜度 · 本次运行指纹'))

        wrapped, _meta = sr.wrap_with_latest_features(
            '## 核心研报正文\n\n- 关键结论',
            raw_code='09988', template='analysis', topic='HK09988 阿里巴巴',
            hours=48, no_chart=True, quote={
                'price': 16.8, 'name': '阿里巴巴', 'source_label': '演示',
                'prev_close': 16.6, 'change_pct': 1.2, 'time': '2026-08-13 15:00',
            }, sent_pack=None, persist_state=False,
        )
        self.assertLess(wrapped.index('核心研报正文'), wrapped.index('数据新鲜度 · 本次运行指纹'))

    def test_selftest_and_main_resilient_when_hf_token_set(self):
        import os
        import hk_brokers as hb

        dirty = {
            'HF_TOKEN': 'hf_dirty_secret_token',
            'HUGGINGFACE_API_KEY': 'hf_dirty_alias',
            'SILICONFLOW_API_KEY': 'sf_dirty_key',
            'SENTIMENT_ENGINE': 'hf',
            'DEEPSEEK_API_KEY': '',
            'OPENAI_API_KEY': '',
        }
        with patch.dict('os.environ', dirty, clear=False):
            self.assertEqual(pp.selftest(), 0)
            self.assertEqual(os.environ.get('HF_TOKEN'), 'hf_dirty_secret_token')

        captured = []

        def fake_post_form(url, fields, timeout=30):
            captured.append((url, dict(fields)))
            return 200, '{"code": 200, "msg": "ok"}'

        with patch.dict(
            'os.environ',
            {'PUSHPLUS_TOKEN': 'tok-test', 'HF_TOKEN': 'hf_no_credits', 'DEEPSEEK_API_KEY': ''},
            clear=False,
        ), \
             patch.object(hb, 'collect_hk_brokers', return_value=hb.demo_pack()), \
             patch.object(pp, 'chat_completion', side_effect=pp.PushError('HTTP 402: no remaining credits')), \
             patch.object(pp, 'http_post_form', side_effect=fake_post_form):
            rc = pp.main([
                '--template', 'hk_brokers',
                '--topic', '港股市场',
                '--channel', 'pushplus',
                '--ai-provider', 'deepseek',
                '--theme', 'dos',
                '--hours', '96',
                '--no-chart',
                '--no-state',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        self.assertIn('香港券商新闻与数据', captured[0][1]['content'])

    def test_deai_investment_advisor_polish_and_rule_style(self):
        import equity_research_column as erc
        import foreign_institution_weekly as fiw
        import hk_brokers as hb
        import skills_hub as sh

        for phrase in (
            '你是一个投资顾问',
            '经验丰富的投研分析师',
            '“去AI化”润色',
            '首先、其次、最后',
            '说起来',
            '实际上',
            '在多数情况下',
            '或许',
            '值得注意的是',
            '毫无疑问',
            '一般来说',
            '反问句或设问句',
            '无需解释修改了哪里',
        ):
            self.assertIn(phrase, pp.DEAI_POLISH_PROMPT)

        self.assertIn('你是一个投资顾问', pp.build_messages('feedscan', '全球市场早报', '')[0]['content'])
        self.assertIn('你是一个投资顾问', erc.build_system_prompt('full'))
        self.assertIn('你是一个投资顾问', sh.build_system_prompt('morning_note'))

        fake_feed = pp.FeedPack(
            hours=24,
            items={},
            agg={
                'n': 0, 'pos': 0, 'neg': 0, 'neu': 0,
                'mean': 0.0, 'n_sources': 0, '综合多头概率锚点': 50,
            },
        )
        draft_md = (
            '## ⚠️ 研报 AI 风险点提示\n\n'
            '首先，科技股估值处于高位。其次，毫无疑问利率路径存在分歧。'
            '综上所述，值得注意的是短期追高风险偏大，投资者需要保持警惕并控制仓位节奏。\n\n'
            '## 🏛 国外研报\n\n'
            '一般来说，高盛与摩根士丹利近期对科技硬件给出偏积极评级，小摩则下调新能源车预期。\n\n'
            '## 📊 全市场快讯与机构动态\n\n'
            '整体来看，南向资金仍在寻找高股息安全垫与业绩确定性标的。'
        )
        polished_md = (
            '好的，以下是润色后的全文：\n\n'
            '## ⚠️ 研报 AI 风险点提示\n\n'
            '说起来，热门科技股的估值眼下已经不算便宜。实际上，市场对利率路径的算盘还在打架——'
            '这时候闭眼追高真的划算吗？在多数情况下，先把仓位安全垫垫厚一点或许更从容。\n\n'
            '## 🏛 国外研报\n\n'
            '高盛和大摩还在给算力硬件递梯子，小摩却对电动车毛利率泼了一盆冷水，外资席位的分歧摆在明面上。\n\n'
            '## 📊 全市场快讯与机构动态\n\n'
            '南向资金一边蹲守高股息做防守，一边盯着财报真能兑现的龙头。\n\n'
            '修改说明：已去除机械连接词并加入反问句。'
        )
        llm_calls = []

        def fake_cc(url, key, model, messages, *args, **kwargs):
            llm_calls.append(messages)
            if len(llm_calls) == 1:
                return draft_md
            return polished_md

        captured = []

        def fake_post_form(url, fields, timeout=30):
            captured.append((url, dict(fields)))
            return 200, '{"code": 200, "msg": "ok"}'

        with patch.dict(
            'os.environ',
            {'PUSHPLUS_TOKEN': 'tok-test', 'DEEPSEEK_API_KEY': 'sk-test', 'BRIEF_COLUMNS': '1'},
            clear=False,
        ), \
             patch.object(pp, 'collect_feeds', return_value=fake_feed), \
             patch.object(fiw, 'collect_foreign_weekly', return_value=fiw.demo_pack()), \
             patch.object(hb, 'collect_hk_brokers', return_value=hb.demo_pack()), \
             patch.object(pp, 'chat_completion', side_effect=fake_cc), \
             patch.object(pp, 'http_post_form', side_effect=fake_post_form):
            rc = pp.main([
                '--template', 'feedscan',
                '--topic', '全球市场早报',
                '--channel', 'pushplus',
                '--ai-provider', 'deepseek',
                '--theme', 'dos',
                '--hours', '24',
                '--no-chart',
                '--no-ai-compress',
                '--no-state',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(llm_calls), 2)
        self.assertIn('“去AI化”润色', llm_calls[1][1]['content'])
        html = captured[0][1]['content']
        self.assertIn('这时候闭眼追高真的划算吗', html)
        self.assertNotIn('修改说明', html)
        self.assertLess(html.index('BofA Bull &amp; Bear Indicator (B&amp;B)'), html.index('研报 AI 风险点提示'))
        self.assertLess(html.index('研报 AI 风险点提示'), html.index('🏛 国外研报'))
        self.assertLess(html.index('金融情绪复核'), html.index('数据新鲜度 · 本次运行指纹'))

        # Verify rule-mode narratives also avoid AI clichés and use natural advisor cadence
        rule_brief = pp.render_market_brief_rule(
            '全球市场早报', fake_feed, fw_pack=fiw.demo_pack(), hk_pack=hb.demo_pack(),
            columns_enabled=True, has_foreign_detail=True,
        )
        rule_fw = fiw.render_foreign_weekly_rule('全球市场', fiw.demo_pack())
        rule_hk = hb.render_hk_brokers_rule('港股市场', hb.demo_pack())
        for txt in (rule_brief, rule_fw, rule_hk):
            for banned in ('首先，', '其次，', '最后，', '综上所述', '毫无疑问', '值得注意的是', '一般来说'):
                self.assertNotIn(banned, txt)


class BofaBbRefreshTest(unittest.TestCase):
    """美银牛熊指标「数据没更新」回归测试：日期、过期标注、快照优先级、新闻解析。"""

    NEWS = """<?xml version="1.0"?><rss><channel>
<item><title>BofA's Bull &amp; Bear Indicator hits 9.5 from 9.3 - Seeking Alpha</title>
<link>https://example.com/a</link><pubDate>Tue, 03 Nov 2026 12:00:00 GMT</pubDate></item>
<item><title>BofA Bull &amp; Bear Indicator fell to 8.8 from 9.3 - Reuters</title>
<link>https://example.com/b</link><pubDate>Thu, 01 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Unrelated market wrap - X</title>
<pubDate>Wed, 04 Nov 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""

    def test_char_box_carries_as_of_and_stale_flag(self):
        tbl = pp.render_bofa_bb_char_table({
            'current': 8.8, 'previous': 9.3, 'as_of': '2026-10-01',
            'signal': 'Sell', 'components': [],
        })
        self.assertIn('Data as of 2026-10-01', tbl)
        self.assertIn('STALE', tbl)
        self.assertLessEqual(len(tbl.splitlines()), 8)  # 简洁：≤8 行
        self.assertEqual({len(ln) for ln in tbl.splitlines()}, {66})
        self.assertTrue(all(ord(ch) < 128 for ch in tbl))
        fresh = pp.render_bofa_bb_char_table({
            'current': 9.5, 'previous': 8.8,
            'as_of': pp.datetime.now(pp.CST).date().isoformat(),
            'signal': 'Sell', 'components': [],
        })
        self.assertNotIn('STALE', fresh)

    def test_block_reports_date_and_never_pretends_latest(self):
        blk = pp.render_bofa_bb_char_block({
            'current': 8.8, 'previous': 9.3, 'as_of': '2026-10-01',
            'signal': 'Sell', 'signal_cn': '反向卖出',
            'source': 'BofA The Flow Show 2026-10-01', 'components': [],
        })
        self.assertIn('截至 **2026-10-01**', blk)
        self.assertIn('已过期', blk)
        self.assertIn('数据来源', blk)
        from source_names import find_brand_leaks
        self.assertEqual(find_brand_leaks(blk), [])

    def test_news_parse_picks_newest_reading(self):
        parsed = pp.parse_bofa_bb_news(
            self.NEWS, now=pp.datetime(2026, 11, 6, tzinfo=pp.UTC))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['current'], 9.5)
        self.assertEqual(parsed['previous'], 9.3)
        self.assertEqual(parsed['as_of'], '2026-11-03')
        self.assertEqual(parsed['signal'], 'Sell')
        self.assertNotIn('Seeking', parsed['source'])  # 品牌名不外泄到正文

    def test_snapshot_beats_default_and_json_beats_snapshot(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'bofa_bb.json')
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump({'current': 9.5, 'previous': 9.3, 'as_of': '2026-11-03',
                           'signal': 'Sell', 'signal_cn': '反向卖出',
                           'source': '演示快照'}, fh)
            with patch.dict('os.environ', {'BOFA_BB_SNAPSHOT': path}, clear=False):
                data = pp.load_bofa_bb_data(allow_network=False)
                self.assertEqual(data['current'], 9.5)
                self.assertEqual(data['as_of'], '2026-11-03')
                with patch.dict(
                    'os.environ',
                    {'BOFA_BB_JSON': json.dumps({'current': 7.0, 'previous': 6.8,
                                                 'as_of': '2026-01-02'})},
                    clear=False,
                ):
                    manual = pp.load_bofa_bb_data(allow_network=False)
                    self.assertEqual(manual['current'], 7.0)
                    self.assertTrue(manual['manual_override'])

    def test_refresh_writes_snapshot_from_news(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'bofa_bb.json')
            with patch.dict('os.environ', {'BOFA_BB_SNAPSHOT': path}, clear=False), \
                 patch.object(pp, 'http_request',
                              return_value=(200, self.NEWS)):
                data = pp.load_bofa_bb_data(allow_network=True)
            self.assertEqual(data['current'], 9.5)
            self.assertTrue(data.get('refreshed'))
            self.assertTrue(os.path.exists(path))
            self.assertEqual(data['components'],
                             pp.DEFAULT_BOFA_BB_DATA['components'])

    def test_network_failure_falls_back_to_snapshot(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'bofa_bb.json')
            with patch.dict('os.environ', {'BOFA_BB_SNAPSHOT': path}, clear=False), \
                 patch.object(pp, 'http_request',
                              side_effect=pp.PushError('offline')):
                data = pp.load_bofa_bb_data(allow_network=True)
            self.assertEqual(data['current'], pp.DEFAULT_BOFA_BB_DATA['current'])
            self.assertFalse(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
