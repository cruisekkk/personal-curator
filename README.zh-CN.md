[English](README.md) | 简体中文

# personal-curator

一个个人用的小工作台：把一堆新闻订阅源，压成每天一页、每周一页。

机械的部分交给 Python（采集、存储、渲染）；需要判断的部分交给一份写成文字的编辑手册（skill），由 [Claude Code](https://claude.com/claude-code) 照着执行（哪些算同一件事、什么重要、哪里该存疑）。两边只通过一个普通的 JSON 文件交接，所以任何一边都可以单独替换。

## 设计：四个阶段

```
Collect  ──►  Interpret  ──►  Keep  ──►  Present
RSS/Atom      editorial       SQLite +    static HTML
              skill (LLM)     text + JSON (Jinja2)
```

| 阶段 | 做什么 | 在哪里 |
|---|---|---|
| **Collect（采集）** | 抓取 RSS/Atom 订阅源，把每条内容转成统一的 `RawItem`，按 `(source, external_id)` 去重。 | `src/curator/ingest/`、`curator-ingest` |
| **Interpret（解读）** | 把条目聚成事件；用稳定的 `cluster_id` 让同一个故事跨天接续；在阅读预算内（默认 35 分钟）分成 P0/P1/P2/Archive；写简报：数据点、论证，以及值得保留的怀疑。 | `skill/SKILL.md` |
| **Keep（留存）** | 条目进 SQLite，正文存成纯文本文件，报告存成 JSON。报告 JSON 是唯一的事实来源，站点随时可以从它重建。 | `src/curator/store.py`、`$CURATOR_HOME` |
| **Present（呈现）** | 把 JSON 渲染成静态站点：日报页、周报页（带跨天故事的泳道图）和目录页。没有服务器，也没有前端框架。 | `src/curator/render.py`、`curator-render` |

解读的一方从不写 HTML，渲染器从不调用模型。这条边界就是设计的重点：报告写坏了，改 JSON 就能修；页面想换样子，也不需要让模型重跑一遍。

## 快速开始：渲染自带的示例

需要 Python 3.12 及以上。

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/curator-render --digests-dir examples/digests
open examples/digests/site/index.html        # macOS；其他系统直接用浏览器打开这个文件
```

`examples/digests/json/` 里有三份日报（2026-03-02 至 2026-03-04）和一份周报（`2026-W10`）。全部是合成数据：公司、数字和链接都是编的。打开周报页可以看到泳道图：有三个故事横跨多天，因为它们的事件共用同一个 `cluster_id`。

运行测试和代码检查：

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

## 用在你自己的订阅源上

数据放在 `$CURATOR_HOME`（默认 `~/.curator`）：

```
curator.db                     sources + items
raw/YYYY-MM-DD/<id>.txt        每条内容的正文
feeds.txt                      每行一个订阅源 URL（由 skill 读取）
digests/json/                  报告 JSON：YYYY-MM-DD.json、YYYY-Www.json
digests/site/                  渲染出的站点（派生产物，可以随时删除）
digests/state.json             水位：{"last_window_to": "..."}
```

1. 采集：`.venv/bin/curator-ingest --feed https://example.com/feed.xml --since-last-fetch`（多个订阅源就重复写 `--feed`）。
2. 解读：先安装 skill，再在 Claude Code 里让它出报告。
   ```bash
   mkdir -p ~/.claude/skills/curator-digest
   cp skill/SKILL.md ~/.claude/skills/curator-digest/SKILL.md
   ```
   在本目录下启动的 Claude Code 会话里：`/curator-digest` 出日报，`/curator-digest --weekly` 出周综述。
3. 呈现：`.venv/bin/curator-render`（skill 的最后一步会自动执行它）。

### 一段话说清报告 JSON

日报包含 `date`、`run_id`、`window`、`stats`、`events[]`，以及可选的 `insights[]`。每个事件有 `cluster_id`、`action`（`new` 或 `update`，后者要写 `what_changed`）、`priority`、`tag`、`read_budget_min` 和 `display`。`display` 只有三个取值：

- `expanded`：完整简报，默认展开，计入阅读预算
- `collapsed`：迷你简报，放在折叠卡片里
- `line`：只列来源链接，放在页面底部

`display` 缺失或取值不认识并不会报错：渲染器会按 `priority` 推断（Archive → `line`，P0 → `expanded`，其余 → `collapsed`），并打印一条警告。完整的字段表见 `skill/SKILL.md`。

## 定时运行是怎么工作的

`scripts/` 里是在 macOS 上用 launchd 无人值守运行 skill 的包装脚本。之所以需要它们，是因为在笔记本上“每天早上跑一次”会以好几种悄无声息的方式失败：

- **到点时电脑在睡觉。** 固定时刻的任务在合盖时会被直接跳过。所以任务改成每两小时轮询一次、登录时也触发一次；当天的报告已经生成时，`daily_digest_cron.sh` 会立刻退出（幂等守卫）。
- **一次运行永远不结束。** 曾经有一次运行挂了 69 天。现在每次模型调用都放在独立的进程组里，并设了硬超时。
- **两个实例同时跑，或者死掉的实例还占着锁。** 用 `mkdir` 做锁，锁里记录 PID；持有者已经不在的锁会被自动回收。
- **登录过期。** 后台运行使用存在 macOS 钥匙串里的长期 token（服务名 `curator-claude-oauth-token`），正式任务之前先做一次探活。探活判断的是“有没有一行恰好是 `OK`”，因为 CLI 可能在回答之前先打印几行警告。
- **瞬时错误和真正的错误要分开。** 网络和过载类错误带退避重试；认证错误立刻通知你；其余问题持续 12 小时以上才通知，并且同一类问题 12 小时内最多通知一次。
- **正常退出，却什么都没产出。** 成功的标准是“水位推进了”，而不是“退出码为 0”。skill 只有在所有步骤都成功之后才推进水位。

配置步骤：

```bash
# 1. 把长期 token 存进钥匙串（需要本人操作，只做一次）
bash scripts/setup_cron_auth.sh

# 2. 填好 plist 里的两个占位符并加载
sed -e "s|/path/to/curator|$PWD|g" -e "s|/path/to/home|$HOME|g" \
  scripts/launchd/com.example.curator.daily-digest.plist \
  > ~/Library/LaunchAgents/com.example.curator.daily-digest.plist
launchctl load -w ~/Library/LaunchAgents/com.example.curator.daily-digest.plist
```

plist 里写的是 `/path/to/curator`（本仓库的路径）和 `/path/to/home`（你的主目录），因为 launchd 不会展开 `~` 和 `$HOME`。周综述的做法：复制这份 plist，改掉 label，把脚本换成 `weekly_digest_cron.sh`，`StartInterval` 设为 `10800`；脚本自己只会在周日真正执行。

包装脚本会读取下面这些环境变量，全部可选：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `CURATOR_HOME` | `~/.curator` | 数据目录 |
| `CURATOR_REPO` | `scripts/` 的上一级目录 | `.venv/bin/curator-*` 所在的仓库 |
| `CLAUDE_BIN` | `PATH` 里的 `claude`，找不到则用 `~/.local/bin/claude` | Claude Code 命令行 |
| `CURATOR_MODEL` | `opus` | 出报告用的模型 |
| `CURATOR_WEEK` | 当前 ISO 周 | 让 `weekly_digest_cron.sh` 生成指定的某一周 |

日志写到 `~/Library/Logs/curator-daily-digest.log`。

## 有意没有放进来的东西

这个仓库是从一个更大的私人项目里抽出来的，只留下了值得分享的部分。

- **没有针对特定网站的采集器。** 只带通用的 RSS/Atom；为某一个网站专门写的采集器不在范围内。
- **没有向量、向量检索和问答。** 早期版本有过；后来发现每天的阅读需要的是编辑式的聚类和排序，而不是检索。
- **没有行情数据，也没有任何与交易相关的内容。** 没有走势图，没有股票代码。
- **没有月报和季报，没有同步到笔记工具，没有 Web 服务器。** 产出就是静态文件。
- **没有真实数据。** 示例是合成的，仓库里不包含任何真实报告。
- **没有 Linux 下的定时方案。** 包装脚本只适用于 macOS（launchd、钥匙串、BSD `stat`）。Python 部分在哪里都能跑。

## 目录结构

```
src/curator/ingest/     RawItem、IngestSource、RSS 采集器
src/curator/store.py    SQLite + 正文文本文件
src/curator/render.py   报告 JSON -> 静态站点
src/curator/templates/  Jinja2 模板和 style.css
skill/SKILL.md          编辑手册（skill）
scripts/                无人值守运行的包装脚本和 launchd plist
examples/digests/json/  合成的示例报告
tests/                  pytest，不访问网络
```

## 许可证

MIT，见 [LICENSE](LICENSE)。
