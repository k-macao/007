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
        self.assertIn('非四路并行', md)
        self.assertIn('本地金融词典', md)

    def test_failure_is_visible_and_cached(self):
        pack = pp.SentPack(hours=48, items={'news': [SimpleNamespace(title='新闻')]})
        with patch.object(hf_hub, 'score_texts', side_effect=RuntimeError('secret')):
            md = pp.render_financial_sentiment_push(pack)
        self.assertIn('主报告照常推送', md)
        self.assertNotIn('secret', md)
        self.assertEqual(md, pack.financial_sentiment_md)


if __name__ == '__main__':
    unittest.main()
