"""后期包装：把字幕硬烧（hardcode）到画面，输出成片 mp4。

本机 ffmpeg 未编译 libass/libfreetype（无 subtitles/drawtext 滤镜），因此用 Pillow
把每条字幕渲染成透明 PNG，再用 overlay 滤镜按时间区间烧录 —— 自包含、无需重装 ffmpeg。

- 视频源：直接在画面上叠字幕。
- 音频源（过往广播）：先 showwaves 合成波形画面，再叠字幕。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from clip.aligner import Cue, parse_srt
from clip.ffmpeg_util import ffmpeg_bin, has_video_stream, run

_W, _H = 1280, 720
_BAND_H = 180
_MARGIN_BOTTOM = 16               # 底部字幕，但留出防裁切安全距离
_JA_SIZE, _ZH_SIZE = 38, 42       # 中文行略大
_JA_MIN_SIZE, _ZH_MIN_SIZE = 30, 32
_GAP = 8
_STROKE_PX = 5
_SHADOW_OFFSET = 3
_TEXT_BOTTOM_INSET = 18
_MAX_TEXT_W = 1120
# 字幕配色（应援色由节目方案传入；默认白）：日文行白、中文行应援色
_ACCENT_DEFAULT = (255, 255, 255)
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",   # 同时覆盖中日文
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _text_box(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=_STROKE_PX)
    return box, box[2] - box[0], box[3] - box[1]


def _fit_font(draw, text: str, size: int, min_size: int):
    font = _font(size)
    _, w, _ = _text_box(draw, text, font)
    while w > _MAX_TEXT_W and size > min_size:
        size -= 1
        font = _font(size)
        _, w, _ = _text_box(draw, text, font)
    return font


def _readable_accent(color):
    """把较深的应援色轻微提亮，靠描边保留辨识度。"""
    return tuple(min(255, int(c * 0.58 + 255 * 0.42)) for c in color)


def _draw_text(draw, x, y, text, font, color):
    box, _, _ = _text_box(draw, text, font)
    pos = (x - box[0], y - box[1])
    shadow = (pos[0] + _SHADOW_OFFSET, pos[1] + _SHADOW_OFFSET)
    draw.text(shadow, text, font=font, fill=(0, 0, 0, 170),
              stroke_width=_STROKE_PX, stroke_fill=(0, 0, 0, 170))
    draw.text(pos, text, font=font, fill=color + (255,),
              stroke_width=_STROKE_PX, stroke_fill=(4, 8, 18, 245))


def _render_cue_png(cue: Cue, path: Path, accent=_ACCENT_DEFAULT) -> None:
    """全透明字幕：黑描边 + 阴影，底部对齐但避免下沿裁切。"""
    img = Image.new("RGBA", (_W, _BAND_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rows = []  # (text, font, color)
    if cue.ja:
        rows.append((cue.ja, _fit_font(draw, cue.ja, _JA_SIZE, _JA_MIN_SIZE), (255, 255, 255)))
    if cue.zh:
        rows.append((cue.zh, _fit_font(draw, cue.zh, _ZH_SIZE, _ZH_MIN_SIZE), _readable_accent(accent)))
    if not rows:
        img.save(path)
        return

    dims = [_text_box(draw, t, f)[1:] for t, f, _ in rows]
    text_h = sum(h for _, h in dims) + _GAP * (len(rows) - 1)
    y = _BAND_H - text_h - _TEXT_BOTTOM_INSET
    for (text, font, color), (w, h) in zip(rows, dims):
        x = (_W - w) / 2
        _draw_text(draw, x, y, text, font, color)
        y += h + _GAP
    img.save(path)


def package(cut_path: Path, srt_path: Path, out_dir: Path, idx: int,
            accent=_ACCENT_DEFAULT) -> Path:
    # _write_srt() already normalizes cue timing. Normalizing again here can shift
    # mid-clip cue boundaries after preview/assembly and make burned subtitles
    # disagree with the reviewed timing.
    cues = parse_srt(srt_path)
    png_dir = out_dir / f"_subs_{idx:02d}"
    png_dir.mkdir(exist_ok=True)
    for old in png_dir.glob("s*.png"):
        old.unlink()
    pngs = []
    for j, c in enumerate(cues):
        p = png_dir / f"s{j:03d}.png"
        _render_cue_png(c, p, accent)
        pngs.append((p, c))

    out_name = f"clip_{idx:02d}_final.mp4"
    out_path = out_dir / out_name
    ff = ffmpeg_bin()
    is_video = has_video_stream(str(cut_path))

    cmd = [ff, "-y", "-i", str(cut_path)]
    for p, _ in pngs:
        cmd += ["-i", str(p)]

    # 基础视频流：视频源缩放到 1280x720；音频源用 showwaves 生成波形画面。
    parts = []
    if is_video:
        parts.append(f"[0:v]scale={_W}:{_H}[base0]")
    else:
        parts.append(
            f"color=c=0x0f1419:s={_W}x{_H}:d=1[bg];"
            f"[0:a]showwaves=s={_W}x{_H}:mode=cline:colors=0x88c0a0[wave];"
            f"[bg][wave]overlay=format=auto[base0]"
        )
    cur = "base0"
    sub_y = _H - _BAND_H - _MARGIN_BOTTOM   # 抬高，避开底部卡拉OK UI
    for j, (_, c) in enumerate(pngs):
        nxt = f"v{j}"
        # PNG 输入索引 = j+1；按时间区间启用。
        parts.append(
            f"[{cur}][{j+1}:v]overlay=0:{sub_y}:"
            f"enable='between(t,{c.start:.3f},{c.end:.3f})'[{nxt}]"
        )
        cur = nxt
    filtergraph = ";".join(parts)

    cmd += [
        "-filter_complex", filtergraph,
        "-map", f"[{cur}]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        "-shortest", str(out_path),
    ]
    run(cmd)
    return out_path
