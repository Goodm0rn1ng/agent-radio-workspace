# 前端对接 API 草案

最近更新：2026-05-17

本 API 是前端 MVP 的本地后端入口。所有重活都后台执行；前端提交任务后拿
`job_id`，再轮询任务状态。

## 启动

```bash
uv run python scripts/main_api.py --host 127.0.0.1 --port 8000
```

## 视频批量处理

`POST /api/video-jobs`

```json
{
  "urls": [
    "https://www.youtube.com/watch?v=..."
  ],
  "profile_id": "mygo_meigo_shukai",
  "fine_translation": false,
  "keep_audio": false
}
```

播放列表倒序处理：

```json
{
  "playlist_url": "https://www.youtube.com/watch?v=zloDD0UC4XE&list=PLUFFl4hYd1R0SQPSCAqPVuT5F2By4pqO7&index=178",
  "playlist_start_index": 178,
  "playlist_end_index": 1,
  "title_template": "{title}",
  "profile_id": "mygo_meigo_shukai",
  "fine_translation": false
}
```

## 播放列表预览

`POST /api/playlists/expand`

```json
{
  "playlist_url": "https://www.youtube.com/watch?v=zloDD0UC4XE&list=PLUFFl4hYd1R0SQPSCAqPVuT5F2By4pqO7&index=178",
  "start_index": 178,
  "end_index": 1
}
```

## 指定时间直播录制

`POST /api/live-jobs`

```json
{
  "url": "https://www.youtube.com/@example/live",
  "start_at": "2026-05-18T21:00:00+09:00",
  "duration_minutes": 60,
  "title": "节目名",
  "profile_id": "hina_radio",
  "fine_translation": false
}
```

Radiko live 也走同一个入口：

```json
{
  "url": "https://radiko.jp/#!/live/QRR",
  "start_at": "2026-05-18T00:30:00+09:00",
  "duration_minutes": 30,
  "title": "文化放送 QRR 00:30 定期番組",
  "profile_id": "hina_radio"
}
```

## Prompt Profiles

`GET /api/profiles`

返回当前 `config/profiles/*/profile.yaml` 中的 profile 列表。内置：

- `mygo_meigo_shukai`
- `hina_radio`
- `general_seiyuu_radio`

`POST /api/profiles`

```json
{
  "id": "new_radio",
  "name": "XX 广播",
  "description": "XX 节目专用 prompt",
  "terminology_path": "config/terminology.yaml",
  "translation_prompt": "Role: ... {input_json}",
  "summary_prompt": "你是一位... {transcript}"
}
```

`translation_prompt` 必须包含 `{input_json}`；`summary_prompt` 必须包含 `{transcript}`。

## 任务状态

`GET /api/jobs/{job_id}`

状态值：

- `queued`
- `waiting`
- `running`
- `succeeded`
- `failed`

API 执行仍是单进程内存 task，但 job / item / run / artifact 快照已写入
`data/state.sqlite`。API 重启后历史 job 仍可查询；重启前未完成的 job 会标记为
`failed`，不会自动恢复执行。

## Artifact 索引

`GET /api/artifacts`

可选 query：

- `job_id`
- `run_id`
- `limit`

返回每个 run 已登记的产物索引。当前最小版本登记 `work_dir`，前端可继续用下面的文件接口展开目录。

`GET /api/artifacts/files?path=...`

列出某个 `recordings/` 内目录的一级文件。接口只允许读取 `runtime.recordings_dir` 内的路径。

`GET /api/artifacts/file?path=...`

以内联方式打开文件。加 `download=1` 时作为下载附件返回。接口同样限制在 `runtime.recordings_dir` 内。
