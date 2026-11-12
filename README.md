# 每日新闻联播文字版：全文 RSS 源

`xwlb.py` 每天从央视网抓取当天《新闻联播》的图文稿，整理成一篇“YYYY年MM月DD日新闻联播文字版”，有两种用法：

- **RSS 模式**（`--rss`）：生成全文 RSS 文件 `docs/feed.xml`，外加每天一页的文字网页，不需要网站后台和数据库。
- **WordPress 模式**：把文章发布到 WordPress，排版和原站 mrxwlb.com 一样。

**只有文字**：不含视频、图片，也不链接到央视的视频页面。

只用 Python 标准库，Python 3.8 及以上，不需要 `pip install`。

> RSSHub 已有新闻联播路由（`/cctv/tv/lm/xwlb`），但只有每天的标题列表和视频链接，没有正文。本项目输出的是纯文字全文。

## 数据从哪来

| 步骤 | 地址 |
|---|---|
| 当天新闻列表（JSON） | `https://api.cntv.cn/NewVideo/getVideoListByColumn?id=TOPC1451528971114112&mode=1&sort=asc&d=YYYYMMDD&n=100&p=1&serviceId=tvcctv` |
| 每条新闻正文 | 列表里每条的 `url`（`tv.cctv.com/...shtml`）页面中 `<div id="content_area">` |

**处理规则**（和原站一致）：

- 标题去掉 `[视频]` 前缀，去掉中文之间的空格。
- 正文去掉开头的“央视网消息（新闻联播）：”。
- 只保留段落和加粗，丢掉图片和脚本。
- 文末注明“来源：央视网《新闻联播》”，纯文字，不带链接。用 `SOURCE_NOTE=false` 可以关掉。

## RSS 源

### RSS 里每天一条

| 字段 | 内容 |
|---|---|
| `title` | 2026年04月16日新闻联播文字版 |
| `link` | 自己生成的当天文字页，例如 `https://你的用户名.github.io/xwlb/20260416.html` |
| `pubDate` | 节目当天 23:45（北京时间） |
| `description` | 当天各条新闻标题（摘要） |
| `content:encoded` | 全文：要点清单 + 每条新闻的标题和正文 |

默认保留最近 30 天。用 `RSS_ITEMS` 可以改。

### 生成方式

```bash
python3 xwlb.py --rss                         # 抓今天，生成 docs/feed.xml
python3 xwlb.py --rss --from yesterday        # 抓昨天和今天（已抓过的跳过）
python3 xwlb.py --rss --from 20260101         # 补抓 2026-01-01 至今
```

**文件说明**：

- `data/YYYYMMDD.json`：每天的抓取结果。抓过的日期不会重复抓，加 `--update` 强制重抓。
- `docs/feed.xml`：RSS 文件。
- `docs/YYYYMMDD.html`：每天一页的纯文字全文，带上一期/下一期导航。所有日期都会生成，旧链接一直有效。
- `docs/index.html`：首页，列出最近各期和 RSS 地址。
- `docs/archive.html`：全部往期，按月分组。

`docs/` 下的文件只在有新内容时才变化，方便用 git 管理。

`RSS_LINK` 要设成 `docs/` 发布后的网址，RSS 里的文章链接才是完整地址。用 GitHub Actions 时会自动设置。

### 托管方式一：GitHub Actions + GitHub Pages（免费，不需要服务器）

仓库里已经带了 `.github/workflows/rss.yml`，每天自动抓取并发布到 GitHub Pages。

1. 在 GitHub 新建一个仓库，例如 `xwlb`，把本目录的文件推上去。不要提交 `.env`，`.gitignore` 已经排除了它。
2. 仓库 → **Settings → Pages → Build and deployment → Source** 选 **GitHub Actions**。
3. 仓库 → **Actions** → “更新新闻联播 RSS” → **Run workflow**，手动跑一次：
   - 想先补历史，就在 `from` 里填起始日期，如 `20260101`。
