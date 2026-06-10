"""真实 B 站爬虫（共用）。

策略：以「分区排行 ranking/v2」为主（无需 WBI、字段已含 stat/desc/pubdate/duration），
再对动量筛选后的 topN 稿件做 WBI 签名的 tags / 热评增强（best-effort，失败不阻断）。

只读公开榜单；匿名即可跑大多数请求，可选 SESSDATA cookie 提升稳定性。
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

from clip.config import clip_config

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_API = "https://api.bilibili.com"

# WBI mixin-key 重排表（B 站 web 端固定常量）。
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


@dataclass
class TrendItem:
    bvid: str
    aid: int
    title: str
    desc: str
    pubdate: int                       # unix ts
    duration: int                      # seconds
    view: int
    like: int
    coin: int
    danmaku: int
    reply: int
    partition: str = ""
    tname: str = ""
    owner: str = ""
    tags: list[str] = field(default_factory=list)
    top_comments: list[str] = field(default_factory=list)

    def hours_since(self, now_ts: float) -> float:
        return max((now_ts - self.pubdate) / 3600.0, 0.01)


class BilibiliClient:
    def __init__(self):
        cookies = {}
        if clip_config.bilibili_sessdata:
            cookies["SESSDATA"] = clip_config.bilibili_sessdata
        self._http = httpx.Client(
            headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"},
            cookies=cookies,
            timeout=15.0,
            follow_redirects=True,
        )
        self._wbi_keys: tuple[str, str] | None = None
        self._bootstrap()

    def _bootstrap(self) -> None:
        """访问首页种下 buvid3 / b_nut cookie，否则榜单/签名接口会被风控拒绝(-352)。"""
        try:
            self._http.get("https://www.bilibili.com/")
        except Exception as e:  # noqa: BLE001 — 没种到 cookie 时后续请求自然报错
            print(f"  [warn] B 站首页 cookie 预热失败: {e}")

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 低层请求 ----
    def _get(self, path: str, params: dict | None = None) -> dict:
        time.sleep(clip_config.bilibili_request_sleep)
        r = self._http.get(_API + path, params=params or {})
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"B 站 API {path} 返回 code={data.get('code')} {data.get('message')}")
        return data.get("data", {})

    # ---- WBI 签名 ----
    def _mixin_key(self) -> str:
        if self._wbi_keys is None:
            nav = self._nav()
            img = nav["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
            sub = nav["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
            self._wbi_keys = (img, sub)
        orig = "".join(self._wbi_keys)
        return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]

    def _nav(self) -> dict:
        # nav 在未登录时 code=-101 但 data.wbi_img 仍可用，故单独处理。
        time.sleep(clip_config.bilibili_request_sleep)
        r = self._http.get(_API + "/x/web-interface/nav")
        r.raise_for_status()
        return r.json().get("data", {})

    def _wbi_get(self, path: str, params: dict) -> dict:
        params = dict(params)
        params["wts"] = int(time.time())
        clean = {
            k: re.sub(r"[!'()*]", "", str(v))
            for k, v in sorted(params.items())
        }
        query = urllib.parse.urlencode(clean)
        params["w_rid"] = hashlib.md5((query + self._mixin_key()).encode()).hexdigest()
        return self._get(path, params)

    # ---- 业务 ----
    def ranking(self, rid: int, partition_name: str, limit: int) -> list[TrendItem]:
        """分区排行 ranking/v2 → TrendItem 列表（字段已含 stat/desc/pubdate）。需 WBI 签名。"""
        data = self._wbi_get("/x/web-interface/ranking/v2", {"rid": rid, "type": "all"})
        items = []
        for v in (data.get("list") or [])[:limit]:
            stat = v.get("stat", {})
            items.append(TrendItem(
                bvid=v.get("bvid", ""),
                aid=int(v.get("aid") or v.get("id") or 0),
                title=v.get("title", ""),
                desc=v.get("desc", "") or "",
                pubdate=int(v.get("pubdate") or 0),
                duration=int(v.get("duration") or 0),
                view=int(stat.get("view") or 0),
                like=int(stat.get("like") or 0),
                coin=int(stat.get("coin") or 0),
                danmaku=int(stat.get("danmaku") or 0),
                reply=int(stat.get("reply") or 0),
                partition=partition_name,
                tname=v.get("tname", ""),
                owner=(v.get("owner") or {}).get("name", ""),
            ))
        return items

    def enrich(self, item: TrendItem, n_comments: int = 5) -> None:
        """给单条稿件补 tags + 热评（WBI 签名；失败仅告警不抛）。"""
        try:
            tags = self._get("/x/tag/archive/tags", {"bvid": item.bvid})
            item.tags = [t.get("tag_name", "") for t in (tags or []) if t.get("tag_name")]
        except Exception as e:  # noqa: BLE001 — 增强失败不阻断主流程
            print(f"  [warn] tags 拉取失败 {item.bvid}: {e}")
        try:
            rep = self._wbi_get(
                "/x/v2/reply/wbi/main",
                {"oid": item.aid, "type": 1, "mode": 3, "ps": 20},
            )
            replies = (rep.get("replies") or [])[:n_comments]
            item.top_comments = [
                r.get("content", {}).get("message", "") for r in replies
            ]
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 热评拉取失败 {item.bvid}: {e}")


    def search(self, keyword: str, limit: int, name: str = "", max_pages: int = 1) -> list[TrendItem]:
        """按关键词搜索视频（WBI 签名）。用于「虚拟主播/bangdream」这类无排行榜分区。
        order=pubdate 取近期稿件，再由动量排序surface涨得快的。"""
        items = []
        seen: set[str] = set()
        for page in range(1, max(1, max_pages) + 1):
            data = self._wbi_get("/x/web-interface/wbi/search/type", {
                "search_type": "video", "keyword": keyword, "order": "pubdate", "page": str(page),
            })
            for v in (data.get("result") or []):
                if v.get("type") and v.get("type") != "video":
                    continue
                bvid = v.get("bvid", "")
                if not bvid or bvid in seen:
                    continue
                seen.add(bvid)
                items.append(TrendItem(
                    bvid=bvid,
                    aid=int(v.get("aid") or v.get("id") or 0),
                    title=_strip_html(v.get("title", "")),
                    desc=v.get("description", "") or "",
                    pubdate=int(v.get("pubdate") or 0),
                    duration=_dur_to_sec(v.get("duration", "")),
                    view=int(v.get("play") or 0),
                    like=int(v.get("like") or 0),
                    coin=0,
                    danmaku=int(v.get("video_review") or v.get("danmaku") or 0),
                    reply=int(v.get("review") or 0),
                    partition=name or f"搜索:{keyword}",
                    tname=v.get("typename", ""),
                    owner=v.get("author", ""),
                ))
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        return items


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def _dur_to_sec(d: str) -> int:
    d = str(d or "").strip()
    if d.isdigit():
        return int(d)
    parts = d.split(":")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else 0


def fetch_trends(partitions: str | None = None, keywords: list[str] | None = None,
                 per_keyword: int = 30) -> list[TrendItem]:
    """抓取热点候选并合并去重。

    partitions: 逗号分隔的分区名/rid（排行榜）。None 用配置默认。
    keywords: 关键词搜索（用于无排行榜的「虚拟主播/bangdream」分区）。"""
    out: list[TrendItem] = []
    seen: set[str] = set()

    def add(items, label):
        n = 0
        for it in items:
            if it.bvid and it.bvid not in seen:
                seen.add(it.bvid)
                out.append(it)
                n += 1
        print(f"  {label}: {len(items)} 条")

    rids = (clip_config.partition_rids() if partitions is None
            else _parse_partitions(partitions))
    with BilibiliClient() as cli:
        for name, rid in rids:
            try:
                add(cli.ranking(rid, name, clip_config.bilibili_top_per_partition), f"分区 {name}(rid={rid})")
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] 分区 {name}(rid={rid}) 排行拉取失败: {e}")
        for kw in (keywords or []):
            try:
                add(cli.search(kw, per_keyword, max_pages=clip_config.trends_search_pages), f"搜索「{kw}」")
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] 搜索「{kw}」失败: {e}")
    return out


def _parse_partitions(partitions: str) -> list[tuple[str, int]]:
    from clip.config import PARTITION_RID
    out = []
    for raw in partitions.split(","):
        n = raw.strip().lower()
        if not n:
            continue
        if n.isdigit():
            out.append((f"rid{n}", int(n)))
        elif n in PARTITION_RID:
            out.append((n, PARTITION_RID[n]))
    return out
