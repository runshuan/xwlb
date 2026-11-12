#!/usr/bin/env python3
"""
每日新闻联播文字版：从央视网抓取当天《新闻联播》图文稿，整理后输出为全文 RSS 源，
或发布到 WordPress。

只用 Python 标准库（3.8+），服务器上拷贝本文件 + .env 即可运行。

用法示例：
    python3 xwlb.py --rss                         # 抓今天，存入 data/，并重新生成 docs/feed.xml
    python3 xwlb.py --rss --from yesterday        # 抓昨天和今天（已抓过的自动跳过）
    python3 xwlb.py --dry-run                     # 抓今天，只生成 output/YYYYMMDD.html 预览
    python3 xwlb.py                               # 抓今天并发布到 WordPress
    python3 xwlb.py --date yesterday              # 抓昨天（补跑用）
    python3 xwlb.py --from 20260401 --to 20260416 # 批量补历史

退出码：0 成功（或已存在被跳过）；1 出错；2 央视网当天内容还没出全。
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

BEIJING = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent

# 央视网《新闻联播》栏目在 api.cntv.cn 上的 ID
XWLB_COLUMN_ID = "TOPC1451528971114112"
LIST_API = "https://api.cntv.cn/NewVideo/getVideoListByColumn"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
REQUEST_INTERVAL = 0.8  # 每次请求央视网之间的间隔（秒）

# 正文开头的“央视网消息（新闻联播）：”
PREFIX_RE = re.compile(r"^\s*央视网消息\s*[（(]\s*新闻联播\s*[）)]\s*[:：]?\s*")
EXIT_NOT_READY = 2


class NotReady(Exception):
    """央视网当天的内容还没有发布完整。"""


# ---------------------------------------------------------------- 配置


def load_env(path: Path) -> None:
    """读取 KEY=VALUE 格式的 .env，已存在的环境变量不覆盖。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def log(msg: str) -> None:
    now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP


def http_get(url: str, retries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"请求失败 {url}: {last_error}")


# ---------------------------------------------------------------- 央视网抓取


def fetch_item_list(date: str) -> list[dict]:
    """返回当天 19:00 那期的分条新闻列表（按播出顺序）。"""
    params = {
        "id": XWLB_COLUMN_ID,
        "n": 100,
        "sort": "asc",
        "p": 1,
        "d": date,
        "mode": 1,  # 1 = 分条新闻；0 = 整期节目
        "serviceId": "tvcctv",
    }
    url = f"{LIST_API}?{urllib.parse.urlencode(params)}"
    data = json.loads(http_get(url))
    items = (data.get("data") or {}).get("list") or []
    result = []
    for it in items:
        title = (it.get("title") or "").strip()
        if title.startswith("《新闻联播》"):  # 整期节目条目，跳过
            continue
        result.append(
            {
                "title": clean_title(title),
                "url": it.get("url", ""),
                "brief": it.get("brief") or "",
                "focus_date": int(it.get("focus_date") or 0),
            }
        )
    # 接口按 sort=asc 已是播出顺序；保险起见再按上线时间排一次（稳定排序）
    result.sort(key=lambda x: x["focus_date"])
    return result


def clean_title(title: str) -> str:
    title = re.sub(r"^\s*\[视频\]\s*", "", title)
    return compact_cjk(title)


def compact_cjk(text: str) -> str:
    """去掉紧挨中文字符的空白（和原站一致），英文单词之间的空格保留。"""
    text = text.replace("　", " ").strip()
    return re.sub(r"(?<=[^\x00-\x7f])\s+|\s+(?=[^\x00-\x7f])", "", text)