4. 跑完后打开 `https://你的用户名.github.io/xwlb/`，RSS 地址是 `https://你的用户名.github.io/xwlb/feed.xml`。

之后每天自动运行两次：

- **北京时间 23:45**：抓当天。
- **北京时间 08:00**：补抓昨天，防止漏掉。

**说明**：

- **运行时间不准**：GitHub 的定时任务常会延迟几分钟到半小时。
- **抓取结果会提交回仓库**（`data/`、`docs/`），所以历史数据不会丢。每天都有提交，仓库保持活跃，定时任务也不会因 60 天无活动被停用。
- **用自己的域名**：在仓库 **Settings → Secrets and variables → Actions → Variables** 里设 `RSS_LINK`（例如 `https://rss.你的域名.com/`）。`RSS_TITLE`、`RSS_DESCRIPTION` 也可以在这里设。

### 托管方式二：自己的服务器

1. 把 `xwlb.py` 放到服务器上，复制 `.env.example` 为 `.env`，填写 `RSS_LINK`。
2. 用 nginx 等把 `docs/` 目录作为静态网站发布。
3. 用 `crontab -e` 加定时任务：

   服务器时区是北京时间时：

   ```cron
   45 23 * * * cd /path/to/xwlb && /usr/bin/python3 xwlb.py --rss --from yesterday >> xwlb.log 2>&1
   0 8 * * *  cd /path/to/xwlb && /usr/bin/python3 xwlb.py --rss --from yesterday >> xwlb.log 2>&1
   ```

   服务器时区是 UTC 时，把时间改成 `45 15` 和 `0 0`。

`today` / `yesterday` 在脚本里始终按北京时间计算，和服务器时区无关。

## WordPress 模式

如果还想要一个和原站一样的网站，可以把文章发到 WordPress。WordPress 自带 RSS（`/feed/`）。要输出全文，需要在后台 → 设置 → 阅读里，把“Feed 中的每篇文章，显示”设为“全文”。

### 准备 WordPress

1. 安装 WordPress，主题可以用官方的 **Twenty Twelve**（原站用的就是它）。
2. 后台 → 设置 → 常规 → 时区，选 **上海**。
3. 后台 → 设置 → 固定链接，选“日期和名称型”。
4. 后台 → 用户 → 个人资料 → **应用程序密码**，新建一个，填进 `.env` 的 `WP_APP_PASSWORD`。

> 应用程序密码要求站点是 **HTTPS**。没有 HTTPS 时，这一栏不会出现。

### 常用命令

| 命令 | 作用 |
|---|---|
| `python3 xwlb.py --dry-run` | 只生成 `output/YYYYMMDD.html` 预览，不发布 |
| `python3 xwlb.py` | 抓今天并发布 |
| `python3 xwlb.py --date yesterday` | 抓昨天 |
| `python3 xwlb.py --from 20250101 --to 20250131` | 批量补历史，发布时间回填为节目当天 23:45 |
| `python3 xwlb.py --status draft` | 存为草稿 |
| `python3 xwlb.py --date 20260416 --update` | 覆盖已存在的文章 |

同标题文章已存在就跳过，重复运行是安全的。

### 常见问题

- **HTTP 401**：
  - 检查用户名和应用程序密码。
  - Apache + PHP-FPM/CGI 的主机可能会吞掉 `Authorization` 头。在站点根目录的 `.htaccess` 里加一行：

    ```apache
    SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1
    ```
- **HTTP 404 `rest_no_route`**：在 `.env` 里设 `WP_REST_PREFIX=query`。
- **HTTP 403**：安全插件可能禁用了 REST API，在插件里放行。

## 退出码

- `0`：成功，或已存在被跳过
- `1`：出错
- `2`：央视网当天内容还没出全，晚点再跑

## 注意

- **版权**：正文版权属于央视网，建议保留文末来源链接（`SOURCE_NOTE=true`）。
- **请求频率**：每次请求央视网之间间隔 0.8 秒。补历史时不要并发跑多个进程。
