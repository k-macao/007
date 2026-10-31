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


if __name__ == '__main__':
    unittest.main()