class ContentAreaParser(HTMLParser):
    """解析新闻页 <div id="content_area">，输出段落列表。

    每个段落是 [(文本, 是否加粗), ...] 的片段列表；<br> 视为分段。
    """

    VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}
    BLOCK = {"p", "div", "section", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0  # >0 表示位于 content_area 内部
        self.done = False  # 只解析页面里第一个 content_area
        self.strong = 0
        self.skip = 0  # script/style 内部
        self.current: list[tuple[str, bool]] = []
        self.paragraphs: list[list[tuple[str, bool]]] = []

    def _flush(self) -> None:
        if any(t.strip() for t, _ in self.current):
            self.paragraphs.append(self.current)
        self.current = []

    def handle_starttag(self, tag, attrs):
        if self.depth == 0:
            if not self.done and dict(attrs).get("id") == "content_area":
                self.depth = 1
            return
        if tag in self.VOID:
            if tag == "br":
                self._flush()
            return
        self.depth += 1
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("strong", "b"):
            self.strong += 1
        elif tag in self.BLOCK:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        if self.depth and tag == "br":
            self._flush()

    def handle_endtag(self, tag):
        if self.depth == 0 or tag in self.VOID:
            return
        self.depth -= 1
        if self.depth == 0:  # 离开 content_area
            self._flush()
            self.done = True
            return
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("strong", "b"):
            self.strong = max(0, self.strong - 1)
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.depth and not self.skip and data:
            self.current.append((data.replace("　", " "), self.strong > 0))


def trim_runs(runs: list[tuple[str, bool]], n: int) -> list[tuple[str, bool]]:
    """从片段列表开头去掉 n 个字符。"""
    out = []
    for text, bold in runs:
        if n >= len(text):
            n -= len(text)
            continue
        out.append((text[n:], bold))
        n = 0
    return out


def plain(runs: list[tuple[str, bool]]) -> str:
    return "".join(t for t, _ in runs)


def is_heading(runs: list[tuple[str, bool]]) -> bool:
    return all(bold for t, bold in runs if t.strip())


def parse_article(page_html: str) -> list[list[tuple[str, bool]]]:
    parser = ContentAreaParser()
    parser.feed(page_html)
    parser.close()
    paragraphs = parser.paragraphs
    # 去掉开头的“央视网消息（新闻联播）：”
    if paragraphs:
        m = PREFIX_RE.match(plain(paragraphs[0]))
        if m:
            paragraphs[0] = trim_runs(paragraphs[0], m.end())
    cleaned = []
    for runs in paragraphs:
        # 去掉段首空白
        while runs and not runs[0][0].strip():
            runs = runs[1:]
        if runs:
            runs = [(runs[0][0].lstrip(), runs[0][1])] + runs[1:]
        if plain(runs).strip():
            cleaned.append(runs)
    return cleaned


def render_paragraph(runs: list[tuple[str, bool]]) -> str:
    if is_heading(runs):
        return f"<p><strong>{html.escape(compact_cjk(plain(runs)), quote=False)}</strong></p>"
    parts = []
    bold_open = False
    for text, bold in runs:
        if bold and not bold_open:
            parts.append("<strong>")
            bold_open = True
        elif not bold and bold_open:
            parts.append("</strong>")
            bold_open = False
        parts.append(html.escape(text, quote=False))
    if bold_open:
        parts.append("</strong>")
    return f"<p>{''.join(parts).strip()}</p>"


def scrape_day(date: str, min_items: int = 5) -> list[dict]:
    """抓取某天全部分条新闻及正文。内容不全时抛出 NotReady。"""
    items = fetch_item_list(date)
    log(f"{date}: 接口返回 {len(items)} 条新闻")
    if len(items) < min_items:
        raise NotReady(f"{date} 只有 {len(items)} 条新闻（少于 {min_items}），央视网可能还没更新完")

    empty = 0
    for i, item in enumerate(items, 1):
        time.sleep(REQUEST_INTERVAL)
        paragraphs = parse_article(http_get(item["url"]))
        if not paragraphs and item["brief"].strip():
            # 极少数条目没有图文稿，用接口里的简介兜底
            paragraphs = [[(line.strip(), False)] for line in item["brief"].splitlines() if line.strip()]
        if not paragraphs:
            empty += 1
        item["paragraphs"] = paragraphs
        log(f"  [{i:02d}/{len(items)}] {item['title']}（{len(paragraphs)} 段）")

    if empty > len(items) * 0.3:
        raise NotReady(f"{date} 有 {empty}/{len(items)} 条没有正文，央视网可能还没更新完")
    return items


# ---------------------------------------------------------------- 生成文章


def post_title(date: str) -> str:
    return f"{date[:4]}年{date[4:6]}月{date[6:]}日新闻联播文字版"


def build_content(date: str, items: list[dict], source_note: bool) -> str:
    lines = [f"<p><strong>{date}今日新闻联播主要内容：</strong></p>", "<ul>"]
    lines += [f"<li>{html.escape(it['title'], quote=False)}</li>" for it in items]
    lines.append("</ul>")
    lines.append('<p><span style="color:red;">以下为详细的文字版全文：</span></p>')
    for it in items:
        lines.append(f"<p><strong>{html.escape(it['title'], quote=False)}</strong></p>")
        paragraphs = it["paragraphs"]
        # 正文第一段若与标题相同（少数条目会这样），跳过避免重复
        if paragraphs and compact_cjk(plain(paragraphs[0])) == it["title"]:
            paragraphs = paragraphs[1:]
        lines += [render_paragraph(p) for p in paragraphs]
    if source_note:
        lines.append("<p>来源：央视网《新闻联播》</p>")
    return "\n".join(lines)


def write_preview(date: str, title: str, content: str) -> Path:
    out_dir = SCRIPT_DIR / "output"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{date}.html"
    path.write_text(
        "<!doctype html>\n<html lang=\"zh-Hans\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<style>body{max-width:720px;margin:40px auto;padding:0 16px;font:16px/1.8 sans-serif}</style>"
        f"</head><body>\n<h1>{html.escape(title)}</h1>\n{content}\n</body></html>\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- RSS


def env_path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name) or default)
    return p if p.is_absolute() else SCRIPT_DIR / p


