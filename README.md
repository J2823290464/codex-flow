# Feishu-driven Codex automation

这个目录是一套“飞书多维表 -> 本地开发执行 -> 人工审核 -> 远程推送”的自动化脚手架。

核心约定：

- 飞书多维表负责存放需求和 BUG，并作为状态机。
- 自动化领取任务时，把状态从 `待处理` 改为 `开发中`。
- 本地执行完成并提交到本地 `main` 后，把状态改为 `待人工审核`。
- 人工审核通过后，把飞书状态改为 `审核通过`。
- 推送流程只处理 `审核通过` 的记录，推送远程 `main` 后改为 `已推送`。

## 文件

- [docs/feishu-codex-automation.md](docs/feishu-codex-automation.md)：流程设计、字段约定和部署建议。
- [config/feishu_automation.example.json](config/feishu_automation.example.json)：配置模板。
- [scripts/feishu_task_runner.py](scripts/feishu_task_runner.py)：飞书多维表状态同步与本地命令执行器。

## 最小使用方式

1. 复制配置模板：

   ```powershell
   Copy-Item config/feishu_automation.example.json config/feishu_automation.local.json
   ```

2. 修改 `config/feishu_automation.local.json` 里的飞书应用、多维表、字段名、仓库路径和执行命令：

   ```json
   {
     "feishu": {
       "app_id": "cli_xxx",
       "app_secret": "xxx",
       "wiki_url": "https://xxx.feishu.cn/wiki/xxx?table=tblxxx&view=vewxxx",
       "app_token": "bascn_xxx",
       "table_id": "tblxxx"
     }
   }
   ```

   如果是飞书 wiki 里的多维表，推荐直接填 `wiki_url`。脚本会自动解析链接里的 wiki 节点 token、`table_id` 和 `view_id`，并通过开放平台获取真正的多维表 `app_token`。
   如果不想限制在某个视图，`view_id` 可以留空或直接删掉。

3. 先做只读检查：

   ```powershell
   python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode list
   ```

4. 执行待处理任务：

   ```powershell
   python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode execute
   ```

5. 人工审核通过后推送远程：

   ```powershell
   python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode push-approved
   ```

注意：脚手架默认不运行本地服务测试。静态校验命令由配置里的 `static_check_command` 控制。

## 飞书原生面板

不用本地页面。飞书多维表本身就是任务面板，建议在飞书里建这些视图：

- `任务总览`：表格视图，展示标题、类型、状态、优先级、版本需求文档、执行结果、本地提交、远程推送。
- `状态看板`：看板视图，按 `状态` 分组，直接看 `待处理`、`开发中`、`待人工审核`、`审核通过`、`已推送`、`执行失败`。
- `待审核`：筛选 `状态 = 待人工审核`，人工审核后把状态改成 `审核通过`。
- `失败任务`：筛选 `状态 = 执行失败`，看 `执行结果` 字段里的错误。
- `版本进度`：按 `版本需求文档` 或版本字段分组，追踪养鸡庄园版本需求进度。

后台 worker 常驻运行，不提供页面：

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode watch
```

它会按配置里的 `automation.poll_interval_seconds` 定时拉飞书：发现 `待处理` 就交给 Codex 开发并回写状态，发现 `审核通过` 就推送远程并回写状态。
默认最多同时处理 3 个需求或 BUG，超出的会留在飞书里等下一轮。

如果 Codex 定时自动化无法联网访问飞书，推荐改用 Windows 计划任务执行一次性扫描：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/register_feishu_task.ps1 -IntervalMinutes 60
```

计划任务每次只运行：

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode execute
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode push-approved
```

执行完自动退出，不会常驻后台。日志写入 `logs/feishu-automation-YYYYMMDD.log`。

Codex 自动化里如果要把提权范围限制在飞书 API，可以使用拆分命令：

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode claim-next
```

这个命令只负责读取飞书、领取一条 `待处理` 任务、写入 `AI开始时间`，并把任务 prompt 写到 `state/current_task.json`。

开发完成并本地提交后：

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode finish-task --message "本地开发完成，已提交到本地 main，等待人工审核。"
```

开发失败时：

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode fail-task --message "失败原因"
```

这三个命令涉及飞书 API，可以单独提权。Codex 实际开发、静态校验和 Git 提交不需要提权。

在养鸡庄园仓库里，静态校验默认使用：

```powershell
D:\Software\NodeJs\npm.cmd run lint:version
```

它会检查 `package.json`、`VERSION` 和 `src/config/version.js` 的版本一致性。

`watch` 现在会把 Codex 开发放到后台异步执行，轮询循环不会一直卡在单个任务上；多条需求会按顺序排队处理。

## 表结构自动识别和补齐

只读查看当前飞书表字段：

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode inspect-schema
```

自动补齐缺失字段：

```powershell
python scripts/feishu_task_runner.py --config config/feishu_automation.local.json --mode sync-schema
```

脚本会自动匹配常见字段名，例如 `任务` 会识别为标题，`需求描述` 会识别为描述，`版本` 会识别为版本字段。

你的表里已有业务状态字段时，建议让自动化单独使用 `自动化状态`，避免把业务状态和 worker 状态混在一起：

```json
"schema": {
  "automation_status_field": "自动化状态"
}
```

这个字段会包含 `待需求审核`、`待处理`、`开发中`、`待人工审核`、`审核通过`、`已推送`、`执行失败`，用于 Codex 自动化流转。

建议别人新提交的需求/BUG 默认填 `自动化状态 = 待需求审核`。你人工确认这个任务可以交给 AI 后，再改成 `待处理`。worker 只会执行 `待处理`，不会执行 `待需求审核`。

自动化还会维护两个看板时间字段：

- `AI开始时间`：任务被 worker 领取并改成 `开发中` 时写入。
- `AI结束时间`：任务执行成功进入 `待人工审核`，或失败进入 `执行失败` 时写入。
- `本地Git记录`：本地提交后写入分支、commit hash、提交标题和记录时间。

下次扫描到 `自动化状态 = 审核通过` 时，worker 会把本地 `main` 推送到远程 `main`，然后写入 `远程推送` 并把状态改成 `已推送`。

## 中文日志

配置里可以控制日志输出：

```json
"logging": {
  "enabled": true,
  "command_output": false
}
```

- `enabled`：是否输出中文运行日志。
- `command_output`：是否输出 Codex、静态校验、Git push 的命令输出，调试时可以改成 `true`。

## Codex AI 执行需求

支持。配置里的 `runner.development_command` 就是 AI 开发入口，默认使用：

```json
[
  "codex",
  "exec",
  "--ask-for-approval",
  "never",
  "--sandbox",
  "workspace-write",
  "--cd",
  "{repo_path}",
  "-"
]
```

脚本会把飞书记录里的标题、类型、描述、版本需求文档和执行要求拼成完整 prompt，通过 stdin 交给 `codex exec` 在目标仓库里改代码。`{repo_path}` 会自动替换成配置里的项目路径。

默认使用当前 Codex CLI 的登录态和模型配置。如果你想指定模型，可以在命令里加上 `"-m", "模型名"`。
