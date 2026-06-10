"""Radiko time-shift 录制（Playwright 版本，绕过反爬）。

为什么用 Playwright：
  Radiko 2026 反爬把 master playlist 给所有人，但 medialist 只对官方 web
  播放器响应。httpx / curl / streamlink / radigo 等"纯 HTTP 客户端"
  拿 master 后访问 medialist 都立即 404。

解法：
  开真浏览器，让它正常加载页面完成 auth；能连接用户自己的 Chrome CDP 时，
  后续 HLS 也留在页面上下文中 `fetch`，避免 medialist 被 Playwright 的
  独立 HTTP 客户端再次判成非浏览器：
    1. 监听 page 的 request 事件，等"playlist.m3u8" 请求出现 → 拿 master URL
    2. page.fetch(master_url) → 拿 medialist URL
    3. page.fetch(medialist_url) → 拿所有 chunk URLs
    4. 并发 page.fetch(chunk) → 拼成 AAC bytes
    5. ffmpeg 转 m4a 容器
  整个过程 30 分钟节目几分钟下完，不依赖浏览器实时播放。
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from loguru import logger

# 复用 httpx 版本里的解析逻辑
from radio.radiko_source import (
    RadikoAudio,
    RadikoLiveSpec,
    RadikoTimefreeSpec,
    build_live_master_url,
    _build_master_playlist_url,
    parse_radiko_url,
)
from radio.utils.ffmpeg import find_ffmpeg


def _load_browser_cookies(cookies_path: Path) -> list[dict[str, Any]]:
    """加载浏览器导出的 cookies JSON，转成 Playwright `context.add_cookies` 格式。

    支持的格式：
    - EditThisCookie / Cookie-Editor 导出的 JSON 数组
      （字段：name/value/domain/path/expirationDate/httpOnly/secure/sameSite）
    - Playwright 原生格式（直接通过）
    """
    if not cookies_path.exists():
        raise FileNotFoundError(f"cookies 文件不存在：{cookies_path}")
    raw = json.loads(cookies_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"cookies JSON 必须是数组顶层：{cookies_path}")

    out: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        path = c.get("path", "/")
        if not name or value is None or not domain:
            continue
        cookie: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
        }
        if c.get("hostOnly"):
            cookie["url"] = f"https://{str(domain).lstrip('.')}{path}"
        else:
            cookie["domain"] = str(domain)
            cookie["path"] = str(path)
        # expires：EditThisCookie 用 expirationDate（float 秒），Playwright 用 expires
        exp = c.get("expires") or c.get("expirationDate")
        if exp is not None:
            try:
                cookie["expires"] = float(exp)
            except (TypeError, ValueError):
                pass
        if "httpOnly" in c:
            cookie["httpOnly"] = bool(c["httpOnly"])
        if "secure" in c:
            cookie["secure"] = bool(c["secure"])
        # sameSite：Playwright 大小写敏感，统一成 Lax/Strict/None
        ss = c.get("sameSite")
        if ss:
            ss_lower = str(ss).lower()
            mapping = {
                "lax": "Lax",
                "strict": "Strict",
                "none": "None",
                "no_restriction": "None",
                "unspecified": "Lax",
            }
            cookie["sameSite"] = mapping.get(ss_lower, "Lax")
        out.append(cookie)
    return out


def _playlist_entries(playlist_text: str, base_url: str) -> list[str]:
    """返回 playlist 中非注释 URI，并按当前 playlist URL 补齐相对路径。"""
    return [
        urljoin(base_url, ln.strip())
        for ln in playlist_text.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


async def _page_fetch_text(
    page: Any, url: str, headers: dict[str, str] | None = None
) -> str:
    result = await page.evaluate(
        """
        async ({ url, headers }) => {
            const resp = await fetch(url, {
                credentials: 'include',
                cache: 'no-store',
                headers,
            });
            const text = await resp.text();
            return { ok: resp.ok, status: resp.status, text };
        }
        """,
        {"url": url, "headers": headers or {}},
    )
    if not result["ok"]:
        raise RuntimeError(f"browser fetch HTTP {result['status']}: {result['text'][:500]}")
    return result["text"]


async def _page_fetch_bytes(
    page: Any, url: str, headers: dict[str, str] | None = None
) -> bytes:
    result = await page.evaluate(
        """
        async ({ url, headers }) => {
            const resp = await fetch(url, {
                credentials: 'include',
                cache: 'no-store',
                headers,
            });
            if (!resp.ok) {
                return {
                    ok: false,
                    status: resp.status,
                    text: await resp.text().catch(() => ''),
                };
            }
            const bytes = new Uint8Array(await resp.arrayBuffer());
            let binary = '';
            const step = 0x8000;
            for (let i = 0; i < bytes.length; i += step) {
                binary += String.fromCharCode(...bytes.subarray(i, i + step));
            }
            return { ok: true, body: btoa(binary) };
        }
        """,
        {"url": url, "headers": headers or {}},
    )
    if not result["ok"]:
        raise RuntimeError(f"browser fetch HTTP {result['status']}: {result['text'][:300]}")
    return base64.b64decode(result["body"])


@dataclass(frozen=True)
class _ChunkResult:
    idx: int
    data: bytes


async def _fetch_hls_chunk_urls_via_browser(
    page: Any,
    master_url: str,
    headers: dict[str, str] | None = None,
) -> list[str]:
    master_text = await _page_fetch_text(page, master_url, headers)
    medialist_urls = _playlist_entries(master_text, master_url)
    if not medialist_urls:
        raise RuntimeError(f"master 中无 variant URL：\n{master_text}")

    medialist_url = medialist_urls[0]
    chunklist_text = await _page_fetch_text(page, medialist_url)
    chunk_urls = _playlist_entries(chunklist_text, medialist_url)
    if not chunk_urls:
        raise RuntimeError(f"medialist 中无 chunk URL：\n{chunklist_text[:500]}")
    return chunk_urls


async def _download_timefree_hls_via_browser_fetch(
    page: Any,
    spec: RadikoTimefreeSpec,
    raw_aac: Path,
    headers: dict[str, str] | None = None,
) -> None:
    """按 Radiko time-free 的 15 秒滑动窗口拉完整节目。"""
    start_dt = datetime.strptime(spec.ft, "%Y%m%d%H%M%S")
    end_dt = datetime.strptime(spec.to, "%Y%m%d%H%M%S")
    window_seconds = 15
    total_windows = int((end_dt - start_dt).total_seconds() // window_seconds)
    if (end_dt - start_dt).total_seconds() % window_seconds:
        total_windows += 1

    logger.info(
        f"HLS step 1-2：按 {window_seconds}s 窗口扫描 time-free playlist（{total_windows} 窗口）"
    )
    seen: set[str] = set()
    chunk_urls: list[str] = []
    for idx in range(total_windows):
        seek_dt = start_dt + timedelta(seconds=idx * window_seconds)
        master_url = _build_master_playlist_url(
            spec,
            seek=seek_dt.strftime("%Y%m%d%H%M%S"),
            preroll=0,
            window_seconds=window_seconds,
        )
        window_chunks = await _fetch_hls_chunk_urls_via_browser(page, master_url, headers)
        for url in window_chunks:
            if url not in seen:
                seen.add(url)
                chunk_urls.append(url)
        if (idx + 1) % 20 == 0 or idx + 1 == total_windows:
            logger.info(
                f"playlist 扫描进度：{idx + 1}/{total_windows} 窗口，累计 {len(chunk_urls)} chunks"
            )

    if not chunk_urls:
        raise RuntimeError("未能从 time-free playlist 收集到任何 chunk")
    logger.info(f"HLS step 3：待下载 {len(chunk_urls)} 个 AAC chunks")

    sem = asyncio.Semaphore(6)

    async def _fetch(idx: int, url: str) -> _ChunkResult:
        async with sem:
            for attempt in range(3):
                try:
                    return _ChunkResult(idx, await _page_fetch_bytes(page, url))
                except Exception as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1.0 * (attempt + 1))
                    logger.warning(
                        f"chunk {idx} 第 {attempt + 1} 次失败：{e!r}，重试"
                    )
            raise RuntimeError("unreachable")

    results = await asyncio.gather(*[_fetch(i, u) for i, u in enumerate(chunk_urls)])
    results.sort(key=lambda x: x.idx)
    total_bytes = sum(len(c.data) for c in results)
    with raw_aac.open("wb") as f:
        for c in results:
            f.write(c.data)
    logger.info(f"全部 chunks 已下载：{total_bytes / 1024 / 1024:.1f} MB AAC")


async def _download_live_via_browser_fetch(
    page: Any,
    spec: RadikoLiveSpec,
    raw_aac: Path,
    headers: dict[str, str] | None = None,
) -> None:
    """Live 滚动 HLS：按墙钟跑 N 分钟，每 5s 拉一次 medialist，对没下过的
    chunk 增量 append 到 raw_aac 文件末尾。

    跟 time-shift 不同：
    - 没有总窗口数（live 永远滚动）
    - 终止条件是 wall-clock 时长
    - chunk URL 用 set 去重（同一 chunk 在多次 medialist 都会出现）
    """
    duration_s = spec.duration_minutes * 60
    poll_interval = 4.0  # 比 chunk duration 短一点防漏
    started = asyncio.get_event_loop().time()
    deadline = started + duration_s

    seen_chunks: set[str] = set()
    total_bytes = 0
    poll_iter = 0
    aac_file = raw_aac.open("wb")

    logger.info(
        f"Live 录制开始：目标 {spec.duration_minutes} min ({duration_s}s)，poll 间隔 {poll_interval}s"
    )

    try:
        while True:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                break

            master_url = build_live_master_url(spec)
            try:
                new_chunk_urls = await _fetch_hls_chunk_urls_via_browser(
                    page, master_url, headers
                )
            except Exception as e:
                # primary 失败 → fallback
                logger.warning(f"primary live master 失败：{e!r}，尝试 fallback")
                master_url = build_live_master_url(spec, prefer_primary=False)
                new_chunk_urls = await _fetch_hls_chunk_urls_via_browser(
                    page, master_url, headers
                )

            new_pending = [u for u in new_chunk_urls if u not in seen_chunks]
            for u in new_pending:
                seen_chunks.add(u)

            # 串行下载新 chunk（live 节奏慢，不需要并发；保证写入顺序）
            for u in new_pending:
                for attempt in range(3):
                    try:
                        data = await _page_fetch_bytes(page, u)
                        aac_file.write(data)
                        total_bytes += len(data)
                        break
                    except Exception as e:
                        if attempt == 2:
                            logger.error(f"live chunk 永久失败：{u[:80]}…")
                        else:
                            await asyncio.sleep(0.5 * (attempt + 1))

            poll_iter += 1
            elapsed = asyncio.get_event_loop().time() - started
            if poll_iter % 6 == 0 or new_pending:
                logger.info(
                    f"live poll #{poll_iter}：累计 {len(seen_chunks)} chunks / "
                    f"{total_bytes / 1024 / 1024:.1f} MB / "
                    f"已录 {elapsed:.0f}s / 还剩 {max(0, deadline - asyncio.get_event_loop().time()):.0f}s"
                )

            # 留时间窗给下一轮 poll
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))
    finally:
        aac_file.close()

    if total_bytes < 1024:
        raise RuntimeError(
            f"live 录制结束但没数据（{total_bytes} 字节）。"
            f"可能浏览器播放器未启动或反爬挡住了 chunk fetch。"
        )
    logger.info(
        f"Live 录制完成：{len(seen_chunks)} chunks / "
        f"{total_bytes / 1024 / 1024:.1f} MB AAC"
    )


async def _extract_radiko_auth_headers(page: Any) -> dict[str, str]:
    token = await page.evaluate(
        """
        () => {
            const player = window.player;
            const token = player?._authToken
                || player?._auth?._token
                || player?._auth?.authToken
                || player?._auth?.token
                || null;
            const areaId = player?._auth?._areaId
                || window.$?.Radiko?.area?.id
                || null;
            return { token, areaId };
        }
        """
    )
    if not token["token"]:
        raise RuntimeError("页面 player 未暴露 X-Radiko-AuthToken，无法拉 smartstream playlist")
    headers = {"X-Radiko-AuthToken": str(token["token"])}
    if token["areaId"]:
        headers["X-Radiko-AreaId"] = str(token["areaId"])
    return headers


async def record_radiko_via_playwright(
    url: str,
    output_dir: Path,
    duration_minutes: int,
    title: str | None = None,
    headless: bool = True,
    cookies_path: Path | None = None,
    cdp_url: str | None = None,
) -> RadikoAudio:
    """用 Playwright + Chromium 录制 Radiko time-shift。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright 未安装。请 `uv sync` 后跑 `uv run playwright install chromium`。"
        ) from e

    ffmpeg = find_ffmpeg()

    spec = parse_radiko_url(url, duration_minutes)
    is_live = spec.is_live
    output_dir.mkdir(parents=True, exist_ok=True)
    out_m4a = output_dir / f"{spec.title_hint}.m4a"
    raw_aac = output_dir / f"{spec.title_hint}.aac"
    if is_live:
        air_start_iso = datetime.now().isoformat(timespec="seconds")
        logger.info(f"模式：LIVE 直播录制（{spec.duration_minutes} min）")
    else:
        air_start_iso = datetime.strptime(spec.ft, "%Y%m%d%H%M%S").isoformat()
        logger.info(f"模式：time-shift 回放录制（{spec.duration_minutes} min）")

    async with async_playwright() as p:
        owns_browser = False
        if cdp_url:
            # 连接到用户预启动的 Chrome（带真实指纹 + cookies + session）
            logger.info(f"Playwright connect_over_cdp → {cdp_url}")
            browser = await p.chromium.connect_over_cdp(cdp_url)
            # 已有 context 复用，没有就新建
            if browser.contexts:
                context = browser.contexts[0]
                logger.info(f"复用已有 context（{len(context.pages)} 个 page）")
            else:
                context = await browser.new_context()
                logger.info("新建 context")
        else:
            # 自启动 Chromium（headless=new 带 audio 模式）
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--no-sandbox",
            ]
            if headless:
                launch_args.insert(0, "--headless=new")
                mode = "headless=new (Chrome 新 headless，带 audio)"
            else:
                mode = "headed (可见浏览器，需 GUI)"
            logger.info(f"Playwright 启动 Chromium ({mode})…")
            browser = await p.chromium.launch(
                headless=False,
                args=launch_args,
            )
            owns_browser = True
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                viewport={"width": 1440, "height": 900},
            )
            # stealth 仅用于自启动的 Chromium，CDP 模式用真实 Chrome 不需要
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP','ja','en-US','en'] });
                window.chrome = window.chrome || { runtime: {} };
                """
            )
        if cookies_path is not None:
            cookies = _load_browser_cookies(cookies_path)
            await context.add_cookies(cookies)
            logger.info(f"已注入 {len(cookies)} 条 cookies（来自 {cookies_path.name}）")
        page = None
        try:
            page = await context.new_page()

            # 监听所有 m3u8 / Radiko API 请求做诊断
            master_url_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
            seen_m3u8_urls: list[str] = []
            console_warnings: list[str] = []
            failed_requests: list[str] = []

            def _remember(items: list[str], value: str, limit: int = 20) -> None:
                if value not in items:
                    items.append(value)
                del items[limit:]

            def _on_console(msg) -> None:
                text = f"{msg.type}: {msg.text}"
                if "cookie" in text.lower() or "playlist" in text.lower():
                    _remember(console_warnings, text)
                    logger.debug(f"browser console: {text[:300]}")

            def _on_request_failed(request) -> None:
                failure = request.failure or ""
                text = f"{request.url} :: {failure}"
                if "playlist" in request.url or ".m3u8" in request.url:
                    _remember(failed_requests, text)
                    logger.debug(f"browser request failed: {text[:300]}")

            def _on_response(response) -> None:
                if (
                    response.status >= 400
                    and ("playlist" in response.url or ".m3u8" in response.url)
                ):
                    text = f"HTTP {response.status} {response.url}"
                    _remember(failed_requests, text)
                    logger.debug(f"browser response failed: {text[:300]}")

            def _on_request(request) -> None:
                url_str = request.url
                # 记下所有 m3u8 请求，便于排错
                if ".m3u8" in url_str or "playlist" in url_str:
                    if url_str not in seen_m3u8_urls:
                        seen_m3u8_urls.append(url_str)
                        logger.debug(f"截获 m3u8/playlist 请求：{url_str[:130]}")
                # 主匹配条件：master playlist。time-shift 必含 station_id 和 ft；
                # live 只过滤 station_id（没 ft），且 URL 在 simul-stream 上。
                if master_url_future.done() or "playlist.m3u8" not in url_str:
                    return
                if spec.station_id not in url_str:
                    return
                if is_live:
                    if "simul-stream" in url_str or "f-radiko.smartstream" in url_str:
                        logger.info(f"截获 live master ✅：{url_str[:120]}…")
                        master_url_future.set_result(url_str)
                else:
                    if spec.ft in url_str:
                        logger.info(f"截获 time-shift master ✅：{url_str[:120]}…")
                        master_url_future.set_result(url_str)

            # 同时挂 context（捕捉 service worker / 子 frame 触发的请求）和 page
            context.on("request", _on_request)
            page.on("request", _on_request)
            page.on("response", _on_response)
            page.on("console", _on_console)
            page.on("requestfailed", _on_request_failed)

            logger.info(f"导航到 {url}")
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            await asyncio.sleep(2)  # 等 React 单页应用充分渲染

            # 1. 处理隐私 / 利用规约同意弹窗
            for sel in (
                ".js-policy-accept",
                "button.btn--primary:has-text('承諾')",
                "button:has-text('承諾してradikoを利用する')",
            ):
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500):
                        await btn.click()
                        logger.info(f"点击同意弹窗：{sel}")
                        await asyncio.sleep(1)
                        break
                except Exception:
                    continue

            # 2. 点击播放按钮触发 master playlist 请求。
            # - time-shift: .live-detail__play a.play-radio（详情页"再生する"按钮）
            # - live:       主播放器 #play a.music-start / .player__play a
            if is_live:
                play_selectors = (
                    "#play a.music-start",
                    "#play a.item",
                    ".player__play a",
                    "a.music-start",
                    ".player__play",
                )
            else:
                play_selectors = (
                    ".live-detail__play a.play-radio",
                    ".live-detail__play a",
                    "a.play-radio",
                    ".btn--play",
                    ".live-detail__play",
                )
            clicked = False
            for sel in play_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=5000)
                        logger.info(f"点击播放按钮：{sel}")
                        clicked = True
                        break
                except Exception as e:
                    logger.debug(f"  按钮 {sel} 点击失败：{e!r}")
                    continue

            # 兜底：用 JS 直接 click 锚 + audio.play()
            if not clicked:
                logger.info("常规选择器失败，用 JS 强制触发播放")
                js_selectors = (
                    "['#play a.music-start','a.music-start','.player__play a','.player__play']"
                    if is_live
                    else "['.live-detail__play a.play-radio','.live-detail__play a','a.play-radio']"
                )
                await page.evaluate(
                    f"""
                    () => {{
                        const sels = {js_selectors};
                        for (const s of sels) {{
                            const el = document.querySelector(s);
                            if (el) {{ el.click(); break; }}
                        }}
                        const audio = document.querySelector('audio');
                        if (audio) {{
                            audio.muted = true;
                            audio.play().catch(() => {{}});
                        }}
                    }}
                    """
                )

            try:
                await asyncio.wait_for(master_url_future, timeout=30.0)
                hls_headers = await _extract_radiko_auth_headers(page)
                logger.info(
                    "截获播放器 master 后，改用 smartstream 15 秒窗口拉完整区间"
                )
            except asyncio.TimeoutError as e:
                hidden_url = await page.evaluate(
                    """
                    () => {
                        const url = document.querySelector('#url')?.value || '';
                        const tmpUrl = document.querySelector('#tmpUrl')?.value || '';
                        return url.includes('playlist.m3u8') ? url : tmpUrl;
                    }
                    """
                )
                if hidden_url and "playlist.m3u8" in hidden_url:
                    hls_headers = await _extract_radiko_auth_headers(page)
                    logger.warning(
                        "30s 内未观察到 playlist 请求，改用 smartstream playlist + 页面 auth token"
                    )
                else:
                    # 调试：dump 当前页面状态 + 所有截获的 m3u8 URL
                    html_len = len(await page.content())
                    seen_dump = "\n  ".join(seen_m3u8_urls[:8]) or "(无)"
                    console_dump = "\n  ".join(console_warnings[:8]) or "(无)"
                    failed_dump = "\n  ".join(failed_requests[:8]) or "(无)"
                    hint = (
                        "自启动 Chromium 仍可能被 Radiko 判定为非真实浏览器；"
                        "建议先启动真实 Chrome 并传 --cdp-url http://127.0.0.1:9222。"
                    )
                    raise RuntimeError(
                        f"30s 内未截获 master playlist URL。\n"
                        f"页面 HTML {html_len} 字符。\n"
                        f"截获到的 m3u8/playlist 请求：\n  {seen_dump}\n"
                        f"相关 console：\n  {console_dump}\n"
                        f"失败请求：\n  {failed_dump}\n"
                        f"{hint}"
                    ) from e

            logger.info("浏览器 auth 完成，开始用页面内 fetch 拉 HLS")
            if is_live:
                await _download_live_via_browser_fetch(
                    page, spec, raw_aac, hls_headers
                )
            else:
                await _download_timefree_hls_via_browser_fetch(
                    page, spec, raw_aac, hls_headers
                )
        finally:
            if owns_browser:
                await browser.close()
            else:
                if page is not None:
                    await page.close()
                await browser.close()

    # 4. ffmpeg AAC ADTS → m4a
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
            f"{stderr.decode('utf-8', errors='replace')[:1000]}"
        )
    raw_aac.unlink(missing_ok=True)

    size_mb = out_m4a.stat().st_size / 1024 / 1024
    logger.info(f"Radiko 录制完成：{out_m4a.name}（{size_mb:.1f} MB）")

    display = title or f"Radiko {spec.station_id} {air_start_iso}"
    return RadikoAudio(
        audio_path=out_m4a,
        title=display,
        source_url=url,
        station_id=spec.station_id,
        air_start_iso=air_start_iso,
    )