def episode_time(date: str) -> datetime:
    """每期在 RSS 里的发布时间：节目当天 23:45（北京时间），与原站一致。"""
    d = datetime.strptime(date, "%Y%m%d")
    return d.replace(hour=23, minute=45, tzinfo=BEIJING)


def save_day(date: str, items: list[dict], content: str) -> Path:
    data_dir = env_path("DATA_DIR", "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{date}.json"
    record = {
        "date": date,
        "title": post_title(date),
        "headlines": [it["title"] for it in items],
        "content": content,
        "fetched_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


PAGE_CSS = """
:root{--bg:#fff;--fg:#222;--muted:#777;--line:#e5e5e5;--link:#b00}
@media (prefers-color-scheme:dark){:root{--bg:#151515;--fg:#e8e8e8;--muted:#999;--line:#333;--link:#f66}}
body{background:var(--bg);color:var(--fg);max-width:720px;margin:32px auto;padding:0 16px;
  font:17px/1.85 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--link)} code{word-break:break-all}
h1{font-size:1.5em;line-height:1.4}
.muted,nav{color:var(--muted)}
nav{display:flex;justify-content:space-between;gap:12px;margin:24px 0;padding:12px 0;
  border-top:1px solid var(--line);font-size:.92em}
ul.list{list-style:none;padding:0}
ul.list li{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--line)}
ul.list li span{color:var(--muted);white-space:nowrap}
h2{font-size:1.1em;margin-top:28px}
"""


def render_page(title: str, body: str, feed_title: str) -> str:
    return (
        '<!doctype html>\n<html lang="zh-Hans"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f'<link rel="alternate" type="application/rss+xml" title="{html.escape(feed_title)}" href="feed.xml">\n'
        f"<style>{PAGE_CSS}</style></head><body>\n{body}\n</body></html>\n"
    )


def write_if_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def page_name(date: str) -> str:
    return f"{date}.html"


def build_feed() -> Path:
    """读取 data/，在 RSS_DIR 下生成 feed.xml、每天一页的纯文字网页、首页和往期列表。"""
    data_dir = env_path("DATA_DIR", "data")
    out_dir = env_path("RSS_DIR", "docs")
    limit = int(os.environ.get("RSS_ITEMS") or 30)
    site = (os.environ.get("RSS_LINK") or "").strip()
    if site and not site.endswith("/"):
        site += "/"
    if not site:
        log("提示：没有设置 RSS_LINK，RSS 里的文章链接将是相对路径，部分阅读器可能打不开")
    feed_title = os.environ.get("RSS_TITLE") or "每日新闻联播文字版"
    feed_desc = os.environ.get("RSS_DESCRIPTION") or "每天的《新闻联播》全文文字稿，内容来自央视网。"

    files = sorted(data_dir.glob("[0-9]" * 8 + ".json"), reverse=True)
    records = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    out_dir.mkdir(parents=True, exist_ok=True)

    # 每天一个纯文字页面。所有日期都生成，旧链接长期有效
    for i, r in enumerate(records):
        newer = records[i - 1] if i > 0 else None
        older = records[i + 1] if i + 1 < len(records) else None
        nav = (
            "<nav>"
            + (f'<a href="{page_name(older["date"])}">← 上一期</a>' if older else "<span></span>")
            + '<a href="index.html">首页</a>'
            + (f'<a href="{page_name(newer["date"])}">下一期 →</a>' if newer else "<span></span>")
            + "</nav>"
        )
        body = f"<h1>{html.escape(r['title'])}</h1>\n{r['content']}\n{nav}"
        write_if_changed(out_dir / page_name(r["date"]), render_page(r["title"], body, feed_title))

    # RSS：最近 N 天
    recent = records[:limit]
    # lastBuildDate 取最新一期的时间，内容不变时文件也不变（方便 git 只在有新内容时提交）
    last_build = format_datetime(episode_time(recent[0]["date"])) if recent else format_datetime(
        datetime(2000, 1, 1, tzinfo=BEIJING))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{xml_escape(feed_title)}</title>",
        f"<link>{xml_escape(site or './')}</link>",
    ]
    if site:
        out.append(f'<atom:link href="{xml_escape(site)}feed.xml" rel="self" type="application/rss+xml"/>')
    out += [
        f"<description>{xml_escape(feed_desc)}</description>",
        "<language>zh-CN</language>",
        f"<lastBuildDate>{last_build}</lastBuildDate>",
        "<ttl>120</ttl>",
    ]
    for r in recent:
        summary = "今日主要内容：" + "；".join(r["headlines"])
        out += [
            "<item>",
            f"<title>{xml_escape(r['title'])}</title>",
            f"<link>{xml_escape(site + page_name(r['date']))}</link>",
            f'<guid isPermaLink="false">xwlb-{r["date"]}</guid>',
            f"<pubDate>{format_datetime(episode_time(r['date']))}</pubDate>",
            f"<description>{xml_escape(summary)}</description>",
            f"<content:encoded>{cdata(r['content'])}</content:encoded>",
            "</item>",
        ]
    out += ["</channel>", "</rss>", ""]
    feed_path = out_dir / "feed.xml"
    write_if_changed(feed_path, "\n".join(out))

    def row(r: dict) -> str:
        return (f'<li><a href="{page_name(r["date"])}">{html.escape(r["title"])}</a>'
                f'<span>{len(r["headlines"])} 条</span></li>')

    # 首页：最近 N 天
    feed_url = site + "feed.xml" if site else "feed.xml"
    index_body = (
        f"<h1>{html.escape(feed_title)}</h1>\n"
        f'<p class="muted">{html.escape(feed_desc)}</p>\n'
        f'<p>RSS 地址：<a href="feed.xml"><code>{html.escape(feed_url)}</code></a></p>\n'
        '<ul class="list">\n' + "\n".join(row(r) for r in recent) + "\n</ul>\n"
        + (f'<p><a href="archive.html">全部往期（{len(records)} 期）→</a></p>' if len(records) > len(recent) else "")
    )
    write_if_changed(out_dir / "index.html", render_page(feed_title, index_body, feed_title))

    # 往期：按月分组列出全部
    months: dict[str, list[dict]] = {}
    for r in records:
        months.setdefault(f"{r['date'][:4]}年{r['date'][4:6]}月", []).append(r)
    archive_body = '<h1>全部往期</h1>\n<p><a href="index.html">← 返回首页</a></p>\n' + "\n".join(
        f'<h2>{m}</h2>\n<ul class="list">\n' + "\n".join(row(r) for r in rs) + "\n</ul>"
        for m, rs in months.items()
    )
    write_if_changed(out_dir / "archive.html", render_page(f"全部往期 - {feed_title}", archive_body, feed_title))
    return feed_path


def process_day_rss(date: str, args) -> str:
    """抓取一天并存入 data/。返回 'saved' / 'skipped'。"""
    path = env_path("DATA_DIR", "data") / f"{date}.json"
    if path.exists() and not args.update:
        log(f"{date}: 已抓取过，跳过（要重新抓请加 --update）")
        return "skipped"
    items = scrape_day(date, args.min_items)
    content = build_content(date, items, env_flag("SOURCE_NOTE", True))
    save_day(date, items, content)
    log(f"{date}: 已保存 {path}")
    return "saved"


# ---------------------------------------------------------------- WordPress


class WordPressError(Exception):
    pass


class WordPress:
    """通过 REST API + 应用程序密码（Application Password）发文章。"""

    def __init__(self, site_url: str, user: str, app_password: str, rest_prefix: str = "/wp-json"):
        self.site = site_url.rstrip("/")
        self.rest_prefix = rest_prefix
        token = base64.b64encode(f"{user}:{app_password.replace(' ', '')}".encode()).decode()
        self.auth = f"Basic {token}"

    def _url(self, route: str, params: dict | None = None) -> str:
        if self.rest_prefix == "query":  # 未开启固定链接的站点
            q = {"rest_route": route}
            q.update(params or {})
            return f"{self.site}/?{urllib.parse.urlencode(q)}"
        url = f"{self.site}{self.rest_prefix}{route}"
        return f"{url}?{urllib.parse.urlencode(params)}" if params else url

    def request(self, method: str, route: str, params: dict | None = None, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url(route, params), data=data, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("User-Agent", "xwlb-publisher/1.0")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise WordPressError(f"{method} {route} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise WordPressError(f"{method} {route} 连接失败: {e.reason}") from None

    def check_auth(self) -> str:
        me = self.request("GET", "/wp/v2/users/me", {"context": "edit"})
        return me.get("username") or me.get("name") or "?"

    def term_id(self, taxonomy: str, name: str, slug: str | None = None) -> int:
        """按名称找分类/标签，没有就创建。taxonomy: categories 或 tags。"""
        found = self.request("GET", f"/wp/v2/{taxonomy}", {"search": name, "per_page": 100})
        for term in found:
            if html.unescape(term.get("name", "")) == name:
                return term["id"]
        body = {"name": name}
        if slug:
            body["slug"] = slug
        created = self.request("POST", f"/wp/v2/{taxonomy}", body=body)
        log(f"已创建{'分类' if taxonomy == 'categories' else '标签'}：{name}")
        return created["id"]

    def find_post(self, title: str) -> dict | None:
        posts = self.request(
            "GET",
            "/wp/v2/posts",
            {
                "search": title,
                "status": "publish,future,draft,pending,private",
                "context": "edit",
                "per_page": 20,
            },
        )
        for post in posts:
            raw = (post.get("title") or {}).get("raw") or html.unescape((post.get("title") or {}).get("rendered", ""))
            if raw.strip() == title:
                return post
        return None

    def save_post(self, body: dict, post_id: int | None = None) -> dict:
        route = f"/wp/v2/posts/{post_id}" if post_id else "/wp/v2/posts"
        return self.request("POST", route, body=body)


def wordpress_from_env() -> WordPress:
    missing = [k for k in ("WP_URL", "WP_USER", "WP_APP_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"缺少配置：{', '.join(missing)}（写在 .env 里，参考 .env.example）")
    return WordPress(
        os.environ["WP_URL"],
        os.environ["WP_USER"],
        os.environ["WP_APP_PASSWORD"],
        os.environ.get("WP_REST_PREFIX", "/wp-json"),
    )


# ---------------------------------------------------------------- 主流程


def parse_date(value: str) -> str:
    today = datetime.now(BEIJING).date()
    if value == "today":
        d = today
    elif value == "yesterday":
        d = today - timedelta(days=1)
    else:
        d = datetime.strptime(value, "%Y%m%d").date()
    return d.strftime("%Y%m%d")


def date_range(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y%m%d").date()
    d1 = datetime.strptime(end, "%Y%m%d").date()
    if d1 < d0:
        d0, d1 = d1, d0
    return [(d0 + timedelta(days=i)).strftime("%Y%m%d") for i in range((d1 - d0).days + 1)]


def process_day(date: str, args, wp: WordPress | None, terms: dict) -> str:
    """处理一天。返回 'published' / 'updated' / 'skipped' / 'dry-run'。"""
    title = post_title(date)

    existing = None
    if wp is not None:
        existing = wp.find_post(title)
        if existing and not args.update:
            log(f"{date}: 已存在文章 #{existing['id']}，跳过（要覆盖请加 --update）")
            return "skipped"

    items = scrape_day(date, args.min_items)
    content = build_content(date, items, env_flag("SOURCE_NOTE", True))

    if wp is None:
        path = write_preview(date, title, content)
        log(f"{date}: 预览已生成 {path}")
        return "dry-run"

    body = {
        "title": title,
        "content": content,
        "status": args.status,
        "categories": terms["categories"],
        "tags": terms["tags"],
    }
    if args.backdate and not existing:
        # 发布时间回填为节目当天 23:45（WordPress 站点时区）
        body["date"] = f"{date[:4]}-{date[4:6]}-{date[6:]}T23:45:00"
    post = wp.save_post(body, existing["id"] if existing else None)
    action = "updated" if existing else "published"
    log(f"{date}: {'已更新' if existing else '已发布'} #{post['id']} {post.get('link', '')}")
    return action


def main() -> int:
    load_env(SCRIPT_DIR / ".env")

    ap = argparse.ArgumentParser(description="抓取央视网《新闻联播》图文稿，输出 RSS 或发布到 WordPress")
    ap.add_argument("--rss", action="store_true",
                    help="RSS 模式：抓取结果存入 data/，并重新生成 docs/feed.xml（不连 WordPress）")
    ap.add_argument("--date", default="today", help="YYYYMMDD / today / yesterday（默认 today，北京时间）")
    ap.add_argument("--from", dest="date_from", help="批量起始日期 YYYYMMDD / yesterday")
    ap.add_argument("--to", dest="date_to", help="批量结束日期 YYYYMMDD（默认今天）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取并生成 output/YYYYMMDD.html，不发布")
    ap.add_argument("--update", action="store_true", help="已存在时重新抓取并覆盖")
    ap.add_argument("--status", default=os.environ.get("WP_POST_STATUS", "publish"),
                    choices=["publish", "draft", "pending", "private"], help="文章状态（默认 publish）")
    ap.add_argument("--backdate", action="store_true",
                    help="发布时间写成节目当天 23:45（批量模式默认开启）")
    ap.add_argument("--min-items", type=int, default=5, help="少于这么多条新闻视为未更新完（默认 5）")
    args = ap.parse_args()

    if args.date_from:
        dates = date_range(parse_date(args.date_from), parse_date(args.date_to or "today"))
        args.backdate = True
    else:
        dates = [parse_date(args.date)]

    wp = None
    terms: dict = {}
    if not args.rss and not args.dry_run:
        wp = wordpress_from_env()
        try:
            log(f"WordPress 登录用户：{wp.check_auth()}")
            category = os.environ.get("WP_CATEGORY", "新闻联播文字版")
            tags = [t.strip() for t in os.environ.get("WP_TAGS", "新闻联播,联播文稿").split(",") if t.strip()]
            terms = {
                "categories": [wp.term_id("categories", category, os.environ.get("WP_CATEGORY_SLUG") or None)],
                "tags": [wp.term_id("tags", t) for t in tags],
            }
        except WordPressError as e:
            log(f"WordPress 连接失败：{e}")
            return 1

    results: dict[str, int] = {}
    exit_code = 0
    for date in dates:
        try:
            if args.rss:
                status = process_day_rss(date, args)
            else:
                status = process_day(date, args, wp, terms)
        except NotReady as e:
            log(f"{date}: 内容未就绪 —— {e}")
            status = "not-ready"
            exit_code = exit_code or EXIT_NOT_READY
        except (RuntimeError, WordPressError, ValueError) as e:
            log(f"{date}: 失败 —— {e}")
            status = "failed"
            exit_code = 1
        results[status] = results.get(status, 0) + 1
        if len(dates) > 1:
            time.sleep(REQUEST_INTERVAL)

    if len(dates) > 1:
        log("汇总：" + "，".join(f"{k} {v}" for k, v in results.items()))

    if args.rss:
        try:
            log(f"RSS 已生成：{build_feed()}")
        except (OSError, ValueError, KeyError) as e:
            log(f"RSS 生成失败：{e}")
            return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
