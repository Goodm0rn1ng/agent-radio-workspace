"""B 站账号监测（只读公开数据）：拉某 UP 主的稿件 + 播放量并聚合。

输入可为 `space.bilibili.com/<UID>` 链接或纯 UID。复用 BilibiliClient（WBI 签名 +
首页 cookie 预热）。只读公开 space 接口，不登录、不写任何东西。
"""
from __future__ import annotations

import re
import statistics
import time

from clip.bilibili_source import BilibiliClient

# space/wbi/arc/search 现需 WebGL 指纹反爬参数，否则 -352/412 风控。静态占位值即可通过。
_DM_ANTICRAWL = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HTEUgKEFwcGxlLCBBcHBsZSBNMSwgT3BlbkdMIDQuMSk",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


def resolve_mid(account: str) -> int:
    """从 URL / UID 字符串解析出 mid。"""
    s = str(account or "").strip()
    m = re.search(r"space\.bilibili\.com/(\d+)", s)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"\D*(\d{2,})\D*", s)
    if m:
        return int(m.group(1))
    raise ValueError(f"无法从「{account}」解析出 B 站 UID（示例：space.bilibili.com/123 或 123）")


def fetch_account(account: str, limit: int = 30) -> dict:
    """返回 {account: 聚合指标, videos: [近期稿件+播放量]}。"""
    mid = resolve_mid(account)
    now = time.time()
    with BilibiliClient() as cli:
        # 近期稿件（按发布时间倒序）——监测「最新产出表现」。space/arc/search 风控随机，
        # 重试若干次基本都能过（见 CHANGELOG 的研究结论）。
        params = {"mid": mid, "pn": 1, "ps": min(max(limit, 1), 50),
                  "order": "pubdate", **_DM_ANTICRAWL}
        data, last_err = None, None
        for attempt in range(5):
            try:
                data = cli._wbi_get("/x/space/wbi/arc/search", params)
                break
            except Exception as e:  # noqa: BLE001 — 风控/412 重试
                last_err = e
                time.sleep(0.8 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"B 站 space 接口风控，重试 5 次仍失败：{last_err}")
        vlist = (data.get("list") or {}).get("vlist") or []
        total = int((data.get("page") or {}).get("count") or len(vlist))
        owner = ""
        videos = []
        for v in vlist:
            owner = owner or v.get("author", "")
            created = int(v.get("created") or 0)
            play = int(v.get("play") or 0)
            age_d = max((now - created) / 86400.0, 0.01) if created else None
            videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "play": play,
                "comment": int(v.get("comment") or 0),
                "danmaku": int(v.get("video_review") or 0),
                "created": created,
                "length": v.get("length", ""),
                "play_per_day": round(play / age_d) if age_d else None,
                "url": f"https://www.bilibili.com/video/{v.get('bvid', '')}",
            })
        follower = None
        try:
            st = cli._get("/x/relation/stat", {"vmid": mid})
            follower = int(st.get("follower") or 0)
        except Exception:  # noqa: BLE001 — 粉丝数拿不到不影响主指标
            pass

    plays = [v["play"] for v in videos]
    latest = max((v["created"] for v in videos if v["created"]), default=0)
    agg = {
        "mid": mid,
        "owner": owner,
        "follower": follower,
        "total_videos": total,
        "sampled": len(videos),
        "play_sum": sum(plays),
        "play_avg": round(sum(plays) / len(plays)) if plays else 0,
        "play_median": round(statistics.median(plays)) if plays else 0,
        "play_max": max(plays) if plays else 0,
        "latest_upload": latest,
        "space_url": f"https://space.bilibili.com/{mid}",
    }
    return {"account": agg, "videos": videos}
