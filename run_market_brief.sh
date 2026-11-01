#!/usr/bin/env bash
# =============================================================================
# run_market_brief.sh —— 全球 49 大财经 RSS 情报源 · AI 解盘一键启动
#
# 作用：基于仓库内已集成的 49 个免费数据源（财经快讯 8 + 社媒热榜 10 +
# 港股/A股行情 3 + 美股行情 4 + 国外投行 12 + 港股券商 12），调用 DeepSeek
# AI 做地缘/政治/政策/财经/科技分类解盘，输出“发生了什么事、影响多少”
# 的说人话市场情报简报。
#
# 前提：可联网环境 + 已配置 DEEPSEEK_API_KEY（或 INFERENCE_* 接入任意
# OpenAI 兼容端点）。纯标准库，无需 pip install。
# =============================================================================
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "============================================================"
echo "  章鱼 AI.券商研报 007 · 全市场情报解盘（49 源）"
echo "============================================================"
echo ""

# ---------- 1. 环境检查 ----------
echo "[1/5] 环境自检 …"
python -m compileall -q . >/dev/null 2>&1 && echo "   ✅ Python 语法通过" || { echo "   ❌ 语法检查失败"; exit 1; }

if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -z "${INFERENCE_API_KEY:-}" ]; then
  echo "   ⚠️  未检测到 DEEPSEEK_API_KEY / INFERENCE_API_KEY"
  echo "      AI 解盘将自动降级为本地规则模板（不耗 API，可离线演示）。"
  echo "      如需 AI 解盘，请先执行：export DEEPSEEK_API_KEY=sk-xxx"
else
  echo "   ✅ 已检测到 AI Key，将使用 DeepSeek / OpenAI 兼容端点做 AI 解盘"
fi

# ---------- 2. 离线门禁 ----------
echo ""
echo "[2/5] 离线单元测试 …"
python -m unittest discover -s . -p "test_*.py" >/tmp/market_brief_ut.log 2>&1
UT_CODE=$?
if [ $UT_CODE -eq 0 ]; then
  echo "   ✅ 58 项离线单元测试全部通过"
else
  echo "   ⚠️  部分单元测试未通过（详见 /tmp/market_brief_ut.log），继续执行"
fi

# ---------- 3. 全市场情报简报（feedscan） ----------
echo ""
echo "[3/5] 采集 13 路全市场快讯 + 国外投行研报 + 港股券商新闻（24h 窗口）…"
python pushplus_deepseek.py \
  --template feedscan \
  --topic "全球市场早报" \
  --hours 24 \
  --no-chart \
  --dry-run \
  --no-state \
  --theme dos \
  > /tmp/market_brief_feedscan.md 2>/tmp/market_brief_feedscan.err

if [ $? -eq 0 ]; then
  echo "   ✅ 全市场快讯情报采集完成"
else
  echo "   ⚠️  采集部分源失败（详见 /tmp/market_brief_feedscan.err），已记数据缺口，不中断"
fi

# ---------- 4. 五大推动力速览（scan） ----------
echo ""
echo "[4/5] 生成“推动股价的 5 大力量”速览 …"
python pushplus_deepseek.py \
  --template scan \
  --topic "全球市场" \
  --dry-run \
  --no-state \
  --theme dos \
  > /tmp/market_brief_scan.md 2>/tmp/market_brief_scan.err

if [ $? -eq 0 ]; then
  echo "   ✅ 五大推动力速览生成完成"
else
  echo "   ⚠️  速览生成部分失败（详见 /tmp/market_brief_scan.err）"
fi

# ---------- 5. 汇总输出 ----------
echo ""
echo "============================================================"
echo "  生成结果（已保存到 /tmp，可直接阅读或推送）"
echo "============================================================"
echo ""
echo "  📄 全市场情报简报：/tmp/market_brief_feedscan.md"
echo "  📄 五大推动力速览： /tmp/market_brief_scan.md"
echo ""
echo "  查看简报：  cat /tmp/market_brief_feedscan.md"
echo "  查看速览：  cat /tmp/market_brief_scan.md"
echo ""
echo "  如需真实推送到微信，去掉 --dry-run 并设置 PUSHPLUS_TOKEN："
echo "    export PUSHPLUS_TOKEN=your_token"
echo "    python pushplus_deepseek.py --template feedscan --topic \"全球市场早报\" \\"
echo "        --hours 24 --channel pushplus --push"
echo ""
echo "  如需对单只股票做产业链传导与多空概率分析："
echo "    python pushplus_deepseek.py --template analysis --topic \"阿里巴巴\" \\"
echo "        --hk-code 09988 --hours 48 --dry-run"
echo ""
echo "============================================================"
