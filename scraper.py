# -*- coding: utf-8 -*-
"""
二重螺旋配装情报站 - 定时抓取脚本
=================================
用法:
    python scraper.py                # 抓取全部来源并写出 data.json
    python scraper.py --output data.json

设计说明:
    - 仅用 Python 标准库, GitHub Actions / 本地计划任务均可直接运行
    - 每个来源独立容错: 单个来源失败不影响其他来源, 失败信息写入 source_status
    - B站搜索接口近年可能要求 wbi 签名 / 触发风控, 失败时自动降级跳过
    - RSS 来源可接入 RSSHub 等实例, 订阅 B站UP主 / NGA 版块 / 游戏媒体
    - 抓取结果按关键词分类为 角色配装(builds) / 阵容(teams) / 攻略流(feeds)
    - builds / teams 的结构化条目适合人工在 data.json 中维护精选;
      脚本会保留手工条目(MANUAL 标记), 只自动合并新抓到的条目
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ---------- 配置 ----------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 搜索关键词: 每个关键词都会向各来源发起一次搜索
KEYWORDS = [
    "二重螺旋 配装",
    "二重螺旋 武器推荐",
    "二重螺旋 阵容",
]

# 角色名表: 用于把抓到的攻略标题自动归类到角色
CHARACTERS = [
    "丽贝卡", "贝蕾妮卡", "松露与榛子", "海尔法", "芙罗拉",
    "达芙妮", "西比尔", "琳恩", "玛格丽特", "塔比瑟", "外之町",
]

# 阵容类标题的识别词
TEAM_HINTS = ["阵容", "配队", "组队", "队伍", "T0", "梯度", "排行"]

# RSS 来源 (可替换为自己的 RSSHub 实例路由)
RSS_SOURCES = [
    # {"name": "某UP主", "url": "https://rsshub.example/bilibili/user/video/UID"},
]

CST = timezone(timedelta(hours=8))


# ---------- 工具 ----------

def timeout_seconds():
    return 15


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def parse_pubdate(raw):
    """尽力解析 RSS 日期, 失败返回 None"""
    if not raw:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def classify(title):
    """按标题归类: 返回 (kind, character_or_None)"""
    hit_char = next((c for c in CHARACTERS if c in title), None)
    if any(h in title for h in TEAM_HINTS):
        return "teams", hit_char
    if hit_char or any(k in title for k in ("配装", "武器", "魔之楔", "模组", "词条")):
        return "builds", hit_char
    return "feeds", hit_char


# ---------- 来源采集器 ----------

def collect_bilibili():
    """B站视频搜索。注意: 该接口可能要求 wbi 签名或被风控, 失败抛异常由上层容错。"""
    items = []
    for kw in KEYWORDS:
        url = ("https://api.bilibili.com/x/web-interface/search/type?"
               + urllib.parse.urlencode({
                   "search_type": "video",
                   "keyword": kw,
                   "order": "pubdate",
                   "page": 1,
               }))
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": "https://search.bilibili.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout_seconds()) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if payload.get("code") != 0:
            raise RuntimeError("B站接口返回 code=%s" % payload.get("code"))
        for v in (payload.get("data") or {}).get("result") or []:
            title = re.sub(r"<[^>]+>", "", v.get("title", "")).strip()
            if not title:
                continue
            items.append({
                "title": title,
                "url": "https://www.bilibili.com/video/" + v.get("bvid", ""),
                "published_at": datetime.fromtimestamp(
                    v.get("pubdate", 0), CST).isoformat(timespec="seconds"),
                "source_name": "B站",
                "type": "视频",
            })
        time.sleep(1)  # 限速, 降低风控概率
    return items


def collect_rss(name, url):
    """通用 RSS / Atom 采集"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout_seconds()) as resp:
        root = ET.fromstring(resp.read())
    items = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    if root.tag.endswith("feed"):  # Atom
        for e in root.findall("atom:entry", ns)[:20]:
            title = (e.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = e.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            pub = parse_pubdate(e.findtext("atom:published", namespaces=ns)
                                or e.findtext("atom:updated", namespaces=ns))
            if title:
                items.append({"title": title, "url": link, "published_at": pub,
                              "source_name": name, "type": "文章"})
    else:  # RSS 2.0
        for e in root.iter("item"):
            title = (e.findtext("title") or "").strip()
            link = (e.findtext("link") or "").strip()
            pub = parse_pubdate(e.findtext("pubDate"))
            if title:
                items.append({"title": title, "url": link, "published_at": pub,
                              "source_name": name, "type": "文章"})
    return items[:20]


# ---------- 主流程 ----------

def run_collectors():
    """运行全部采集器, 返回 (raw_items, source_status)"""
    raw, status = [], []

    try:
        got = collect_bilibili()
        raw.extend(got)
        status.append({"name": "B站搜索", "ok": True, "count": len(got)})
    except Exception as exc:  # noqa: BLE001 - 单源失败不阻塞
        status.append({"name": "B站搜索", "ok": False, "error": str(exc)})

    for src in RSS_SOURCES:
        try:
            got = collect_rss(src["name"], src["url"])
            raw.extend(got)
            status.append({"name": src["name"], "ok": True, "count": len(got)})
        except Exception as exc:  # noqa: BLE001
            status.append({"name": src["name"], "ok": False, "error": str(exc)})

    return raw, status


def merge(raw_items, existing):
    """去重 + 分类合并。保留 data.json 中带 manual 标记的手工精选条目。"""
    seen_urls = set()
    feeds, auto_builds, auto_teams = [], [], []

    for it in raw_items:
        u = it.get("url") or ""
        if u and u in seen_urls:
            continue
        seen_urls.add(u)
        kind, _char = classify(it["title"])
        if kind == "feeds":
            feeds.append(it)
        else:
            # 抓到的配装/阵容线索先全部进攻略流并打上分类标签,
            # 结构化 builds/teams 条目建议人工确认后补全字段再移入
            it = dict(it)
            it["type"] = "配装线索" if kind == "builds" else "阵容线索"
            feeds.append(it)

    feeds.sort(key=lambda x: x.get("published_at") or "", reverse=True)

    builds = [b for b in existing.get("builds", []) if b.get("manual")]
    teams = [t for t in existing.get("teams", []) if t.get("manual")]
    return builds, teams, feeds[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="data.json")
    args = ap.parse_args()

    existing = {}
    try:
        with open(args.output, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        pass

    raw, status = run_collectors()
    builds, teams, feeds = merge(raw, existing)

    data = {
        "updated_at": now_iso(),
        "source_status": status,
        "builds": builds,
        "teams": teams,
        "feeds": feeds,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    ok = sum(1 for s in status if s["ok"])
    print("[%s] 来源 %d/%d 正常, feeds=%d builds=%d teams=%d -> %s"
          % (now_iso(), ok, len(status), len(feeds), len(builds), len(teams), args.output))
    return 0 if ok or builds else 1


if __name__ == "__main__":
    sys.exit(main())
