# 批次 2 本地存储运行手册

## 布局与边界

控制面只监听 `127.0.0.1`。运行目录中有三类持久状态：

```text
runtime/
├── state/pipeline.sqlite3        # SQLite 主库（WAL 模式）
├── state/control.lock            # 单控制面互斥锁
├── artifacts/blobs/sha256/       # 内容寻址、不可变 WAV
├── artifacts/manifests/versions/ # 不可变版本清单
├── artifacts/staging/            # 崩溃后会自动清理
├── artifacts/quarantine/         # 无 DB 归属的旧 blob
└── jobs/<job_id>/                # 单次执行工作目录
```

禁止手工删除 SQLite 的 `-wal`/`-shm` 文件、直接更新 current 指针、编辑 blob 或编辑版本
清单。通过 API/CLI 完成激活、重试和清理。

## 启动与停止

```powershell
pwsh -NoProfile -File scripts/start.ps1 -Config config/app.fake.yaml -Json
uv run voice-pipeline doctor --server http://127.0.0.1:8765 --json
pwsh -NoProfile -File scripts/stop.ps1 `
  -RunFile runtime/run/processes.json `
  -ReceiptPath runtime/run/stop-receipt.json -Json
```

启动在持有 `control.lock` 时迁移数据库、执行 `quick_check`、清理陈旧中间文件、检查
ready 版本、启动引擎、队列和持久化 dispatcher。停止不删除 WAL/SHM；先停止接收新请求，
再中断未完成任务，最后释放数据库锁。

## 备份与恢复

控制面停止后使用 SQLite backup API 或 `sqlite3 .backup`，并一同复制 `artifacts/`：

```powershell
sqlite3 runtime/state/pipeline.sqlite3 ".backup runtime/backup/pipeline.sqlite3"
Copy-Item runtime/artifacts runtime/backup/artifacts -Recurse
```

恢复时使用同一对数据库和 artifact 根目录。启动恢复会删除 staging partial、隔离没有数据
库记录的旧 blob，并将缺失/哈希损坏版本标记为不可用；若该版本是当前版本，指针会清空而
不会猜测回退到另一条历史。

## 版本、重试与保留

- `POST /api/v1/segments/{segment_id}/jobs/reference` 生成新的 reference 版本；
- `POST /api/v1/segments/{segment_id}/jobs/gsv` 只使用提交时冻结的 active reference，
  不会重新调用 IndexTTS；
- `POST /api/v1/jobs/{job_id}/retry` 从原始输入快照新建一个 job ID；
- `POST /api/v1/maintenance/retention/plan` 预览清理，随后对返回 plan ID 调用 `/apply`；
  清理保留当前版本、每类五个最近非当前版本、被保留 GSV 的父 reference、以及队列/运行
  任务快照引用的版本。

## 质量模型离线准备

真实模式需要锁定的 faster-whisper small 资产。首次联网准备，之后可离线校验：

```powershell
pwsh -NoProfile -File scripts/setup-quality.ps1 -Root (Get-Location).Path
pwsh -NoProfile -File scripts/setup-quality.ps1 -Root (Get-Location).Path -Offline
```

没有本地模型时，真实控制面不会报告 quality ready；fake/external_test 模式使用确定性分析
器，不下载模型。

## 批次 2 非目标

本批次不提供 OpenAI LLM、自动文本分块、WebUI/SSE、用户级 draft 编辑、自动拼接或公网/
多用户/分布式队列。它只提供后续工作台需要的本地版本、指针、缓存、保留和恢复原语。
