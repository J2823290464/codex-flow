#!/usr/bin/env python3
"""Feishu Bitable task runner for a human-gated Codex workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


class RunnerError(RuntimeError):
    pass


@dataclass
class TaskRecord:
    record_id: str
    fields: dict[str, Any]


DEFAULT_FIELD_SPECS: dict[str, dict[str, Any]] = {
    "title": {"name": "标题", "type": 1, "aliases": ["标题", "任务", "任务标题", "需求标题", "名称", "任务名称"]},
    "type": {
        "name": "类型",
        "type": 3,
        "aliases": ["类型", "任务类型", "类别"],
        "options": ["需求", "BUG", "优化"],
    },
    "status": {
        "name": "状态",
        "type": 3,
        "aliases": ["状态", "任务状态", "进度", "处理状态"],
        "options": ["待需求审核", "待处理", "开发中", "待人工审核", "审核通过", "已推送", "执行失败"],
    },
    "priority": {
        "name": "优先级",
        "type": 3,
        "aliases": ["优先级", "优先度", "Priority"],
        "options": ["高", "中", "低"],
    },
    "description": {
        "name": "需求/BUG描述",
        "type": 1,
        "aliases": ["需求/BUG描述", "描述", "需求描述", "BUG描述", "任务描述", "详情"],
    },
    "version_doc": {
        "name": "版本需求文档",
        "type": 1,
        "aliases": ["版本需求文档", "版本文档", "需求文档", "文档", "版本"],
    },
    "branch": {"name": "目标分支", "type": 1, "aliases": ["目标分支", "分支", "branch"]},
    "result": {
        "name": "执行结果",
        "type": 1,
        "aliases": ["执行结果", "运行结果", "处理结果", "日志", "错误信息"],
    },
    "local_commit": {
        "name": "本地提交",
        "type": 1,
        "aliases": ["本地提交", "本地commit", "commit", "提交"],
    },
    "local_git_record": {
        "name": "本地Git记录",
        "type": 1,
        "aliases": ["本地Git记录", "本地 Git 记录", "Git记录", "本地Git", "本地提交记录"],
    },
    "remote_push": {
        "name": "远程推送",
        "type": 1,
        "aliases": ["远程推送", "推送结果", "远程提交"],
    },
    "ai_start_time": {
        "name": "AI开始时间",
        "type": 5,
        "aliases": ["AI开始时间", "AI执行开始时间", "开始执行时间", "自动化开始时间"],
        "property": {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False},
    },
    "ai_end_time": {
        "name": "AI结束时间",
        "type": 5,
        "aliases": ["AI结束时间", "AI执行结束时间", "结束执行时间", "自动化结束时间"],
        "property": {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False},
    },
}


DEFAULT_STATUS_SPECS = {
    "intake": {"name": "待需求审核", "aliases": ["待需求审核", "待审核需求", "需求待审核", "待确认", "待评审"]},
    "pending": {"name": "待处理", "aliases": ["待处理", "未开始", "待开发", "待领取", "待办", "TODO", "Todo"]},
    "in_progress": {"name": "开发中", "aliases": ["开发中", "施工中", "处理中", "进行中", "已领取", "Doing"]},
    "review": {"name": "待人工审核", "aliases": ["待人工审核", "待审核", "待验收", "待Review", "Review"]},
    "approved": {"name": "审核通过", "aliases": ["审核通过", "已审核", "已通过", "通过", "Approved"]},
    "pushed": {"name": "已推送", "aliases": ["已推送", "已发布", "已上线", "Done"]},
    "failed": {"name": "执行失败", "aliases": ["执行失败", "已停滞", "失败", "处理失败", "Failed"]},
}


def log_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("logging", {}).get("enabled", True))


def command_output_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("logging", {}).get("command_output", False))


def log_info(config: dict[str, Any], message: str) -> None:
    if log_enabled(config):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")


def log_error(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 错误：{message}", file=sys.stderr)


def log_command_output(config: dict[str, Any], title: str, output: str) -> None:
    if not command_output_enabled(config) or not output.strip():
        return
    log_info(config, f"{title}输出：")
    print(output.strip())


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    normalize_feishu_config(config)
    return config


def configured(value: Any, placeholder: str = "") -> bool:
    if not isinstance(value, str):
        return value is not None
    text = value.strip()
    return bool(text) and text not in {placeholder, "xxx", "cli_xxx", "bascn_xxx", "tblxxx"}


def normalize_feishu_config(config: dict[str, Any]) -> None:
    feishu_config = config.setdefault("feishu", {})
    wiki_url = feishu_config.get("wiki_url", "")
    if not wiki_url:
        return

    parsed = urllib.parse.urlparse(wiki_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "wiki" and not configured(feishu_config.get("wiki_node_token")):
        feishu_config["wiki_node_token"] = parts[1]

    query = urllib.parse.parse_qs(parsed.query)
    if query.get("table") and not configured(feishu_config.get("table_id"), "tblxxx"):
        feishu_config["table_id"] = query["table"][0]
    if query.get("view") and "view_id" not in feishu_config:
        feishu_config["view_id"] = query["view"][0]

    config.setdefault("fields", {})
    config.setdefault("statuses", {})


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    last_url_error: urllib.error.URLError | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RunnerError(f"HTTP {exc.code} from Feishu: {details}") from exc
        except urllib.error.URLError as exc:
            last_url_error = exc
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            raise RunnerError(f"Cannot reach Feishu after {attempt} attempts: {last_url_error}") from exc

    decoded = json.loads(body)
    if decoded.get("code", 0) != 0:
        raise RunnerError(f"Feishu API error: {decoded}")
    return decoded


def get_tenant_access_token(config: dict[str, Any]) -> str:
    feishu_config = config.get("feishu", {})
    app_id = feishu_config.get("app_id") or os.environ.get("FEISHU_APP_ID")
    app_secret = feishu_config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RunnerError("Set feishu.app_id and feishu.app_secret in config before running.")

    log_info(config, "开始获取飞书 tenant_access_token。")
    response = request_json(
        "POST",
        f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    log_info(config, "飞书 tenant_access_token 获取成功。")
    return response["tenant_access_token"]


def bitable_records_url(config: dict[str, Any]) -> str:
    app_token = config["feishu"]["resolved_app_token"]
    table_id = config["feishu"]["table_id"]
    return f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records"


def bitable_fields_url(config: dict[str, Any]) -> str:
    app_token = config["feishu"]["resolved_app_token"]
    table_id = config["feishu"]["table_id"]
    return f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields"


def ensure_bitable_app_token(config: dict[str, Any], token: str) -> str:
    feishu_config = config["feishu"]
    if configured(feishu_config.get("resolved_app_token")):
        return feishu_config["resolved_app_token"]

    if configured(feishu_config.get("app_token"), "bascn_xxx"):
        feishu_config["resolved_app_token"] = feishu_config["app_token"]
        return feishu_config["resolved_app_token"]

    wiki_node_token = feishu_config.get("wiki_node_token")
    if not configured(wiki_node_token):
        raise RunnerError("Set feishu.app_token or feishu.wiki_url in config before running.")

    log_info(config, f"开始通过飞书 wiki 节点解析多维表 app_token：{wiki_node_token}")
    query = urllib.parse.urlencode({"token": wiki_node_token})
    response = request_json("GET", f"{FEISHU_BASE_URL}/wiki/v2/spaces/get_node?{query}", token=token)
    node = response.get("data", {}).get("node", {})
    obj_token = node.get("obj_token")
    obj_type = node.get("obj_type")
    if obj_type and obj_type != "bitable":
        raise RunnerError(f"Wiki node is {obj_type}, expected bitable.")
    if not configured(obj_token):
        raise RunnerError(f"Cannot resolve bitable app_token from wiki node: {wiki_node_token}")

    feishu_config["resolved_app_token"] = obj_token
    log_info(config, "已通过飞书 wiki 链接解析到多维表 app_token。")
    return obj_token


def list_fields(config: dict[str, Any], token: str) -> list[dict[str, Any]]:
    ensure_bitable_app_token(config, token)
    log_info(config, "开始读取飞书多维表字段结构。")
    page_token = ""
    fields: list[dict[str, Any]] = []
    while True:
        query = {"page_size": "100"}
        if page_token:
            query["page_token"] = page_token
        url = f"{bitable_fields_url(config)}?{urllib.parse.urlencode(query)}"
        response = request_json("GET", url, token=token)
        data = response.get("data", {})
        fields.extend(data.get("items", []))
        if not data.get("has_more"):
            log_info(config, f"飞书多维表字段读取完成，共 {len(fields)} 个字段。")
            return fields
        page_token = data.get("page_token", "")


def create_field(config: dict[str, Any], token: str, spec: dict[str, Any]) -> dict[str, Any]:
    ensure_bitable_app_token(config, token)
    payload: dict[str, Any] = {
        "field_name": spec["name"],
        "type": spec["type"],
    }
    options = spec.get("options")
    if options:
        payload["property"] = {
            "options": [{"name": option, "color": index} for index, option in enumerate(options)]
        }
    elif spec.get("property"):
        payload["property"] = spec["property"]
    query = urllib.parse.urlencode({"client_token": str(uuid4())})
    response = request_json("POST", f"{bitable_fields_url(config)}?{query}", token=token, payload=payload)
    return response.get("data", {}).get("field", {})


def update_field(config: dict[str, Any], token: str, field: dict[str, Any], property_value: dict[str, Any]) -> None:
    ensure_bitable_app_token(config, token)
    field_id = field.get("field_id")
    if not field_id:
        raise RunnerError(f"字段缺少 field_id，无法更新：{field.get('field_name')}")
    payload = {
        "field_name": field["field_name"],
        "type": field["type"],
        "property": property_value,
    }
    url = f"{bitable_fields_url(config)}/{field_id}"
    request_json("PUT", url, token=token, payload=payload)


def normalize_name(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def match_name(existing_names: dict[str, str], candidates: list[str]) -> str:
    for candidate in candidates:
        normalized = normalize_name(candidate)
        if normalized in existing_names:
            return existing_names[normalized]
    return ""


def resolve_field_mapping(config: dict[str, Any], token: str, *, create_missing: bool = False) -> None:
    fields = list_fields(config, token)
    existing_names = {
        normalize_name(field.get("field_name", "")): field.get("field_name", "")
        for field in fields
        if field.get("field_name")
    }

    config_fields = config.setdefault("fields", {})
    automation_status_field = config.get("schema", {}).get("automation_status_field")
    missing: list[tuple[str, dict[str, Any]]] = []
    for key, spec in DEFAULT_FIELD_SPECS.items():
        spec = dict(spec)
        if key == "status" and configured(automation_status_field):
            spec["name"] = str(automation_status_field)
            spec["aliases"] = [str(automation_status_field)]
        configured_name = config_fields.get(key)
        candidates = []
        if configured(configured_name) and not (key == "status" and configured(automation_status_field)):
            candidates.append(str(configured_name))
        if key != "status" or not configured(automation_status_field):
            candidates.extend(spec.get("aliases", []))
        else:
            candidates.extend([str(automation_status_field)])
        candidates.append(spec["name"])

        matched = match_name(existing_names, candidates)
        if matched:
            config_fields[key] = matched
        else:
            missing.append((key, spec))

    if missing and not create_missing:
        missing_names = "、".join(spec["name"] for _, spec in missing)
        raise RunnerError(f"飞书表缺少必要字段：{missing_names}。可先运行 --mode sync-schema 自动补齐。")

    for key, spec in missing:
        log_info(config, f"正在创建飞书字段：{spec['name']}")
        created = create_field(config, token, spec)
        config_fields[key] = created.get("field_name") or spec["name"]


def resolve_status_mapping(config: dict[str, Any], token: str) -> None:
    resolve_field_mapping(config, token, create_missing=False)
    fields = list_fields(config, token)
    status_name = config["fields"]["status"]
    status_field = next((field for field in fields if field.get("field_name") == status_name), None)
    options = ((status_field or {}).get("property") or {}).get("options", [])
    option_names = [option.get("name", "") for option in options if option.get("name")]
    existing_options = {normalize_name(name): name for name in option_names}

    statuses = config.setdefault("statuses", {})
    for key, spec in DEFAULT_STATUS_SPECS.items():
        configured_status = statuses.get(key)
        candidates = []
        if configured(configured_status):
            candidates.append(str(configured_status))
        candidates.extend(spec["aliases"])
        candidates.append(spec["name"])
        matched = match_name(existing_options, candidates)
        statuses[key] = matched or str(configured_status or spec["name"])


def ensure_status_options(config: dict[str, Any], token: str) -> None:
    fields = list_fields(config, token)
    status_name = config["fields"]["status"]
    status_field = next((field for field in fields if field.get("field_name") == status_name), None)
    if not status_field:
        raise RunnerError(f"找不到自动化状态字段：{status_name}")
    if status_field.get("type") != 3:
        raise RunnerError(f"自动化状态字段必须是单选字段：{status_name}")

    property_value = dict(status_field.get("property") or {})
    options = list(property_value.get("options") or [])
    option_names = {normalize_name(option.get("name", "")) for option in options}
    missing_options = [
        spec["name"]
        for spec in DEFAULT_STATUS_SPECS.values()
        if normalize_name(spec["name"]) not in option_names
    ]
    if not missing_options:
        return

    for option in missing_options:
        options.append({"name": option, "color": len(options)})
    property_value["options"] = options
    log_info(config, f"正在补齐自动化状态选项：{'、'.join(missing_options)}")
    update_field(config, token, status_field, property_value)


def sync_schema(config: dict[str, Any], token: str) -> None:
    resolve_field_mapping(config, token, create_missing=True)
    ensure_status_options(config, token)
    resolve_status_mapping(config, token)
    log_info(config, "表结构检查完成。缺失字段已补齐；状态选项将按现有选项自动匹配。")


def inspect_schema(config: dict[str, Any], token: str) -> None:
    fields = list_fields(config, token)
    log_info(config, f"当前数据表共有 {len(fields)} 个字段：")
    for field in fields:
        name = field.get("field_name", "")
        field_type = field.get("type", "")
        ui_type = field.get("ui_type", "")
        print(f"- {name}（type={field_type}, ui_type={ui_type}）")
        options = (field.get("property") or {}).get("options", [])
        if options:
            option_names = "、".join(option.get("name", "") for option in options if option.get("name"))
            print(f"  选项：{option_names}")


def list_records(config: dict[str, Any], token: str) -> list[TaskRecord]:
    ensure_bitable_app_token(config, token)
    log_info(config, "开始读取飞书多维表记录。")
    page_size = int(config["feishu"].get("page_size", 50))
    view_id = config["feishu"].get("view_id")
    page_token = ""
    records: list[TaskRecord] = []

    while True:
        query = {"page_size": str(page_size)}
        if view_id:
            query["view_id"] = view_id
        if page_token:
            query["page_token"] = page_token
        url = f"{bitable_records_url(config)}?{urllib.parse.urlencode(query)}"
        try:
            response = request_json("GET", url, token=token)
        except RunnerError as exc:
            if view_id and "WrongViewId" in str(exc):
                log_info(config, f"飞书视图 ID 无效，已自动切换为读取整张表：{view_id}")
                config["feishu"]["view_id"] = ""
                view_id = ""
                page_token = ""
                records = []
                continue
            raise
        data = response["data"]
        for item in data.get("items", []):
            records.append(TaskRecord(record_id=item["record_id"], fields=item.get("fields", {})))
        if not data.get("has_more"):
            log_info(config, f"飞书多维表记录读取完成，共 {len(records)} 条。")
            return records
        page_token = data.get("page_token", "")


def update_record(config: dict[str, Any], token: str, record_id: str, fields: dict[str, Any]) -> None:
    ensure_bitable_app_token(config, token)
    url = f"{bitable_records_url(config)}/{record_id}"
    request_json("PUT", url, token=token, payload={"fields": fields})


def field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or item.get("url") or item))
            else:
                parts.append(str(item))
        return " ".join(parts)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value)
    return str(value)


def status_of(record: TaskRecord, config: dict[str, Any]) -> str:
    return field_text(record.fields.get(config["fields"]["status"]))


def records_with_status(records: list[TaskRecord], config: dict[str, Any], status_key: str) -> list[TaskRecord]:
    expected = config["statuses"][status_key]
    return [record for record in records if status_of(record, config) == expected]


def render_task_prompt(record: TaskRecord, config: dict[str, Any]) -> str:
    fields = config["fields"]
    values = {name: field_text(record.fields.get(field_name)) for name, field_name in fields.items()}
    title = values.get("title", "")
    task_type = values.get("type", "")
    description = values.get("description", "")
    version_doc = values.get("version_doc", "")

    return "\n".join(
        [
            f"飞书任务：{title}",
            f"类型：{task_type}",
            f"描述：{description}",
            f"版本需求文档：{version_doc}",
            "",
            "执行要求：",
            "1. 如果是养鸡庄园的需求、BUG 或优化，必须更新对应版本需求文档。",
            "2. 可以本地跑静态校验，不要本地跑服务测试。",
            "3. 修改完成后提交到本地 main。",
            "4. 不要推送远程，等待飞书状态变为审核通过。",
        ]
    )


def run_command(command: list[str], *, cwd: Path, task_prompt: str = "", timeout_seconds: int | None = None) -> subprocess.CompletedProcess[str]:
    use_prompt_arg = any("{task_prompt}" in part for part in command)
    resolved = [
        part.replace("{task_prompt}", task_prompt).replace("{repo_path}", str(cwd))
        for part in command
    ]
    stdin = None if use_prompt_arg or not task_prompt else task_prompt
    try:
        if stdin is None:
            return subprocess.run(
                resolved,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        completed = subprocess.run(
            resolved,
            cwd=cwd,
            input=stdin.encode("utf-8"),
            text=False,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        return subprocess.CompletedProcess(
            args=completed.args,
            returncode=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError as exc:
        command_name = resolved[0] if resolved else ""
        raise RunnerError(
            f"找不到可执行命令：{command_name}。请把它安装到 PATH，或在 config 的命令数组里写完整 exe 路径。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        command_name = resolved[0] if resolved else ""
        raise RunnerError(f"命令执行超时：{command_name}，超过 {timeout_seconds} 秒未完成。") from exc


def display_command(command: list[str], *, cwd: Path) -> str:
    display_parts = []
    for part in command:
        if "{task_prompt}" in part:
            display_parts.append("<飞书任务内容>")
        else:
            display_parts.append(part.replace("{repo_path}", str(cwd)))
    return " ".join(display_parts)


def now_millis() -> int:
    return int(time.time() * 1000)


def default_state_path() -> Path:
    return Path("state/current_task.json")


def task_state_path(record_id: str) -> Path:
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in record_id)
    return Path("state/tasks") / f"{safe_id}.json"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RunnerError(f"找不到任务状态文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run_checked(command: list[str], *, cwd: Path, task_prompt: str = "", timeout_seconds: int | None = None) -> str:
    completed = run_command(command, cwd=cwd, task_prompt=task_prompt, timeout_seconds=timeout_seconds)
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise RunnerError(f"命令执行失败：{display_command(command, cwd=cwd)}\n{output}")
    return output


def git_output(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    output = (stdout + "\n" + stderr).strip()
    if completed.returncode != 0:
        raise RunnerError(f"git {' '.join(args)} failed\n{output}")
    return stdout.strip()


def ensure_main_branch(config: dict[str, Any], repo_path: Path) -> None:
    main_branch = config["workspace"].get("main_branch", "main")
    current = git_output(["branch", "--show-current"], cwd=repo_path)
    if current != main_branch:
        raise RunnerError(f"Expected branch {main_branch}, current branch is {current}.")


def commit_local(config: dict[str, Any], repo_path: Path, title: str) -> str:
    git_output(["add", "-A"], cwd=repo_path)
    status = git_output(["status", "--porcelain"], cwd=repo_path)
    if not status:
        return git_output(["rev-parse", "HEAD"], cwd=repo_path)

    message = f"chore: complete Feishu task - {title}".strip()
    git_output(["commit", "-m", message], cwd=repo_path)
    return git_output(["rev-parse", "HEAD"], cwd=repo_path)


def local_git_record(config: dict[str, Any], repo_path: Path) -> str:
    branch = git_output(["branch", "--show-current"], cwd=repo_path)
    commit_hash = git_output(["rev-parse", "HEAD"], cwd=repo_path)
    commit_title = git_output(["log", "-1", "--pretty=%s"], cwd=repo_path)
    recorded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join(
        [
            f"分支：{branch}",
            f"提交：{commit_hash}",
            f"标题：{commit_title}",
            f"记录时间：{recorded_at}",
        ]
    )


def execute_tasks(config: dict[str, Any], token: str) -> None:
    repo_path = Path(config["workspace"]["repo_path"]).expanduser().resolve()
    log_info(config, f"开始扫描待处理任务，目标仓库：{repo_path}")
    if not repo_path.exists():
        raise RunnerError(f"Repo path does not exist: {repo_path}")
    ensure_main_branch(config, repo_path)
    resolve_status_mapping(config, token)

    records = records_with_status(list_records(config, token), config, "pending")
    max_tasks = max(1, min(int(config["runner"].get("max_tasks_per_run", 3)), 3))
    selected = records[:max_tasks]
    if not selected:
        log_info(config, "没有待处理任务。")
        return
    log_info(config, f"发现 {len(records)} 个待处理任务，本轮执行 {len(selected)} 个。")

    status_field = config["fields"]["status"]
    result_field = config["fields"]["result"]
    local_commit_field = config["fields"]["local_commit"]
    local_git_record_field = config["fields"].get("local_git_record")
    ai_start_time_field = config["fields"].get("ai_start_time")
    ai_end_time_field = config["fields"].get("ai_end_time")
    command_timeout = int(config.get("runner", {}).get("command_timeout_seconds", 3600))

    for record in selected:
        title = field_text(record.fields.get(config["fields"]["title"]))
        log_info(config, f"领取任务：{title or record.record_id}")
        in_progress_fields: dict[str, Any] = {status_field: config["statuses"]["in_progress"]}
        if ai_start_time_field:
            in_progress_fields[ai_start_time_field] = now_millis()
        update_record(config, token, record.record_id, in_progress_fields)
        try:
            task_prompt = render_task_prompt(record, config)
            log_info(config, f"已把飞书状态改为“开发中”，开始调用 Codex 执行需求，最长等待 {command_timeout} 秒。")
            codex_output = run_checked(
                config["runner"]["development_command"],
                cwd=repo_path,
                task_prompt=task_prompt,
                timeout_seconds=command_timeout,
            )
            log_info(config, "Codex 执行完成。")
            log_command_output(config, "Codex", codex_output)
            static_check = config["runner"].get("static_check_command")
            if static_check:
                log_info(config, "开始运行静态校验。")
                static_output = run_checked(static_check, cwd=repo_path, timeout_seconds=command_timeout)
                log_info(config, "静态校验通过。")
                log_command_output(config, "静态校验", static_output)
            log_info(config, "开始提交到本地 main。")
            commit_hash = commit_local(config, repo_path, title)
            finished_fields: dict[str, Any] = {
                status_field: config["statuses"]["review"],
                result_field: "本地开发完成，已提交到本地 main，等待人工审核。",
                local_commit_field: commit_hash,
            }
            if local_git_record_field:
                finished_fields[local_git_record_field] = local_git_record(config, repo_path)
            if ai_end_time_field:
                finished_fields[ai_end_time_field] = now_millis()
            update_record(config, token, record.record_id, finished_fields)
            log_info(config, f"任务已完成本地提交，等待人工审核：{title or record.record_id}，commit：{commit_hash}")
        except Exception as exc:
            original_error = str(exc)
            log_error(f"任务执行失败，准备回写飞书状态：{title or record.record_id}；原始原因：{original_error}")
            failed_fields: dict[str, Any] = {
                status_field: config["statuses"]["failed"],
                result_field: original_error,
            }
            if ai_end_time_field:
                failed_fields[ai_end_time_field] = now_millis()
            try:
                update_record(config, token, record.record_id, failed_fields)
                log_error(f"任务执行失败，已回写飞书状态为“执行失败”：{title or record.record_id}；原因：{original_error}")
            except Exception as update_exc:
                log_error(
                    "任务执行失败，且回写飞书失败；"
                    f"任务：{title or record.record_id}；原始原因：{original_error}；回写失败原因：{update_exc}"
                )
            raise RunnerError(original_error) from exc


def push_approved(config: dict[str, Any], token: str) -> None:
    repo_path = Path(config["workspace"]["repo_path"]).expanduser().resolve()
    log_info(config, f"开始扫描审核通过任务，目标仓库：{repo_path}")
    ensure_main_branch(config, repo_path)
    resolve_status_mapping(config, token)

    records = records_with_status(list_records(config, token), config, "approved")
    if not records:
        log_info(config, "没有审核通过的任务需要推送。")
        return
    log_info(config, f"发现 {len(records)} 个审核通过任务，开始推送远程。")

    remote = config["workspace"].get("remote", "origin")
    main_branch = config["workspace"].get("main_branch", "main")
    push_output = git_output(["push", remote, main_branch], cwd=repo_path)

    status_field = config["fields"]["status"]
    remote_push_field = config["fields"]["remote_push"]
    for record in records:
        local_commit = field_text(record.fields.get(config["fields"].get("local_commit", "")))
        remote_result = push_output or f"pushed {remote}/{main_branch}"
        if local_commit:
            remote_result = f"{remote_result}\n已推送本地提交：{local_commit}"
        update_record(
            config,
            token,
            record.record_id,
            {
                status_field: config["statuses"]["pushed"],
                remote_push_field: remote_result,
            },
        )
    log_info(config, f"远程推送完成，已回写 {len(records)} 条飞书记录为“已推送”。")
    log_command_output(config, "Git push", push_output)


def print_records(config: dict[str, Any], token: str) -> None:
    resolve_status_mapping(config, token)
    records = list_records(config, token)
    log_info(config, f"读取到 {len(records)} 条飞书记录。")
    if not records:
        return
    title_field = config["fields"]["title"]
    for record in records:
        print(f"{record.record_id}\t{status_of(record, config)}\t{field_text(record.fields.get(title_field))}")


def claim_next_task(config: dict[str, Any], token: str, state_path: Path) -> None:
    task = claim_task_record(config, token, state_path)
    if not task:
        log_info(config, "没有待处理任务。")
        write_json(state_path, {"claimed": False, "message": "没有待处理任务"})
        return
    log_info(config, f"已领取任务并写入状态文件：{task['title'] or task['record_id']}")
    print(json.dumps(task, ensure_ascii=False, indent=2))


def claim_task_record(config: dict[str, Any], token: str, state_path: Path | None = None) -> dict[str, Any] | None:
    repo_path = Path(config["workspace"]["repo_path"]).expanduser().resolve()
    log_info(config, f"开始领取下一条待处理任务，目标仓库：{repo_path}")
    if not repo_path.exists():
        raise RunnerError(f"Repo path does not exist: {repo_path}")
    resolve_status_mapping(config, token)

    records = records_with_status(list_records(config, token), config, "pending")
    if not records:
        return None

    record = records[0]
    title = field_text(record.fields.get(config["fields"]["title"]))
    status_field = config["fields"]["status"]
    ai_start_time_field = config["fields"].get("ai_start_time")
    update_fields: dict[str, Any] = {status_field: config["statuses"]["in_progress"]}
    if ai_start_time_field:
        update_fields[ai_start_time_field] = now_millis()
    update_record(config, token, record.record_id, update_fields)

    task_prompt = render_task_prompt(record, config)
    task = {
        "claimed": True,
        "record_id": record.record_id,
        "title": title,
        "repo_path": str(repo_path),
        "main_branch": config["workspace"].get("main_branch", "main"),
        "static_check_command": config["runner"].get("static_check_command"),
        "task_prompt": task_prompt,
    }
    if state_path is not None:
        write_json(state_path, task)
    return task


def finish_task(config: dict[str, Any], token: str, state_path: Path, message: str) -> None:
    task = read_json(state_path)
    if not task.get("claimed"):
        log_info(config, "状态文件中没有已领取任务，无需回写完成状态。")
        return
    resolve_status_mapping(config, token)

    repo_path = Path(task.get("repo_path") or config["workspace"]["repo_path"]).expanduser().resolve()
    commit_hash = git_output(["rev-parse", "HEAD"], cwd=repo_path)
    status_field = config["fields"]["status"]
    result_field = config["fields"]["result"]
    local_commit_field = config["fields"]["local_commit"]
    ai_end_time_field = config["fields"].get("ai_end_time")
    update_fields: dict[str, Any] = {
        status_field: config["statuses"]["review"],
        result_field: message or "本地开发完成，已提交到本地 main，等待人工审核。",
        local_commit_field: commit_hash,
    }
    local_git_record_field = config["fields"].get("local_git_record")
    if local_git_record_field:
        update_fields[local_git_record_field] = local_git_record(config, repo_path)
    if ai_end_time_field:
        update_fields[ai_end_time_field] = now_millis()
    update_record(config, token, task["record_id"], update_fields)
    task["finished"] = True
    task["commit_hash"] = commit_hash
    write_json(state_path, task)
    log_info(config, f"已回写任务为“待人工审核”：{task.get('title') or task['record_id']}，commit：{commit_hash}")


def fail_task(config: dict[str, Any], token: str, state_path: Path, message: str) -> None:
    task = read_json(state_path)
    if not task.get("claimed"):
        log_info(config, "状态文件中没有已领取任务，无需回写失败状态。")
        return
    resolve_status_mapping(config, token)

    status_field = config["fields"]["status"]
    result_field = config["fields"]["result"]
    ai_end_time_field = config["fields"].get("ai_end_time")
    update_fields: dict[str, Any] = {
        status_field: config["statuses"]["failed"],
        result_field: message or "Codex 自动化执行失败。",
    }
    if ai_end_time_field:
        update_fields[ai_end_time_field] = now_millis()
    update_record(config, token, task["record_id"], update_fields)
    task["failed"] = True
    task["error"] = message
    write_json(state_path, task)
    log_info(config, f"已回写任务为“执行失败”：{task.get('title') or task['record_id']}")


def process_claimed_task(config: dict[str, Any], state_path: Path) -> str:
    task = read_json(state_path)
    if not task.get("claimed"):
        return "没有已领取任务"

    repo_path = Path(task["repo_path"]).expanduser().resolve()
    command_timeout = int(config.get("runner", {}).get("command_timeout_seconds", 3600))
    task_prompt = str(task.get("task_prompt", ""))

    try:
        log_info(config, f"后台异步执行 Codex 任务：{task.get('title') or task['record_id']}，最长等待 {command_timeout} 秒。")
        codex_output = run_checked(
            config["runner"]["development_command"],
            cwd=repo_path,
            task_prompt=task_prompt,
            timeout_seconds=command_timeout,
        )
        log_info(config, "Codex 执行完成。")
        log_command_output(config, "Codex", codex_output)

        static_check = task.get("static_check_command") or config["runner"].get("static_check_command")
        if static_check:
            log_info(config, "开始运行静态校验。")
            static_output = run_checked(static_check, cwd=repo_path, timeout_seconds=command_timeout)
            log_info(config, "静态校验通过。")
            log_command_output(config, "静态校验", static_output)

        log_info(config, "开始提交到本地 main。")
        title = str(task.get("title") or task["record_id"])
        commit_hash = commit_local(config, repo_path, title)
        token = get_tenant_access_token(config)
        finish_task(config, token, state_path, "本地开发完成，已提交到本地 main，等待人工审核。")
        return commit_hash
    except Exception as exc:
        try:
            token = get_tenant_access_token(config)
            fail_task(config, token, state_path, str(exc))
        except Exception as update_exc:
            log_error(
                "任务失败后回写飞书也失败；"
                f"任务：{task.get('title') or task['record_id']}；原始原因：{exc}；回写失败原因：{update_exc}"
            )
        raise


def run_watch(config: dict[str, Any]) -> None:
    automation = config.get("automation", {})
    interval = max(10, int(automation.get("poll_interval_seconds", 60)))
    execute_pending = bool(automation.get("execute_pending", True))
    push_after_approval = bool(automation.get("push_approved", True))
    max_parallel = max(1, min(int(automation.get("max_parallel_tasks", 3)), 3))

    log_info(config, f"后台 worker 已启动，每 {interval} 秒轮询一次飞书。最多并行处理 {max_parallel} 个任务。按 Ctrl+C 停止。")
    active_futures: dict[Future[str], Path] = {}
    executor = ThreadPoolExecutor(max_workers=max_parallel)
    while True:
        finished_futures = [future for future in active_futures if future.done()]
        for future in finished_futures:
            state_file = active_futures.pop(future)
            try:
                commit_hash = future.result()
                task = read_json(state_file)
                log_info(config, f"异步 Codex 任务完成：{task.get('title') or state_file.stem}，本地提交：{commit_hash}")
            except Exception as exc:
                log_error(f"异步 Codex 任务结束时出错：{state_file}；原因：{exc}")

        log_info(config, "开始轮询飞书多维表。")
        try:
            token = get_tenant_access_token(config)
            if push_after_approval:
                push_approved(config, token)
            if execute_pending:
                available_slots = max_parallel - len(active_futures)
                if available_slots > 0:
                    for _ in range(available_slots):
                        task = claim_task_record(config, token, None)
                        if not task:
                            break
                        state_file = task_state_path(task["record_id"])
                        write_json(state_file, task)
                        future = executor.submit(process_claimed_task, config, state_file)
                        active_futures[future] = state_file
                        log_info(config, f"Codex 任务已异步启动，后台处理中：{task.get('title') or task['record_id']}")
                    if not active_futures:
                        log_info(config, "本轮没有需要异步处理的任务。")
                else:
                    log_info(config, f"已有 {len(active_futures)} 个 Codex 任务在后台执行，达到并行上限。")
        except Exception as exc:
            log_error(f"本轮轮询失败：{exc}")
        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Feishu Bitable-driven Codex automation.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "list",
            "execute",
            "push-approved",
            "watch",
            "inspect-schema",
            "sync-schema",
            "claim-next",
            "finish-task",
            "fail-task",
        ],
    )
    parser.add_argument("--state-file", type=Path, default=default_state_path())
    parser.add_argument("--message", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        if args.mode == "watch":
            run_watch(config)
            return 0

        token = get_tenant_access_token(config)
        if args.mode == "inspect-schema":
            inspect_schema(config, token)
        elif args.mode == "sync-schema":
            sync_schema(config, token)
        elif args.mode == "list":
            print_records(config, token)
        elif args.mode == "execute":
            execute_tasks(config, token)
        elif args.mode == "push-approved":
            push_approved(config, token)
        elif args.mode == "claim-next":
            claim_next_task(config, token, args.state_file)
        elif args.mode == "finish-task":
            finish_task(config, token, args.state_file, args.message)
        elif args.mode == "fail-task":
            fail_task(config, token, args.state_file, args.message)
        return 0
    except KeyboardInterrupt:
        print("已停止。")
        return 0
    except Exception as exc:
        log_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
