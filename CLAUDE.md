# battery-digest

英文电池行业每日 digest 静态站。抓 20+ 信源 → 筛选 → 写成 markdown → 生成静态站 →
部署到 Cloudflare Workers。同一套代码跑两个站：主站和 solid-state 变体站。

线上：`https://battery-digest.yubinxing.workers.dev`

## 运行

```bash
python scripts/run_daily.py        # 每日全流程：抓取 + 筛选 + 生成
python build.py                    # 只构建主站 → output/
python build.py --site solidstate  # 构建 solid-state 站 → output-solidstate/
npx wrangler deploy                # 部署主站（wrangler.jsonc）
```

每日流程由 GitHub Actions 跑（`.github/workflows/daily.yml`，每天 12:00 UTC）：两站各自
scrape → curate → build → `wrangler deploy`，最后把新 digest 提交回仓库。Cloudflare 本身只托管静态
文件，Worker 里 13:00 UTC 的 cron 只负责给订阅者发邮件。

`startup.bat` 是旧的本地跑法（任务计划 BatteryDigest，登录触发），2026-09-13 起已停用。不要和
Actions 同时开着：同一天会被 curate 两次，且本地 push 会因落后于远端而失败。

## 结构

| 路径 | 作用 |
|---|---|
| `scripts/scrape.py` | 按 `config/sources*.yaml` 抓信源 |
| `scripts/curate.py` | 筛选、排序、挑出当日 3 条 |
| `scripts/run_daily.py` | 串起 scrape → curate → build |
| `build.py` | Jinja2 + markdown + feedgen，生成 HTML 和 `feed.xml` |
| `config/sites/main.yaml`、`sites/solidstate.yaml` | 每站的标题、base_url、导航 |
| `config/sources.yaml`、`sources-solidstate.yaml` | 信源清单 |
| `content/digests/YYYY-MM-DD.md` | 每日一篇，是站点的唯一内容源 |
| `templates/`、`static/` | Jinja2 模板与静态资源 |
| `src/worker.js` | Cloudflare Worker，serve `output/` |

## 注意

- **加新站**：在 `config/sites/` 加一个 yaml，`build.py --site <名字>` 会自动找它，
  找不到才回落到根目录 `config.yaml`。同时要加对应的 `wrangler-<名字>.jsonc`。
- `output/` 和 `output-solidstate/` 是构建产物，每次 build 全量重写，别手改。
- 内容站的调性定在 `config.yaml` 的 tagline：3 条、5 分钟。加内容前先想清楚是否破坏这个约束。
