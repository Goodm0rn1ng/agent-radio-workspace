"""Clipper 命令行入口。

  python -m clip.cli past --dry-run                 # Branch A 选材 → plan.json
  python -m clip.cli past --topk 5                   # Branch A 全流程出成片
  python -m clip.cli new --url <youtube> --dry-run   # Branch B 下载+入库+分析
  python -m clip.cli new --url <youtube>             # Branch B 全流程出成片
"""
from __future__ import annotations

import argparse

from clip.config import clip_config


def main() -> None:
    ap = argparse.ArgumentParser(prog="clipper")
    sub = ap.add_subparsers(dest="branch", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="只选材，产 plan.json，不切片/不渲染")
    common.add_argument("--no-render", action="store_true", help="切片但不做字幕对齐/烧录")
    common.add_argument("--partition", help="覆盖 B 站分区（逗号分隔，如 music,game,vtuber）")
    common.add_argument("--hours", type=int, help="时间窗（小时）")
    common.add_argument("--topk", type=int, help="保留的热点/片段数")

    p_past = sub.add_parser("past", parents=[common], help="Branch A：过往素材二次创作")
    p_new = sub.add_parser("new", parents=[common], help="Branch B：新上传直播二次创作")
    p_new.add_argument("--url", required=True, help="YouTube 直播/视频 URL")
    p_new.add_argument("--res", type=int, help="下载分辨率上限")
    p_new.add_argument("--program", help="节目处理/归档方案 id（clip/programs/<id>.yaml）")
    p_new.add_argument("--telegram", action="store_true",
                       help="处理入库后推送 Telegram 切片菜单（点击即切片），不在本地自动渲染")

    args = ap.parse_args()

    # CLI 覆盖配置
    if args.partition:
        clip_config.bilibili_partitions = args.partition
    if args.hours:
        clip_config.clip_hours_window = args.hours
    if args.topk:
        clip_config.clip_topk = args.topk
    if args.branch == "new" and args.res:
        clip_config.clip_video_res = args.res

    if args.branch == "past":
        from clip.pipeline import pipeline_past
        pipeline_past(dry_run=args.dry_run, no_render=args.no_render)
    else:
        from clip.pipeline import pipeline_new
        pipeline_new(args.url, profile_id=args.program,
                     dry_run=args.dry_run, no_render=args.no_render,
                     to_telegram=getattr(args, "telegram", False))


if __name__ == "__main__":
    main()
