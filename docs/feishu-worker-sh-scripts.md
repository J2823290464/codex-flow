# Feishu worker start and stop scripts

The shell scripts in `scripts/` can run the Feishu `watch` worker in the
background, write logs to `logs/`, and store the process id in `state/`.

这些脚本用于把 Feishu `watch` worker 放到后台运行，不需要一直开着前台
cmd 窗口。日志会写到 `logs/`，进程号会写到 `state/`。

## Start

```sh
sh scripts/start_feishu_worker.sh
```

If `sh` is not in PATH on Windows, run it from PowerShell with Git Bash:

```powershell
& 'C:\Program Files\Git\bin\sh.exe' scripts/start_feishu_worker.sh
```

It is equivalent to:

```sh
sh scripts/feishu_worker.sh start
```

Runtime logs are written to:

```text
logs/feishu-worker.log
```

Start, stop, status, and runtime messages are written to the same file. When it
exceeds 5 MB, the script keeps one backup:

```text
logs/feishu-worker.log.1
```

## Stop

```sh
sh scripts/stop_feishu_worker.sh
```

PowerShell with Git Bash:

```powershell
& 'C:\Program Files\Git\bin\sh.exe' scripts/stop_feishu_worker.sh
```

It is equivalent to:

```sh
sh scripts/feishu_worker.sh stop
```

## Status or restart

```sh
sh scripts/feishu_worker.sh status
sh scripts/feishu_worker.sh restart
```

## Optional environment variables

Use another config file:

```sh
CONFIG_PATH=config/another.json sh scripts/start_feishu_worker.sh
```

Use another Python command:

```sh
PYTHON_BIN=python3 sh scripts/start_feishu_worker.sh
```

Use another worker log path:

```sh
WORKER_LOG=logs/worker-debug.log sh scripts/start_feishu_worker.sh
```

Change the log rotation threshold:

```sh
MAX_LOG_BYTES=10485760 sh scripts/start_feishu_worker.sh
```
