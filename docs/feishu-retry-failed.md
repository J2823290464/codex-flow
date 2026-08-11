# Retry failed Feishu tasks

失败任务可以手动重新转为待处理，让后台 worker 下一轮继续领取。

## Retry one failed task

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode retry-failed
```

默认只处理 1 条执行失败的记录，避免失败任务被批量重复执行。

## Retry a specific record

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode retry-failed --record-id recxxxx
```

## Retry all failed records

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode retry-failed --limit 0
```

## Continue previous Codex session

每次 Codex 执行时，脚本会尽量把本地 Codex session id 记录到：

```text
state/tasks/<record_id>.json
```

失败任务通过 `retry-failed` 重新转为待处理后，如果这个 state 文件里有
`codex_session_id`，下一次执行会使用：

```text
codex exec resume <codex_session_id> -
```

这样就会接着上一次会话继续处理。没有找到 session id 时，任务仍会重新转为
待处理，但下一次会按新会话执行。

## Commit-only retry

如果上次失败发生在 Git 本地提交阶段，例如：

```text
git add -A failed
fatal: Unable to create .../.git/index.lock
```

`retry-failed` 会标记为只重试本地提交。下一轮 worker 会跳过 Codex 开发步骤，
复用上次已经完成的修改和校验结果，直接再次执行本地提交并回写飞书。

如果 `runner.commit_after_development=false`，下一轮不会重新提交，只会检查工作区
是否干净，并读取 AI 会话已经提交的 `HEAD` 回写飞书。

脚本不会自动删除 `.git/index.lock`。如果确认没有其他 Git 命令、编辑器或
提交工具正在运行，可以手动清理这个锁文件后再重试。

## Merge-only retry

worktree 多任务模式下，如果失败发生在把任务分支合并到本地 `main` 的阶段
（通常是文件冲突），`retry-failed` 会标记为只重试合并。下一轮 worker 不会
重新执行 Codex，只尝试再次合并 `feishu/<record_id>` 分支到本地 `main`。

冲突需要先在保留的 worktree 或任务分支上人工解决。手动处理时可以：

```powershell
git -C C:\Users\12624\Documents\养鸡庄园 worktree list
```

找到对应 `feishu/<record_id>` 分支的 worktree，在分支上解决冲突并提交，然后
重新运行 `retry-failed` 让 worker 只重试合并。
