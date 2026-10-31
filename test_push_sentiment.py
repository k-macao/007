import unittest
from types import SimpleNamespace
from unittest.mock import patch

import hf_hub
import pushplus_deepseek as pp


class PushSentimentTest(unittest.TestCase):
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
        idx_risk = html.index('研报 AI 风险点提示')
        idx_foreign = html.index('🏛 国外研报')
        idx_foreign_detail = html.index('国外机构一周研报明细')
        idx_other_feed = html.index('全市场快讯与机构动态')
        idx_hk = html.index('香港券商新闻与数据')
        idx_freshness = html.index('数据新鲜度')
        self.assertLess(idx_risk, idx_foreign)
        self.assertLess(idx_foreign, idx_foreign_detail)
        self.assertLess(idx_foreign_detail, idx_other_feed)
        self.assertLess(idx_other_feed, idx_hk)
        self.assertLess(idx_hk, idx_freshness)

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
        self.assertLess(html.index('研报 AI 风险点提示'), html.index('🏛 国外研报'))
        self.assertLess(html.index('🏛 国外研报'), html.index('国外机构一周研报明细'))
        self.assertLess(html.index('国外机构一周研报明细'), html.index('全市场快讯与机构动态'))
        self.assertLess(html.index('全市场快讯与机构动态'), html.index('香港券商新闻与数据'))
        self.assertLess(html.index('香港券商新闻与数据'), html.index('数据新鲜度'))

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
        self.assertLess(html.index('研报 AI 风险点提示'), html.index('🏛 国外研报'))
        self.assertLess(html.index('🏛 国外研报'), html.index('国外机构一周研报明细'))
        self.assertLess(html.index('国外机构一周研报明细'), html.index('机构情绪汇总与补充说明'))
        self.assertLess(html.index('机构情绪汇总与补充说明'), html.index('数据新鲜度'))


if __name__ == '__main__':
    unittest.main()
