"""Radiko 录制：支持 time-shift（タイムフリー）和 live 实时直播两种模式。

URL 形态：
  time-shift: https://radiko.jp/#!/ts/QRR/20260511003000
  live:       https://radiko.jp/#!/live/QRR

共同流程：
  1. POST /v2/api/auth1   → 拿 X-Radiko-AuthToken + KeyLength + KeyOffset
  2. 算 partialkey = base64(authkey[offset:offset+length])
  3. POST /v2/api/auth2   → 验证地域（IP 必须在日本）

time-shift 拉流：按 15s 窗口 seek 扫描过去节目（一次性下载所有 chunks）。
live 实时拉流：按墙钟跑 N 分钟，每 5s 拉新 medialist，去重 append 新 chunks。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from loguru import logger

from radio.utils.ffmpeg import find_ffmpeg

# Radiko PC HTML5 播放器内置的 authkey（公开常数，可从开源项目反推）
_RADIKO_AUTHKEY = "bcd151073c03b352e1ef2fd66c32209da9ca0afa"

_AUTH1_URL = "https://radiko.jp/v2/api/auth1"
_AUTH2_URL = "https://radiko.jp/v2/api/auth2"

# 2026 起 Radiko time-shift playlist 的真实端点
_PLAYLIST_HOST_PRIMARY = "https://tf-f-rpaa-radiko.smartstream.ne.jp/tf/playlist.m3u8"
# 同一 CDN 的备用边缘
_PLAYLIST_HOST_FALLBACK = "https://tf-c-rpaa-radiko.smartstream.ne.jp/tf/playlist.m3u8"

# Radiko live（实时直播）playlist 端点 — 跟 streamlink 同款（simul-stream）
_LIVE_HOST_PRIMARY = "https://f-radiko.smartstream.ne.jp"
_LIVE_HOST_FALLBACK = "https://c-radiko.smartstream.ne.jp"

# 模仿浏览器的请求头（auth 时必填）
_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "ja,en;q=0.9",
    "x-radiko-user": "dummy_user",
    "x-radiko-app": "pc_html5",
    "x-radiko-app-version": "0.0.1",
    "x-radiko-device": "pc",
}


@dataclass(frozen=True)
class RadikoTimefreeSpec:
    """解析后的 Radiko time-shift 录制规格。"""

    station_id: str       # "QRR" / "TBS" / "LFR" ...
    ft: str               # "YYYYMMDDhhmmss" 开始
    to: str               # "YYYYMMDDhhmmss" 结束
    duration_minutes: int
    title_hint: str       # 用于产物文件名，如 "QRR_20260511_003000"

    @property
    def is_live(self) -> bool:
        return False


@dataclass(frozen=True)
class RadikoLiveSpec:
    """解析后的 Radiko 实时直播录制规格（没有 ft/to，只有时长）。"""

    station_id: str       # "QRR" / "TBS" / "LFR" ...
    duration_minutes: int
    title_hint: str       # 如 "QRR_LIVE_20260516_152230"

    @property
    def is_live(self) -> bool:
        return True


# 联合类型方便函数签名
RadikoSpec = RadikoTimefreeSpec | RadikoLiveSpec


@dataclass(frozen=True)
class RadikoAudio:
    """Radiko 录制完成后的产物 + 元信息。"""

    audio_path: Path
    title: str
    source_url: str
    station_id: str
    air_start_iso: str


def parse_radiko_url(url: str, duration_minutes: int) -> RadikoSpec:
    """从 Radiko URL 解析录制规格。支持 time-shift 和 live 两种形态。

    形态：
      time-shift: https://radiko.jp/#!/ts/QRR/20260511003000
      live:       https://radiko.jp/#!/live/QRR
    """
    # 优先尝试 time-shift（更具体的形态）
    ts_match = re.search(r"/ts/([A-Z0-9]+)/(\d{14})", url)
    if ts_match:
        station_id, ft = ts_match.group(1), ts_match.group(2)
        start_dt = datetime.strptime(ft, "%Y%m%d%H%M%S")
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        to = end_dt.strftime("%Y%m%d%H%M%S")
        return RadikoTimefreeSpec(
            station_id=station_id,
            ft=ft,
            to=to,
            duration_minutes=duration_minutes,
            title_hint=f"{station_id}_{ft[:8]}_{ft[8:]}",
        )

    # 再尝试 live
    live_match = re.search(r"/live/([A-Z0-9]+)", url)
    if live_match:
        station_id = live_match.group(1)
        now_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        return RadikoLiveSpec(
            station_id=station_id,
            duration_minutes=duration_minutes,
            title_hint=f"{station_id}_LIVE_{now_tag}",
        )

    raise ValueError(
        f"无法解析 Radiko URL：{url}\n"
        f"期望形态：\n"
        f"  time-shift: https://radiko.jp/#!/ts/STATION_ID/YYYYMMDDhhmmss\n"
        f"  live:       https://radiko.jp/#!/live/STATION_ID"
    )


def build_live_master_url(
    spec: RadikoLiveSpec,
    prefer_primary: bool = True,
    lsid: str | None = None,
) -> str:
    """构造 Radiko live（simul-stream）的 master playlist URL。

    Live 不像 time-shift 有 seek/ft/to；它是滚动 HLS（live edge）。
    lsid 全程同一个（关键！否则 chunk URL 不同导致去重失败）。
    """
    if lsid is None:
        lsid = hashlib.md5(str(random.random()).encode("utf-8")).hexdigest()
    params = {
        "station_id": spec.station_id,
        "l": 15,
        "lsid": lsid,
        "type": "b",
    }
    host = _LIVE_HOST_PRIMARY if prefer_primary else _LIVE_HOST_FALLBACK
    return (
        f"{host}/{spec.station_id}/_definst_/simul-stream.stream/playlist.m3u8"
        f"?{urlencode(params)}"
    )


def _chunk_dedup_key(url: str) -> str:
    """从 chunk URL 提取去重 key（仅 sequence number）。

    chunk URL 形如：
      https://.../media-ugjvhjx09_w1864818758_602533.aac?station_id=QRR&l=15&lsid=...&type=b
                                  ^^^^^^^^^^         ^^^^^^
                                  每次会换的 session token  ↑
                                                     真正稳定的 sequence #

    只取末尾的 sequence number 作为去重 key——这是 HLS 序号，全程递增唯一。
    """
    path = url.split("?", 1)[0]
    # 抓末尾的 _数字.aac 或 _数字.ts 部分
    m = re.search(r"_(\d+)\.(?:aac|ts|m4s|mp4)$", path)
    if m:
        return m.group(1)
    # 兜底：用整个 path
    return path


async def _authenticate(client: httpx.AsyncClient) -> str:
    """两步认证，返回 authtoken。auth2 失败说明 IP 不在日本。"""
    logger.info("Radiko auth1：请求 authtoken")
    r1 = await client.get(_AUTH1_URL, headers=_BASE_HEADERS, timeout=15.0)
    r1.raise_for_status()

    authtoken = r1.headers.get("x-radiko-authtoken") or r1.headers.get("X-Radiko-AuthToken")
    key_length = int(r1.headers.get("x-radiko-keylength") or r1.headers.get("X-Radiko-KeyLength") or "0")
    key_offset = int(r1.headers.get("x-radiko-keyoffset") or r1.headers.get("X-Radiko-KeyOffset") or "0")

    if not authtoken or not key_length:
        raise RuntimeError(f"auth1 响应缺字段：{dict(r1.headers)}")

    partial = _RADIKO_AUTHKEY[key_offset : key_offset + key_length].encode("utf-8")
    partialkey = base64.b64encode(partial).decode("utf-8")
    logger.debug(
        f"Radiko auth1 OK：authtoken={authtoken[:10]}…, key offset={key_offset}, length={key_length}"
    )

    logger.info("Radiko auth2：验证地域 + 激活 authtoken")
    r2 = await client.get(
        _AUTH2_URL,
        headers={
            **_BASE_HEADERS,
            "x-radiko-authtoken": authtoken,
            "x-radiko-partialkey": partialkey,
        },
        timeout=15.0,
    )
    if r2.status_code != 200:
        raise RuntimeError(
            f"auth2 失败 HTTP {r2.status_code}：{r2.text[:200]}\n"
            f"通常意味着 IP 不在日本（Radiko 全国版需付费エリアフリー）。"
        )
    area_info = r2.text.strip()
    logger.info(f"Radiko auth2 OK：area={area_info}")
    return authtoken


def _build_master_playlist_url(
    spec: RadikoTimefreeSpec,
    prefer_primary: bool = True,
    seek: str | None = None,
    preroll: int | None = None,
    window_seconds: int = 15,
) -> str:
    """构造 master playlist URL。

    注意：`l` 是播放器滑动窗口秒数，不是节目总时长；完整 time-free 需要用
    `seek` 每 15 秒递进请求。master 里的 medialist URL 带一次性 session，
    所以这里只构造 URL 字符串。
    """
    lsid = hashlib.md5(str(random.random()).encode("utf-8")).hexdigest()
    params = {
        "station_id": spec.station_id,
        "start_at": spec.ft,
        "ft": spec.ft,
        "end_at": spec.to,
        "to": spec.to,
        "l": window_seconds,
        "lsid": lsid,
        "type": "b",
    }
    if seek is not None:
        params["seek"] = seek
    if preroll is not None:
        params["preroll"] = preroll
    host = _PLAYLIST_HOST_PRIMARY if prefer_primary else _PLAYLIST_HOST_FALLBACK
    return f"{host}?{urlencode(params)}"


async def _download_hls_via_httpx(
    client: httpx.AsyncClient,
    master_url: str,
    authtoken: str,
    out_path: Path,
) -> None:
    """用 httpx 跟 streamlink 一样自己拉 HLS（master → medialist → chunks），
    然后用 ffmpeg 仅做 AAC ADTS → m4a 容器转换。

    为什么不让 ffmpeg 拉：ffmpeg 的 `-headers` 不作用于跟随的子 m3u8 / chunk
    请求，Radiko 的 medialist endpoint 要求每个请求都带 X-Radiko-AuthToken，
    所以 ffmpeg 一跟随就 404。
    """
    ffmpeg = find_ffmpeg()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {**_BASE_HEADERS, "X-Radiko-AuthToken": authtoken}

    # 1. master playlist
    logger.info("HLS step 1：GET master playlist")
    r = await client.get(master_url, headers=headers, timeout=30.0)
    r.raise_for_status()
    if not r.text.strip().startswith("#EXTM3U"):
        raise RuntimeError(f"master playlist 不是 m3u8：{r.text[:200]}")
    medialist_url = next(
        (l.strip() for l in r.text.splitlines() if l.strip() and not l.startswith("#")),
        None,
    )
    if not medialist_url:
        raise RuntimeError(f"master 中没有 variant URL：\n{r.text}")
    logger.info(f"HLS step 1 OK → medialist: {medialist_url[:100]}…")

    # 2. medialist：拿到所有 chunk .aac URL
    logger.info("HLS step 2：GET medialist（含全部 chunk URI）")
    r2 = await client.get(medialist_url, headers=headers, timeout=60.0)
    r2.raise_for_status()
    if not r2.text.strip().startswith("#EXTM3U"):
        raise RuntimeError(f"medialist 不是 m3u8：{r2.text[:200]}")
    chunk_urls: list[str] = [
        l.strip()
        for l in r2.text.splitlines()
        if l.strip() and not l.startswith("#")
    ]
    if not chunk_urls:
        raise RuntimeError(f"medialist 中无 chunk URL：\n{r2.text[:500]}")
    logger.info(f"HLS step 2 OK → {len(chunk_urls)} 个 AAC chunks 待下载")

    # 3. 并发下 chunks（保持 chunk 顺序——按 list index 索引）
    raw_aac_path = out_path.with_suffix(".aac")
    sem = asyncio.Semaphore(8)

    async def _fetch_chunk(idx: int, url: str) -> tuple[int, bytes]:
        async with sem:
            for attempt in range(3):
                try:
                    cr = await client.get(url, headers=headers, timeout=30.0)
                    cr.raise_for_status()
                    return idx, cr.content
                except Exception as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1.0 * (attempt + 1))
                    logger.warning(
                        f"chunk {idx} 第 {attempt + 1} 次失败：{e!r}，重试"
                    )
            raise RuntimeError("unreachable")

    results = await asyncio.gather(
        *[_fetch_chunk(i, u) for i, u in enumerate(chunk_urls)]
    )
    results.sort(key=lambda x: x[0])
    total_bytes = sum(len(b) for _, b in results)
    with raw_aac_path.open("wb") as f:
        for _, data in results:
            f.write(data)
    logger.info(
        f"HLS step 3 OK → 全部 {len(results)} chunks 已下载（{total_bytes / 1024 / 1024:.1f} MB AAC）"
    )

    # 4. ffmpeg 仅做容器转换 AAC ADTS → m4a
    cmd = [
        ffmpeg, "-y",
        "-i", str(raw_aac_path),
        "-acodec", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-loglevel", "warning",
        str(out_path),
    ]
    logger.info("HLS step 4：ffmpeg 转封装 AAC → m4a")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 转封装失败 (exit {proc.returncode})：\n"
            f"{stderr.decode('utf-8', errors='replace')[:1000]}"
        )
    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise RuntimeError(f"ffmpeg 没产生可用文件：{out_path}")

    # 清理 .aac 中间文件
    raw_aac_path.unlink(missing_ok=True)
    size_mb = out_path.stat().st_size / 1024 / 1024
    logger.info(f"Radiko 录制完成：{out_path.name}（{size_mb:.1f} MB）")


async def record_radiko_live(
    url: str,
    output_dir: Path,
    duration_minutes: int,
    title: str | None = None,
) -> RadikoAudio:
    """录制 Radiko 实时直播（live / simul-stream）。

    Radiko live HLS 端点不被反爬挡，纯 httpx 就能拉。
    流程：
      1. auth1 + auth2 拿 token
      2. 墙钟 N 分钟内，每 ~4s 拉 master → medialist → 新 chunks
      3. chunk 去重 append 到 raw.aac
      4. ffmpeg 转 m4a 容器
    """
    spec = parse_radiko_url(url, duration_minutes)
    if not isinstance(spec, RadikoLiveSpec):
        raise ValueError(
            f"record_radiko_live 需要 /live/STATION URL；当前：{url}"
        )

    ffmpeg = find_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_m4a = output_dir / f"{spec.title_hint}.m4a"
    raw_aac = output_dir / f"{spec.title_hint}.aac"
    # 防止之前失败 run 留下的 .aac 残留污染本次结果
    if raw_aac.exists():
        raw_aac.unlink()
    air_start_iso = datetime.now().isoformat(timespec="seconds")

    duration_s = duration_minutes * 60
    poll_interval = 4.0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        authtoken = await _authenticate(client)
        headers = {**_BASE_HEADERS, "X-Radiko-AuthToken": authtoken}

        # 录制全程用同一 lsid（防止 chunk URL 因 query 参数变化导致去重失败）
        lsid = hashlib.md5(str(random.random()).encode("utf-8")).hexdigest()

        seen_chunks: set[str] = set()  # key = path（去掉 query）
        total_bytes = 0
        poll_iter = 0
        loop = asyncio.get_event_loop()
        started = loop.time()
        deadline = started + duration_s

        logger.info(
            f"Radiko live 录制开始：station={spec.station_id} 时长 {duration_minutes} min"
        )

        with raw_aac.open("wb") as aac_file:
            while True:
                now_t = loop.time()
                if now_t >= deadline:
                    break

                master_url = build_live_master_url(spec, lsid=lsid)
                try:
                    r1 = await client.get(master_url, headers=headers, timeout=15.0)
                    r1.raise_for_status()
                except Exception as e:
                    logger.warning(
                        f"primary master 失败：{e!r}，尝试 fallback host"
                    )
                    master_url = build_live_master_url(
                        spec, prefer_primary=False, lsid=lsid
                    )
                    r1 = await client.get(master_url, headers=headers, timeout=15.0)
                    r1.raise_for_status()

                medialist_url = next(
                    (
                        ln.strip()
                        for ln in r1.text.splitlines()
                        if ln.strip() and not ln.startswith("#")
                    ),
                    None,
                )
                if not medialist_url:
                    logger.warning("master 中无 medialist URL，本轮 skip")
                    await asyncio.sleep(poll_interval)
                    continue

                r2 = await client.get(medialist_url, headers=headers, timeout=15.0)
                r2.raise_for_status()
                chunk_urls = [
                    ln.strip()
                    for ln in r2.text.splitlines()
                    if ln.strip() and not ln.startswith("#")
                ]
                # 去重 key 用 path（不含 query），防 lsid 之外其他参数变动
                new_pending: list[str] = []
                for u in chunk_urls:
                    key = _chunk_dedup_key(u)
                    if key not in seen_chunks:
                        seen_chunks.add(key)
                        new_pending.append(u)

                for u in new_pending:
                    for attempt in range(3):
                        try:
                            cr = await client.get(u, headers=headers, timeout=30.0)
                            cr.raise_for_status()
                            aac_file.write(cr.content)
                            total_bytes += len(cr.content)
                            break
                        except Exception as e:
                            if attempt == 2:
                                logger.error(f"chunk 永久失败：{u[:80]}…")
                            else:
                                await asyncio.sleep(0.5 * (attempt + 1))

                poll_iter += 1
                elapsed = loop.time() - started
                remaining = deadline - loop.time()
                if poll_iter % 6 == 0 or new_pending:
                    logger.info(
                        f"live poll #{poll_iter}：+{len(new_pending)} chunks，"
                        f"累计 {len(seen_chunks)} / {total_bytes / 1024 / 1024:.1f} MB，"
                        f"已录 {elapsed:.0f}s / 剩 {max(0, remaining):.0f}s"
                    )
                if remaining <= 0:
                    break
                await asyncio.sleep(min(poll_interval, remaining))

    if total_bytes < 1024:
        raise RuntimeError(
            f"live 录制结束但没数据（{total_bytes} 字节）"
        )

    # AAC → m4a
    logger.info("ffmpeg 转封装 AAC → m4a")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y",
        "-i", str(raw_aac),
        "-acodec", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-loglevel", "warning",
        str(out_m4a),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 转封装失败 (exit {proc.returncode})：\n"
            f"{stderr.decode('utf-8', errors='replace')[:600]}"
        )
    raw_aac.unlink(missing_ok=True)
    size_mb = out_m4a.stat().st_size / 1024 / 1024
    logger.info(
        f"Radiko live 录制完成：{out_m4a.name}（{size_mb:.1f} MB，"
        f"{len(seen_chunks)} chunks）"
    )

    display = title or f"Radiko {spec.station_id} LIVE {air_start_iso}"
    return RadikoAudio(
        audio_path=out_m4a,
        title=display,
        source_url=url,
        station_id=spec.station_id,
        air_start_iso=air_start_iso,
    )


async def record_radiko_timefree(
    url: str,
    output_dir: Path,
    duration_minutes: int,
    title: str | None = None,
) -> RadikoAudio:
    """端到端：解析 URL → 认证 → 拉 playlist → ffmpeg 录制 → 返回产物。

    Args:
        url: Radiko time-shift URL，如 https://radiko.jp/#!/ts/QRR/20260511003000
        output_dir: 输出目录
        duration_minutes: 节目时长（分钟），用于计算 `to` 时间戳
        title: 节目展示标题；不传则用 station_id + datetime 生成
    """
    spec = parse_radiko_url(url, duration_minutes)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{spec.title_hint}.m4a"
    air_start_iso = datetime.strptime(spec.ft, "%Y%m%d%H%M%S").isoformat()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        authtoken = await _authenticate(client)

        # primary 端点（2026 起官方 time-shift CDN）
        playlist_url = _build_master_playlist_url(spec)
        try:
            await _download_hls_via_httpx(client, playlist_url, authtoken, out_path)
        except Exception as e:
            logger.warning(f"primary 端点录制失败，尝试 fallback：{e!r}")
            playlist_url = _build_master_playlist_url(spec, prefer_primary=False)
            await _download_hls_via_httpx(client, playlist_url, authtoken, out_path)

    display = title or f"Radiko {spec.station_id} {air_start_iso}"
    return RadikoAudio(
        audio_path=out_path,
        title=display,
        source_url=url,
        station_id=spec.station_id,
        air_start_iso=air_start_iso,
    )
