#!/usr/bin/env python3
"""Feishu Bitable task runner for a human-gated Codex workflow."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
Command = str | list[str]
CODEX_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
VERSION_DOC_BEGIN = "[VERSION_DOC_BEGIN]"
VERSION_DOC_END = "[VERSION_DOC_END]"
ACTIVE_CONFIG: dict[str, Any] | None = None


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
    "deployment_result": {
        "name": "部署结果",
        "type": 1,
        "aliases": ["部署结果", "部署日志", "服务器部署", "上线结果", "发布结果"],
        "optional": True,
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


def log_file_enabled(config: dict[str, Any]) -> bool:
    logging_config = config.get("logging", {})
    return bool(logging_config.get("file_enabled", logging_config.get("enabled", True)))


def log_file_path(config: dict[str, Any]) -> Path:
    configured_path = str(config.get("logging", {}).get("file_path") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    return Path("logs") / "feishu-runner.log"


def write_log_file(config: dict[str, Any], line: str) -> None:
    if not log_file_enabled(config):
        return
    path = log_file_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(f"{line}\n")
    except OSError as exc:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 错误：日志文件写入失败：{path}；原因：{exc}", file=sys.stderr)


def command_output_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("logging", {}).get("command_output", False))


def verbose_log_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("logging", {}).get("verbose", False))


def log_info(config: dict[str, Any], message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    if log_enabled(config):
        print(line)
    write_log_file(config, line)


def log_debug(config: dict[str, Any], message: str) -> None:
    if verbose_log_enabled(config):
        log_info(config, message)


def log_error(message: str, config: dict[str, Any] | None = None) -> None:
    resolved_config = config or ACTIVE_CONFIG
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 错误：{message}"
    print(line, file=sys.stderr)
    if resolved_config is not None:
        write_log_file(resolved_config, line)


def log_command_output(config: dict[str, Any], title: str, output: str) -> None:
    if not command_output_enabled(config) or not output.strip():
        return
    log_info(config, f"{title}输出：")
    print(output.strip())
    for line in output.strip().splitlines():
        write_log_file(config, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {title}> {line}")


def runtime_cache(config: dict[str, Any]) -> dict[str, Any]:
    return config.setdefault("_runtime", {})


def schema_loaded(config: dict[str, Any]) -> bool:
    return bool(runtime_cache(config).get("schema_loaded"))


def mark_schema_loaded(config: dict[str, Any]) -> None:
    runtime_cache(config)["schema_loaded"] = True


def schema_cache_path(config: dict[str, Any]) -> Path:
    configured_path = config.get("schema", {}).get("cache_path", "state/feishu_schema_cache.json")
    return Path(str(configured_path)).expanduser()


def schema_cache_key(config: dict[str, Any]) -> str:
    feishu_config = config.get("feishu", {})
    app_ref = (
        feishu_config.get("resolved_app_token")
        or feishu_config.get("app_token")
        or feishu_config.get("wiki_node_token")
        or feishu_config.get("wiki_url")
        or ""
    )
    table_id = feishu_config.get("table_id", "")
    automation_status_field = config.get("schema", {}).get("automation_status_field", "")
    return "|".join(str(part) for part in [app_ref, table_id, automation_status_field])


def load_schema_cache_file(config: dict[str, Any]) -> dict[str, Any]:
    path = schema_cache_path(config)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log_debug(config, f"表结构缓存不可用，将重新读取飞书字段：{exc}")
        return {}


def save_schema_cache_file(config: dict[str, Any], data: dict[str, Any]) -> None:
    path = schema_cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_schema_from_cache(config: dict[str, Any]) -> bool:
    if schema_loaded(config):
        return True

    data = load_schema_cache_file(config)
    entry = data.get(schema_cache_key(config))
    if not isinstance(entry, dict):
        return False

    cached_fields = entry.get("fields")
    cached_statuses = entry.get("statuses")
    if not isinstance(cached_fields, dict) or not isinstance(cached_statuses, dict):
        return False

    config.setdefault("fields", {}).update(cached_fields)
    config.setdefault("statuses", {}).update(cached_statuses)
    mark_schema_loaded(config)
    log_debug(config, "已复用本地表结构缓存，本轮不再读取飞书字段结构。")
    return True


def save_schema_to_cache(config: dict[str, Any]) -> None:
    data = load_schema_cache_file(config)
    data[schema_cache_key(config)] = {
        "fields": config.get("fields", {}),
        "statuses": config.get("statuses", {}),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_schema_cache_file(config, data)
    log_info(config, f"已写入表结构缓存：{schema_cache_path(config)}")


def truncate_text(text: str, limit: int = 3500) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned

    head = max(1200, limit // 2)
    tail = max(800, limit - head - 40)
    omitted = len(cleaned) - (head + tail)
    return "\n".join(
        [
            cleaned[:head].rstrip(),
            f"...（已截断 {omitted} 字）...",
            cleaned[-tail:].lstrip(),
        ]
    )


def format_review_result(
    message: str,
    *,
    commit_hash: str = "",
    codex_output: str = "",
    static_output: str = "",
    local_git_record_text: str = "",
) -> str:
    sections: list[str] = []
    summary = message.strip()
    if summary:
        sections.append(summary)
    if commit_hash:
        sections.append(f"提交：{commit_hash}")
    if codex_output.strip():
        sections.append(f"Codex 输出：\n{truncate_text(codex_output)}")
    if static_output.strip():
        sections.append(f"静态校验：\n{truncate_text(static_output)}")
    if local_git_record_text.strip():
        sections.append(f"本地 Git 记录：\n{truncate_text(local_git_record_text, 1400)}")
    return "\n\n".join(sections)


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


def request_binary(url: str, *, token: str) -> tuple[bytes, str]:
    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_type = response.headers.get("content-type", "")
            return response.read(), content_type
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RunnerError(f"HTTP {exc.code} from Feishu media download: {details}") from exc
    except urllib.error.URLError as exc:
        raise RunnerError(f"Cannot download Feishu media: {exc}") from exc


def get_tenant_access_token(config: dict[str, Any]) -> str:
    feishu_config = config.get("feishu", {})
    app_id = feishu_config.get("app_id") or os.environ.get("FEISHU_APP_ID")
    app_secret = feishu_config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RunnerError("Set feishu.app_id and feishu.app_secret in config before running.")

    log_debug(config, "开始获取飞书 tenant_access_token。")
    response = request_json(
        "POST",
        f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    log_debug(config, "飞书 tenant_access_token 获取成功。")
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

    log_debug(config, f"开始通过飞书 wiki 节点解析多维表 app_token：{wiki_node_token}")
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
    log_debug(config, "已通过飞书 wiki 链接解析到多维表 app_token。")
    return obj_token


def list_fields(config: dict[str, Any], token: str) -> list[dict[str, Any]]:
    ensure_bitable_app_token(config, token)
    log_debug(config, "开始读取飞书多维表字段结构。")
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
            log_debug(config, f"飞书多维表字段读取完成，共 {len(fields)} 个字段。")
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
        elif create_missing or not spec.get("optional"):
            missing.append((key, spec))
        else:
            config_fields.pop(key, None)

    if missing and not create_missing:
        missing_names = "、".join(spec["name"] for _, spec in missing)
        raise RunnerError(f"飞书表缺少必要字段：{missing_names}。可先运行 --mode sync-schema 自动补齐。")

    for key, spec in missing:
        log_info(config, f"正在创建飞书字段：{spec['name']}")
        created = create_field(config, token, spec)
        config_fields[key] = created.get("field_name") or spec["name"]


def resolve_status_mapping(config: dict[str, Any], token: str, *, refresh: bool = False) -> None:
    if schema_loaded(config) and not refresh:
        return
    ensure_bitable_app_token(config, token)
    if not refresh and load_schema_from_cache(config):
        return

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
    mark_schema_loaded(config)
    save_schema_to_cache(config)


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
    resolve_status_mapping(config, token, refresh=True)
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
    log_debug(config, "开始读取飞书多维表记录。")
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
                log_debug(config, f"飞书视图 ID 无效，已自动切换为读取整张表：{view_id}")
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
            log_debug(config, f"飞书多维表记录读取完成，共 {len(records)} 条。")
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


def safe_file_part(value: str, fallback: str = "file") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value.strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def attachment_dir(record_id: str) -> Path:
    return Path("state/attachments") / safe_file_part(record_id, "record")


def configured_image_field_names(config: dict[str, Any]) -> set[str]:
    fields = config.get("fields", {})
    names = config.get("runner", {}).get("image_fields", [])
    if isinstance(names, str):
        names = [names]
    result: set[str] = set()
    for name in names if isinstance(names, list) else []:
        text = str(name).strip()
        if not text:
            continue
        result.add(text)
        mapped = fields.get(text)
        if mapped:
            result.add(str(mapped))
    return result


def looks_like_image_attachment(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    file_token = item.get("file_token") or item.get("token")
    if not file_token:
        return False
    media_type = str(item.get("type") or item.get("mime_type") or item.get("mime") or "").lower()
    if media_type.startswith("image/"):
        return True
    name = str(item.get("name") or "")
    guessed_type = mimetypes.guess_type(name)[0] or ""
    return guessed_type.startswith("image/")


def image_attachments(record: TaskRecord, config: dict[str, Any]) -> list[dict[str, Any]]:
    allowed_fields = configured_image_field_names(config)
    attachments: list[dict[str, Any]] = []
    for field_name, value in record.fields.items():
        if allowed_fields and field_name not in allowed_fields:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if looks_like_image_attachment(item):
                attachment = dict(item)
                attachment["_field_name"] = field_name
                attachments.append(attachment)
    return attachments


def image_download_url(attachment: dict[str, Any]) -> str:
    url = str(attachment.get("url") or attachment.get("tmp_url") or "").strip()
    if url:
        return url
    file_token = str(attachment.get("file_token") or attachment.get("token") or "")
    if not file_token:
        raise RunnerError(f"图片附件缺少 file_token：{attachment}")
    return f"{FEISHU_BASE_URL}/drive/v1/medias/{urllib.parse.quote(file_token)}/download"


def image_file_name(attachment: dict[str, Any], index: int, content_type: str = "") -> str:
    name = safe_file_part(str(attachment.get("name") or ""), f"image-{index}")
    suffix = Path(name).suffix
    if not suffix:
        suffix = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".png"
        name = f"{name}{suffix}"
    return name


def download_task_images(config: dict[str, Any], token: str, record: TaskRecord) -> list[str]:
    if not bool(config.get("runner", {}).get("include_images", True)):
        return []

    attachments = image_attachments(record, config)
    if not attachments:
        return []

    target_dir = attachment_dir(record.record_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index, attachment in enumerate(attachments, start=1):
        data, content_type = request_binary(image_download_url(attachment), token=token)
        file_name = image_file_name(attachment, index, content_type)
        path = target_dir / f"{index:02d}-{file_name}"
        path.write_bytes(data)
        paths.append(str(path.resolve()))

    log_info(config, f"已下载 {len(paths)} 张飞书图片附件：{record.record_id}")
    return paths


def render_task_prompt(
    record: TaskRecord,
    config: dict[str, Any],
    *,
    task_branch: str = "",
    worktree_mode: bool = False,
) -> str:
    fields = config["fields"]
    values = {name: field_text(record.fields.get(field_name)) for name, field_name in fields.items()}
    title = values.get("title", "")
    task_type = values.get("type", "")
    description = values.get("description", "")
    version_doc = values.get("version_doc", "")

    if worktree_mode:
        execution_lines = [
            "1. 如果是养鸡庄园的需求、BUG 或优化，对应版本需求文档由自动化在合并后补充，不要直接修改版本需求文档文件。",
            "2. 可以本地跑静态校验，不要本地跑服务测试。",
            f"3. 修改完成后提交到当前任务分支 {task_branch}，不要切换分支，不要创建其他分支，不要推送远程仓库。",
            "4. 开发完成后在回复末尾用 [VERSION_DOC_BEGIN] 和 [VERSION_DOC_END] 包裹一段 Markdown（以 `### ` 开头，包含目标、范围和验收标准），自动化会把它写入对应版本需求文档。",
            "5. 不要推送远程，等待飞书状态变为审核通过。",
        ]
    else:
        execution_lines = [
            "1. 如果是养鸡庄园的需求、BUG 或优化，必须更新对应版本需求文档。",
            "2. 可以本地跑静态校验，不要本地跑服务测试。",
            "3. 修改完成后提交到本地 main。",
            "4. 不要推送远程，等待飞书状态变为审核通过。",
        ]

    lines = [
        f"飞书任务：{title}",
        f"类型：{task_type}",
        f"描述：{description}",
        f"版本需求文档：{version_doc}",
        "",
        "执行要求：",
        *execution_lines,
    ]
    if image_attachments(record, config):
        lines.extend(["", "飞书记录包含图片附件，图片已通过 Codex --image 参数随本任务一并提供，请结合图片内容处理问题。"])
    return "\n".join(lines)


def resolve_command_part(part: str, *, cwd: Path, task_prompt: str = "") -> str:
    return part.replace("{task_prompt}", task_prompt).replace("{repo_path}", str(cwd))


def run_command(command: Command, *, cwd: Path, task_prompt: str = "", timeout_seconds: int | None = None) -> subprocess.CompletedProcess[str]:
    if isinstance(command, str):
        use_prompt_arg = "{task_prompt}" in command
        resolved_text = resolve_command_part(command, cwd=cwd, task_prompt=task_prompt)
        stdin_text = None if use_prompt_arg or not task_prompt else task_prompt
        try:
            return subprocess.run(
                resolved_text,
                cwd=cwd,
                input=stdin_text,
                text=True,
                shell=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"命令执行超时：{resolved_text}，超过 {timeout_seconds} 秒未完成。") from exc

    use_prompt_arg = any("{task_prompt}" in part for part in command)
    resolved = [resolve_command_part(part, cwd=cwd, task_prompt=task_prompt) for part in command]
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


def display_command(command: Command, *, cwd: Path) -> str:
    if isinstance(command, str):
        if "{task_prompt}" in command:
            return command.replace("{task_prompt}", "<飞书任务内容>").replace("{repo_path}", str(cwd))
        return command.replace("{repo_path}", str(cwd))

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


STATE_TEXT_LIMITS = {
    "codex_output": 12000,
    "static_output": 8000,
    "ai_commit_output": 8000,
    "previous_result": 8000,
    "error": 8000,
}


def compact_json_data(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(item_key): compact_json_data(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [compact_json_data(item) for item in value]
    if isinstance(value, str) and key in STATE_TEXT_LIMITS:
        return truncate_text(value, STATE_TEXT_LIMITS[key])
    return value


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compact_json_data(data), ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RunnerError(f"找不到任务状态文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def extract_session_id(value: str) -> str:
    match = CODEX_SESSION_ID_RE.search(value)
    return match.group(0) if match else ""


def codex_session_id_from_path(path: Path) -> str:
    return extract_session_id(path.name)


def codex_session_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            for _ in range(8):
                line = file.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "session_meta":
                    payload = event.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def same_path(left: str | Path, right: str | Path) -> bool:
    left_text = str(left).replace("\\", "/").rstrip("/").lower()
    right_text = str(right).replace("\\", "/").rstrip("/").lower()
    return left_text == right_text


def codex_session_snapshot(cwd: Path) -> set[str]:
    sessions_dir = codex_home() / "sessions"
    if not sessions_dir.exists():
        return set()

    ids: set[str] = set()
    for path in sessions_dir.rglob("*.jsonl"):
        session_id = codex_session_id_from_path(path)
        if not session_id:
            continue
        meta = codex_session_meta(path)
        meta_cwd = meta.get("cwd")
        if not meta_cwd or same_path(str(meta_cwd), cwd):
            ids.add(session_id)
    return ids


def latest_codex_session_id(cwd: Path, previous_ids: set[str], started_at: float) -> str:
    sessions_dir = codex_home() / "sessions"
    if not sessions_dir.exists():
        return ""

    candidates: list[tuple[float, str]] = []
    for path in sessions_dir.rglob("*.jsonl"):
        session_id = codex_session_id_from_path(path)
        if not session_id or session_id in previous_ids:
            continue
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        if modified_at + 5 < started_at:
            continue
        meta = codex_session_meta(path)
        meta_cwd = meta.get("cwd")
        if meta_cwd and not same_path(str(meta_cwd), cwd):
            continue
        candidates.append((modified_at, session_id))

    if not candidates:
        return ""
    candidates.sort()
    return candidates[-1][1]


def recent_codex_session_id_by_title(title: str) -> str:
    title = title.strip()
    if not title:
        return ""

    index_path = codex_home() / "session_index.jsonl"
    if not index_path.exists():
        return ""

    matched_session_id = ""
    try:
        with index_path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                thread_name = str(entry.get("thread_name") or "")
                if not thread_name:
                    continue
                if title in thread_name or thread_name in title:
                    session_id = extract_session_id(str(entry.get("id") or ""))
                    if session_id:
                        matched_session_id = session_id
    except OSError:
        return ""
    return matched_session_id


def extract_codex_session_id_from_output(output: str) -> str:
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            session_id = extract_session_id(line)
            if session_id:
                return session_id
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            session_id = str(payload.get("id") or payload.get("session_id") or "")
            if extract_session_id(session_id):
                return extract_session_id(session_id)
        session_id = str(event.get("session_id") or event.get("id") or "")
        if extract_session_id(session_id):
            return extract_session_id(session_id)
    return ""


def build_codex_resume_command(command: Command, session_id: str) -> Command:
    if not session_id:
        return command
    if isinstance(command, str):
        return command

    parts = [str(part) for part in command]
    if "exec" not in parts:
        return command

    prompt_index = len(parts)
    if parts and (parts[-1] == "-" or "{task_prompt}" in parts[-1]):
        prompt_index = len(parts) - 1
    return [*parts[:prompt_index], "resume", session_id, *parts[prompt_index:]]


def image_args(image_paths: list[str]) -> list[str]:
    args: list[str] = []
    for path in image_paths:
        if path:
            args.extend(["--image", path])
    return args


def build_codex_image_command(command: Command, image_paths: list[str]) -> Command:
    args = image_args(image_paths)
    if not args:
        return command
    if isinstance(command, str):
        if "{image_args}" in command:
            return command.replace("{image_args}", shell_join(args))
        return command

    parts = [str(part) for part in command]
    if "resume" in parts:
        resume_index = parts.index("resume")
        return [*parts[: resume_index + 1], *args, *parts[resume_index + 1 :]]

    if "exec" not in parts:
        return command

    prompt_index = len(parts)
    if parts and (parts[-1] == "-" or "{task_prompt}" in parts[-1]):
        prompt_index = len(parts) - 1
    return [*parts[:prompt_index], *args, *parts[prompt_index:]]


def is_codex_exec_command(parts: list[str]) -> bool:
    if not parts or "exec" not in parts:
        return False
    command_name = Path(parts[0]).name.lower()
    return command_name in {"codex", "codex.exe"}


def pop_option_value(parts: list[str], option_names: set[str]) -> tuple[list[str], str]:
    cleaned: list[str] = []
    value = ""
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in option_names:
            if index + 1 < len(parts) and not parts[index + 1].startswith("-"):
                value = parts[index + 1]
                index += 2
            else:
                index += 1
            continue
        cleaned.append(part)
        index += 1
    return cleaned, value


def normalize_codex_development_command(command: Command) -> Command:
    if isinstance(command, str):
        return command

    parts = [str(part) for part in command]
    if not is_codex_exec_command(parts):
        return command

    bypass_flag = "--dangerously-bypass-approvals-and-sandbox"
    has_bypass = bypass_flag in parts
    parts, approval = pop_option_value(parts, {"--ask-for-approval", "-a"})
    parts, sandbox = pop_option_value(parts, {"--sandbox", "-s"})
    parts, cwd_arg = pop_option_value(parts, {"--cd", "-C"})

    exec_index = parts.index("exec")
    global_args: list[str] = ["--ask-for-approval", approval or "never"]
    if not has_bypass:
        if sandbox not in {"workspace-write", "danger-full-access"}:
            sandbox = "workspace-write"
        global_args.extend(["--sandbox", sandbox])
    if cwd_arg:
        global_args.extend(["--cd", cwd_arg])

    return [*parts[:exec_index], *global_args, *parts[exec_index:]]


def task_retry_enabled(task: dict[str, Any]) -> bool:
    return bool(task.get("retry_requested") and task.get("codex_session_id"))


def development_command_for_task(config: dict[str, Any], task: dict[str, Any]) -> Command:
    command = normalize_codex_development_command(config["runner"]["development_command"])
    if task_retry_enabled(task):
        command = build_codex_resume_command(command, str(task.get("codex_session_id") or ""))
    return build_codex_image_command(command, [str(path) for path in task.get("image_paths") or []])


def run_codex_task(config: dict[str, Any], task: dict[str, Any], repo_path: Path, timeout_seconds: int) -> str:
    command = development_command_for_task(config, task)
    previous_ids = codex_session_snapshot(repo_path)
    started_at = time.time()
    try:
        output = run_checked(
            command,
            cwd=repo_path,
            task_prompt=str(task.get("task_prompt") or ""),
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        session_id = latest_codex_session_id(repo_path, previous_ids, started_at)
        if session_id:
            task["codex_session_id"] = session_id
        raise

    session_id = extract_codex_session_id_from_output(output) or latest_codex_session_id(repo_path, previous_ids, started_at)
    if session_id:
        task["codex_session_id"] = session_id
    return output


def run_codex_followup(
    config: dict[str, Any],
    task: dict[str, Any],
    repo_path: Path,
    prompt: str,
    timeout_seconds: int,
) -> str:
    if task.get("codex_session_id"):
        task["retry_requested"] = True
    previous_prompt = task.get("task_prompt")
    task["task_prompt"] = prompt
    try:
        return run_codex_task(config, task, repo_path, timeout_seconds)
    finally:
        task["task_prompt"] = previous_prompt


def run_checked(command: Command, *, cwd: Path, task_prompt: str = "", timeout_seconds: int | None = None) -> str:
    completed = run_command(command, cwd=cwd, task_prompt=task_prompt, timeout_seconds=timeout_seconds)
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise RunnerError(f"命令执行失败：{display_command(command, cwd=cwd)}\n{output}")
    return output


def deployment_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("deployment", {}).get("enabled", False))


def command_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def deploy_placeholders(config: dict[str, Any], repo_path: Path, pushed_commits: list[str]) -> dict[str, str]:
    remote = str(config["workspace"].get("remote", "origin"))
    branch = str(config["workspace"].get("main_branch", "main"))
    return {
        "repo_path": str(repo_path),
        "remote": remote,
        "branch": branch,
        "main_branch": branch,
        "pushed_commits": ",".join(commit for commit in pushed_commits if commit),
    }


def replace_placeholders(value: str, placeholders: dict[str, str]) -> str:
    for key, replacement in placeholders.items():
        value = value.replace(f"{{{key}}}", replacement)
    return value


def normalize_deploy_command(command: Any, placeholders: dict[str, str]) -> Command:
    if isinstance(command, str):
        return replace_placeholders(command, placeholders)
    if isinstance(command, list):
        return [replace_placeholders(str(part), placeholders) for part in command]
    raise RunnerError(f"部署命令格式不支持：{command!r}")


def custom_deploy_commands(deployment: dict[str, Any], placeholders: dict[str, str]) -> list[Command]:
    commands = deployment.get("commands")
    if isinstance(commands, list) and commands:
        return [normalize_deploy_command(command, placeholders) for command in commands]

    command = deployment.get("command")
    if command:
        return [normalize_deploy_command(command, placeholders)]
    return []


def docker_compose_prefix(docker_config: dict[str, Any]) -> list[str]:
    prefix = command_items(docker_config.get("compose_command") or ["docker", "compose"])
    compose_files = docker_config.get("compose_files") or docker_config.get("compose_file") or []
    if isinstance(compose_files, str):
        compose_files = [compose_files]
    for compose_file in compose_files:
        if compose_file:
            prefix.extend(["-f", str(compose_file)])
    project_name = docker_config.get("project_name")
    if project_name:
        prefix.extend(["-p", str(project_name)])
    return prefix


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part))


def default_docker_deploy_commands(config: dict[str, Any], repo_path: Path, placeholders: dict[str, str]) -> list[Command]:
    deployment = config.get("deployment", {})
    docker_config = deployment.get("docker", {})
    remote = placeholders["remote"]
    branch = placeholders["branch"]
    server_path = str(docker_config.get("repo_path") or docker_config.get("server_repo_path") or "{repo_path}")
    server_path = replace_placeholders(server_path, placeholders)

    compose_prefix = docker_compose_prefix(docker_config)
    services = command_items(docker_config.get("services", []))
    up_command = [*compose_prefix, "up", "-d"]
    if bool(docker_config.get("build", True)):
        up_command.append("--build")
    if bool(docker_config.get("remove_orphans", True)):
        up_command.append("--remove-orphans")
    up_command.extend(services)

    pull_before_up = bool(docker_config.get("pull_before_up", False))
    host = str(docker_config.get("host") or "").strip()
    if host:
        remote_steps = [
            f"cd {shlex.quote(server_path)}",
            shell_join(["git", "fetch", remote, branch]),
            shell_join(["git", "checkout", branch]),
            shell_join(["git", "pull", "--ff-only", remote, branch]),
        ]
        if pull_before_up:
            remote_steps.append(shell_join([*compose_prefix, "pull", *services]))
        remote_steps.append(shell_join(up_command))

        ssh_command = ["ssh"]
        ssh_port = docker_config.get("ssh_port")
        if ssh_port:
            ssh_command.extend(["-p", str(ssh_port)])
        identity_file = docker_config.get("identity_file")
        if identity_file:
            ssh_command.extend(["-i", str(identity_file)])
        ssh_command.extend([host, " && ".join(remote_steps)])
        return [ssh_command]

    commands: list[Command] = [["git", "pull", "--ff-only", remote, branch]]
    if pull_before_up:
        commands.append([*compose_prefix, "pull", *services])
    commands.append(up_command)
    return commands


def deployment_commands(config: dict[str, Any], repo_path: Path, pushed_commits: list[str]) -> list[Command]:
    deployment = config.get("deployment", {})
    if not deployment_enabled(config):
        return []

    placeholders = deploy_placeholders(config, repo_path, pushed_commits)
    commands = custom_deploy_commands(deployment, placeholders)
    if commands:
        return commands

    strategy = str(deployment.get("strategy", "docker")).lower()
    if strategy != "docker":
        raise RunnerError(f"未知部署策略：{strategy}。请配置 deployment.command 或使用 strategy=docker。")
    return default_docker_deploy_commands(config, repo_path, placeholders)


def run_deployment(config: dict[str, Any], repo_path: Path, pushed_commits: list[str], timeout_seconds: int) -> str:
    commands = deployment_commands(config, repo_path, pushed_commits)
    outputs: list[str] = []
    for index, command in enumerate(commands, start=1):
        log_info(config, f"开始执行部署命令 {index}/{len(commands)}：{display_command(command, cwd=repo_path)}")
        output = run_checked(command, cwd=repo_path, timeout_seconds=timeout_seconds)
        outputs.append(output or f"部署命令 {index} 执行完成。")
        log_command_output(config, f"部署命令 {index}", output)
    return "\n\n".join(outputs).strip()


def deployment_update_fields(
    remote_push_field: str,
    deployment_result_field: str,
    remote_result: str,
    deployment_result: str,
) -> dict[str, str]:
    if remote_push_field == deployment_result_field:
        combined = remote_result
        if deployment_result:
            combined = f"{combined}\n{deployment_result}" if combined else deployment_result
        return {remote_push_field: combined}
    fields = {remote_push_field: remote_result}
    if deployment_result:
        fields[deployment_result_field] = deployment_result
    return fields


def approved_record_commits(config: dict[str, Any], records: list[TaskRecord], repo_path: Path) -> dict[str, str]:
    local_commit_field = config["fields"].get("local_commit", "")
    title_field = config["fields"].get("title", "")
    commit_by_record: dict[str, str] = {}
    record_by_commit: dict[str, str] = {}
    main_branch = config["workspace"].get("main_branch", "main")

    for record in records:
        title = field_text(record.fields.get(title_field)) or record.record_id
        commit_hash = field_text(record.fields.get(local_commit_field)).splitlines()[0].strip()
        if not commit_hash:
            raise RunnerError(f"Approved Feishu task has no local commit: {title}")
        try:
            git_output(["cat-file", "-e", f"{commit_hash}^{{commit}}"], cwd=repo_path)
        except Exception as exc:
            raise RunnerError(f"Approved Feishu task references a missing local commit: {title} -> {commit_hash}") from exc
        try:
            git_output(["merge-base", "--is-ancestor", commit_hash, "HEAD"], cwd=repo_path)
        except RunnerError as exc:
            raise RunnerError(
                f"Approved Feishu task references a commit not on local {main_branch}: {title} -> {commit_hash}"
            ) from exc
        previous_record = record_by_commit.get(commit_hash)
        if previous_record:
            raise RunnerError(
                "Multiple approved Feishu tasks reference the same local commit. "
                f"Refusing to push until the records are corrected: {previous_record}, {record.record_id} -> {commit_hash}"
            )
        record_by_commit[commit_hash] = record.record_id
        commit_by_record[record.record_id] = commit_hash

    return commit_by_record


def git_output(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    output = (stdout + "\n" + stderr).strip()
    if completed.returncode != 0:
        raise RunnerError(f"git {' '.join(args)} failed\n{output}")
    return stdout.strip()


def current_commit(repo_path: Path) -> str:
    return git_output(["rev-parse", "HEAD"], cwd=repo_path)


def ensure_main_branch(config: dict[str, Any], repo_path: Path) -> None:
    main_branch = config["workspace"].get("main_branch", "main")
    current = git_output(["branch", "--show-current"], cwd=repo_path)
    if current != main_branch:
        raise RunnerError(f"Expected branch {main_branch}, current branch is {current}.")


def worktree_mode_enabled(config: dict[str, Any]) -> bool:
    mode = str(config.get("automation", {}).get("parallel_mode", "serial") or "serial").strip().lower()
    return mode in {"worktree", "worktrees", "parallel"}


def requested_parallel(config: dict[str, Any]) -> int:
    return max(1, min(int(config.get("automation", {}).get("max_parallel_tasks", 1)), 3))


def configured_worktree_dir(config: dict[str, Any], repo_path: Path) -> Path:
    configured_path = str(config.get("automation", {}).get("worktree_dir") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return (repo_path.parent / f"{repo_path.name}-worktrees").resolve()


def task_branch_name(record_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "-", record_id).strip("-")
    return f"feishu/{safe or 'task'}"


def _local_branch_exists(repo_path: Path, branch: str) -> bool:
    completed = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_path,
        capture_output=True,
    )
    return completed.returncode == 0


def _worktree_path_for_branch(repo_path: Path, branch: str) -> Path | None:
    try:
        output = git_output(["worktree", "list", "--porcelain"], cwd=repo_path)
    except RunnerError:
        return None
    current_path: Path | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree "):].strip()).resolve()
        elif line.startswith("branch refs/heads/") and current_path is not None:
            listed_branch = line[len("branch refs/heads/"):].strip()
            if listed_branch == branch:
                return current_path
        elif not line.strip():
            current_path = None
    return None


def ensure_main_clean(config: dict[str, Any], repo_path: Path) -> None:
    status = git_output(["status", "--porcelain"], cwd=repo_path)
    if status:
        raise RunnerError(
            "本地 main 工作区有未提交改动，worktree 多任务模式需要先处理干净再领取任务。\n"
            + status
        )


def prepare_task_workspace(
    config: dict[str, Any],
    main_repo: Path,
    record_id: str,
    base_commit: str,
) -> dict[str, Any]:
    branch = task_branch_name(record_id)
    worktree_dir = configured_worktree_dir(config, main_repo)
    worktree_path = worktree_dir / record_id
    existing_path = _worktree_path_for_branch(main_repo, branch)
    if existing_path is not None:
        return {
            "worktree_path": str(existing_path),
            "task_branch": branch,
            "main_repo_path": str(main_repo),
        }

    worktree_dir.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        try:
            current_branch = git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path)
            if current_branch == branch:
                return {
                    "worktree_path": str(worktree_path.resolve()),
                    "task_branch": branch,
                    "main_repo_path": str(main_repo),
                }
        except Exception as exc:
            raise RunnerError(
                f"worktree 目录已存在但不是目标任务 worktree，请先处理该目录：{worktree_path}；原因：{exc}"
            ) from exc

    if _local_branch_exists(main_repo, branch):
        git_output(["worktree", "add", str(worktree_path), branch], cwd=main_repo)
    else:
        git_output(["worktree", "add", str(worktree_path), "-b", branch, base_commit], cwd=main_repo)
    return {
        "worktree_path": str(worktree_path.resolve()),
        "task_branch": branch,
        "main_repo_path": str(main_repo),
    }


@contextmanager
def task_merge_lock():
    lock_dir = Path("state") / "merge.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + 900
    while True:
        if lock_dir.exists():
            try:
                age = time.time() - lock_dir.stat().st_mtime
                if age > 600:
                    lock_dir.rmdir()
                    continue
            except OSError:
                pass
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            if time.time() > deadline:
                raise RunnerError("等待 main 合并锁超时，可能有其他任务正在合并。")
            time.sleep(1)
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def version_doc_settings(config: dict[str, Any]) -> tuple[str, str]:
    runner = config.get("runner", {})
    doc_dir = str(runner.get("version_doc_dir") or "docs/requirements")
    extension = str(runner.get("version_doc_extension") or ".md")
    return doc_dir, extension


def resolve_version_doc_path(config: dict[str, Any], task: dict[str, Any], repo_path: Path) -> Path | None:
    version = str(task.get("version_doc") or "").strip()
    if not version:
        return None
    doc_dir, extension = version_doc_settings(config)
    lower = version.lower()
    if "/" in version or "\\" in version or lower.endswith(extension.lower()):
        relative = Path(version).expanduser()
        if relative.is_absolute():
            try:
                relative = relative.resolve().relative_to(repo_path.resolve())
            except ValueError:
                return None
    else:
        relative = Path(doc_dir) / f"{version}{extension}"
    candidate = (repo_path / relative).resolve()
    try:
        candidate.relative_to(repo_path.resolve())
    except ValueError:
        return None
    return candidate if candidate.exists() else None


def extract_version_doc_section(output: str, task: dict[str, Any]) -> str:
    text = str(output or "")
    start = text.find(VERSION_DOC_BEGIN)
    end = text.find(VERSION_DOC_END)
    if start != -1 and end != -1 and end > start:
        section = text[start + len(VERSION_DOC_BEGIN):end].strip()
        if section:
            return section

    title = str(task.get("title") or task.get("record_id") or "任务")
    description = ""
    for line in str(task.get("task_prompt") or "").splitlines():
        if line.startswith("描述："):
            description = line[len("描述："):].strip()
            break
    return "\n".join(
        [
            f"### {title}",
            "",
            "目标：完成飞书任务对应的需求/BUG/优化，并满足描述中的验收要求。",
            "",
            "范围：",
            "",
            f"- {description or title}",
            "",
            "验收标准：",
            "",
            "- 任务描述中的功能或修复已实现，且不影响原有功能。",
            "- 本地静态校验通过，未引入新的构建错误。",
        ]
    )


def append_version_doc_section(path: Path, section: str, task: dict[str, Any]) -> None:
    section = section.strip()
    if not section:
        return
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)

    heading_by_type = {
        "需求": "新增功能",
        "BUG": "修复 Bug",
        "优化": "优化项",
    }
    if not (section.splitlines() and section.splitlines()[0].startswith("### ")):
        title = str(task.get("title") or task.get("record_id") or "任务")
        section = f"### {title}\n\n{section}"
    task_type = str(task.get("type") or "")
    section_heading = heading_by_type.get(task_type, "新增功能")

    insert_idx: int | None = None
    section_start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            if section_start is not None:
                insert_idx = index
                break
            if line.strip() == f"## {section_heading}":
                section_start = index
    if section_start is None:
        for index, line in enumerate(lines):
            if line.startswith("## 发布记录要求"):
                insert_idx = index
                break
    if insert_idx is None:
        insert_idx = len(lines)
    block = f"\n\n{section.strip()}\n"
    lines.insert(insert_idx, block)
    path.write_text("".join(lines), encoding="utf-8")


def _merge_conflict_files(repo_path: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_path,
        capture_output=True,
    )
    if completed.returncode != 0:
        return []
    return [
        line.strip()
        for line in (completed.stdout or b"").decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


def integrate_task_branch(
    config: dict[str, Any],
    task: dict[str, Any],
    state_path: Path,
    title: str,
    codex_output: str,
) -> str:
    main_repo = Path(task.get("main_repo_path") or config["workspace"]["repo_path"]).expanduser().resolve()
    branch = str(task.get("task_branch") or "")
    if not branch:
        raise RunnerError("worktree 任务缺少 task_branch，无法合并到本地 main。")

    with task_merge_lock():
        ensure_main_branch(config, main_repo)
        ensure_main_clean(config, main_repo)
        if not _local_branch_exists(main_repo, branch):
            raise RunnerError(f"任务分支不存在：{branch}")

        if not task.get("integrated_commit"):
            try:
                git_output(["merge", "--no-ff", branch, "-m", f"feishu: merge {title}"], cwd=main_repo)
            except RunnerError as exc:
                conflict_files = _merge_conflict_files(main_repo)
                try:
                    git_output(["merge", "--abort"], cwd=main_repo)
                except Exception:
                    pass
                task["retry_merge_only"] = True
                task["integration_error"] = str(exc)
                write_json(state_path, task)
                detail = f"合并任务分支失败：{branch}\n{exc}"
                if conflict_files:
                    detail += "\n冲突文件：\n" + "\n".join(conflict_files)
                raise RunnerError(detail) from exc
            merged_commit = current_commit(main_repo)
            task["merged_commit"] = merged_commit
            task["integrated_commit"] = merged_commit
            write_json(state_path, task)
        else:
            merged_commit = str(task["integrated_commit"])

        final_commit = str(task.get("commit_hash") or merged_commit)
        if not task.get("version_doc_commit"):
            version_path = resolve_version_doc_path(config, task, main_repo)
            if version_path is None:
                log_info(config, "未找到对应版本需求文档，跳过 runner 自动追加。")
            else:
                section = extract_version_doc_section(codex_output, task)
                append_version_doc_section(version_path, section, task)
                relative_path = version_path.relative_to(main_repo)
                git_output(["add", str(relative_path)], cwd=main_repo)
                version = str(task.get("version_doc") or "requirements").strip()
                try:
                    git_output(["commit", "-m", f"docs: update {version} requirements - {title}"], cwd=main_repo)
                except RunnerError as exc:
                    try:
                        git_output(["restore", "--staged", "--worktree", str(relative_path)], cwd=main_repo)
                    except Exception:
                        pass
                    raise
                final_commit = current_commit(main_repo)
                task["version_doc_commit"] = final_commit

        task["commit_hash"] = final_commit
        task["retry_merge_only"] = False
        task["integration_error"] = ""
        write_json(state_path, task)
        return final_commit


def cleanup_task_workspace(config: dict[str, Any], task: dict[str, Any]) -> None:
    worktree_path = task.get("worktree_path")
    branch = task.get("task_branch")
    if not worktree_path or not branch:
        return
    main_repo = Path(task.get("main_repo_path") or config["workspace"]["repo_path"]).expanduser().resolve()
    try:
        git_output(["worktree", "remove", "--force", str(worktree_path)], cwd=main_repo)
        if _local_branch_exists(main_repo, str(branch)):
            git_output(["branch", "-D", str(branch)], cwd=main_repo)
        log_info(config, f"已清理任务 worktree 和本地分支：{branch}")
    except Exception as exc:
        log_error(f"清理任务 worktree 失败：{branch}；原因：{exc}")


def cleanup_task_workspace_by_record(config: dict[str, Any], record_id: str) -> None:
    state_path = task_state_path(record_id)
    if not state_path.exists():
        return
    try:
        task = read_json_if_exists(state_path)
        if task.get("worktree_path") and task.get("task_branch"):
            cleanup_task_workspace(config, task)
            task["workspace_cleaned"] = True
            write_json(state_path, task)
    except Exception as exc:
        log_error(f"清理任务 worktree 时更新状态失败：{record_id}；原因：{exc}")


def commit_local(config: dict[str, Any], repo_path: Path, title: str, base_commit: str = "") -> str:
    head_before = current_commit(repo_path)
    git_output(["add", "-A"], cwd=repo_path)
    status = git_output(["status", "--porcelain"], cwd=repo_path)
    if not status:
        if base_commit and head_before != base_commit:
            return head_before
        raise RunnerError(
            "Codex task finished without a new local commit or uncommitted changes. "
            "Refusing to reuse the current HEAD for this Feishu task."
        )

    message = f"chore: complete Feishu task - {title}".strip()
    git_output(["commit", "-m", message], cwd=repo_path)
    return current_commit(repo_path)


def runner_commit_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("runner", {}).get("commit_after_development", True))


def complete_local_commit(config: dict[str, Any], repo_path: Path, title: str, base_commit: str = "") -> str:
    if runner_commit_enabled(config):
        log_info(config, "开始由 runner 提交到本地 main。")
        return commit_local(config, repo_path, title, base_commit)

    log_info(config, "runner 本地提交已关闭，读取 AI 会话已提交的本地 HEAD。")
    status = git_output(["status", "--porcelain"], cwd=repo_path)
    if status:
        raise RunnerError(
            "runner.commit_after_development=false，但目标仓库仍有未提交改动。"
            "请让 AI 会话完成本地提交，或把 runner.commit_after_development 改为 true。"
        )
    head = current_commit(repo_path)
    if base_commit and head == base_commit:
        raise RunnerError(
            "Codex task finished without a new local commit. "
            "Refusing to mark this Feishu task as completed with the previous HEAD."
        )
    return head


def ensure_ai_local_commit(
    config: dict[str, Any],
    task: dict[str, Any],
    repo_path: Path,
    timeout_seconds: int,
    state_path: Path,
) -> str:
    if runner_commit_enabled(config):
        return ""

    status = git_output(["status", "--porcelain"], cwd=repo_path)
    if not status:
        return ""

    log_info(config, "runner 本地提交已关闭，但工作区仍有未提交改动，准备让 AI 会话继续完成本地提交。")
    prompt = "\n".join(
        [
            "上一次任务开发已经完成，但目标仓库仍有未提交改动。",
            "请只处理本地提交收尾：",
            "1. 查看 git status，确认本轮修改内容。",
            "2. 如有必要，可以运行项目配置允许的静态校验，但不要本地跑服务测试。",
            (
                f"3. 将当前任务相关修改提交到当前任务分支 {task.get('task_branch')}，不要切换分支。"
                if task.get("task_branch")
                else "3. 将当前任务相关修改提交到本地 main。"
            ),
            "4. 不要继续新增功能，不要重构无关代码，不要推送远程仓库。",
            "5. 完成后给出本地 commit hash。",
        ]
    )
    output = run_codex_followup(config, task, repo_path, prompt, timeout_seconds)
    task["ai_commit_output"] = output
    task["retry_requested"] = False
    write_json(state_path, task)

    status_after = git_output(["status", "--porcelain"], cwd=repo_path)
    if status_after:
        raise RunnerError(
            "runner.commit_after_development=false，AI 会话补提交后目标仓库仍有未提交改动。"
            "请检查 AI 会话输出，或临时把 runner.commit_after_development 改为 true。"
        )
    log_info(config, "AI 会话已完成本地提交，工作区已干净。")
    return output


def local_git_record(config: dict[str, Any], repo_path: Path, commit_hash: str = "") -> str:
    branch = git_output(["branch", "--show-current"], cwd=repo_path)
    commit_hash = commit_hash or current_commit(repo_path)
    commit_title = git_output(["log", "-1", "--pretty=%s", commit_hash], cwd=repo_path)
    recorded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join(
        [
            f"分支：{branch}",
            f"提交：{commit_hash}",
            f"标题：{commit_title}",
            f"记录时间：{recorded_at}",
        ]
    )


def build_task_state(
    record: TaskRecord,
    config: dict[str, Any],
    repo_path: Path,
    token: str | None = None,
    *,
    task_branch: str = "",
    worktree_mode: bool = False,
) -> dict[str, Any]:
    title = field_text(record.fields.get(config["fields"]["title"]))
    task_type = field_text(record.fields.get(config["fields"].get("type", "")))
    version_doc = field_text(record.fields.get(config["fields"].get("version_doc", "")))
    previous = read_json_if_exists(task_state_path(record.record_id))
    retry_count = int(previous.get("retry_count") or 0)
    retry_requested = bool(previous.get("retry_requested"))
    base_commit = str(previous.get("base_commit") or current_commit(repo_path))

    task = {
        "claimed": True,
        "record_id": record.record_id,
        "title": title,
        "type": task_type,
        "version_doc": version_doc,
        "repo_path": str(repo_path),
        "main_branch": config["workspace"].get("main_branch", "main"),
        "static_check_command": config["runner"].get("static_check_command"),
        "task_prompt": render_task_prompt(
            record,
            config,
            task_branch=task_branch,
            worktree_mode=worktree_mode,
        ),
        "base_commit": base_commit,
        "retry_count": retry_count + 1 if retry_requested else retry_count,
        "retry_requested": retry_requested,
    }
    if worktree_mode:
        task["task_branch"] = task_branch

    codex_session_id = str(previous.get("codex_session_id") or "")
    if codex_session_id:
        task["codex_session_id"] = codex_session_id
    previous_error = str(previous.get("error") or "")
    if previous_error:
        task["previous_error"] = previous_error
    for key in ("codex_output", "static_output", "retry_commit_only", "retry_merge_only", "image_paths"):
        if key in previous:
            task[key] = previous[key]
    for key in ("main_repo_path", "worktree_path", "task_branch"):
        if key in previous:
            task[key] = previous[key]
    if token:
        try:
            image_paths = download_task_images(config, token, record)
            if image_paths:
                task["image_paths"] = image_paths
        except Exception as exc:
            task["image_download_error"] = str(exc)
            log_error(f"飞书图片附件下载失败，将继续按文字任务执行：{title or record.record_id}；原因：{exc}")
    return task


def commit_failure_text(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "git add",
            "git commit",
            "git status --porcelain",
            "index.lock",
            "commit_after_development=false",
        )
    )


def execute_tasks_worktree(config: dict[str, Any], token: str) -> None:
    main_repo = Path(config["workspace"]["repo_path"]).expanduser().resolve()
    log_info(config, f"开始扫描待处理任务（worktree 多任务模式），目标仓库：{main_repo}")
    if not main_repo.exists():
        raise RunnerError(f"Repo path does not exist: {main_repo}")
    ensure_main_branch(config, main_repo)
    ensure_main_clean(config, main_repo)
    resolve_status_mapping(config, token)

    records = records_with_status(list_records(config, token), config, "pending")
    max_tasks = max(1, min(int(config["runner"].get("max_tasks_per_run", 3)), 3))
    max_parallel = requested_parallel(config)
    limit = min(max_tasks, max_parallel)
    if not records or limit <= 0:
        log_debug(config, "没有待处理任务。")
        return
    log_info(config, f"发现 {len(records)} 个待处理任务，本轮最多并行执行 {limit} 个。")

    executor = ThreadPoolExecutor(max_workers=limit)
    active: dict[Future[str], Path] = {}
    errors: list[str] = []
    try:
        for _ in range(limit):
            task = claim_task_record(config, token, None)
            if not task:
                break
            state_file = task_state_path(task["record_id"])
            write_json(state_file, task)
            future = executor.submit(process_claimed_task, config, state_file)
            active[future] = state_file
            log_info(config, f"Codex 任务已并行启动：{task.get('title') or task['record_id']}")
        if not active:
            return
        for future, state_file in list(active.items()):
            try:
                commit_hash = future.result()
                task = read_json(state_file)
                log_info(config, f"异步 Codex 任务完成：{task.get('title') or state_file.stem}，本地提交：{commit_hash}")
            except Exception as exc:
                log_error(f"异步 Codex 任务结束时出错：{state_file}；原因：{exc}")
                errors.append(str(exc))
    finally:
        executor.shutdown(wait=True)
    if errors:
        raise RunnerError("本轮 worktree 任务存在失败：\n" + "\n".join(errors))


def execute_tasks(config: dict[str, Any], token: str) -> None:
    if worktree_mode_enabled(config):
        execute_tasks_worktree(config, token)
        return
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
        log_debug(config, "没有待处理任务。")
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
        codex_output = ""
        static_output = ""
        state_path = task_state_path(record.record_id)
        task = build_task_state(record, config, repo_path, token)
        write_json(state_path, task)
        try:
            if task.get("retry_commit_only"):
                log_info(config, f"上次已完成 Codex 执行，本轮只重试本地提交：{title or record.record_id}")
                codex_output = str(task.get("codex_output") or "")
                static_output = str(task.get("static_output") or "")
            else:
                log_info(config, f"已把飞书状态改为“开发中”，开始调用 Codex 执行需求，最长等待 {command_timeout} 秒。")
                codex_output = run_codex_task(config, task, repo_path, command_timeout)
                task["codex_output"] = codex_output
                task["retry_requested"] = False
                write_json(state_path, task)
                log_info(config, "Codex 执行完成。")
                log_command_output(config, "Codex", codex_output)
                static_check = config["runner"].get("static_check_command")
                if static_check:
                    log_info(config, "开始运行静态校验。")
                    static_output = run_checked(static_check, cwd=repo_path, timeout_seconds=command_timeout)
                    task["static_output"] = static_output
                    write_json(state_path, task)
                    log_info(config, "静态校验通过。")
                    log_command_output(config, "静态校验", static_output)
            ai_commit_output = ensure_ai_local_commit(config, task, repo_path, command_timeout, state_path)
            commit_hash = complete_local_commit(config, repo_path, title, str(task.get("base_commit") or ""))
            task["commit_hash"] = commit_hash
            task["codex_output"] = codex_output
            task["static_output"] = static_output
            write_json(state_path, task)
            local_git_record_text = local_git_record(config, repo_path, commit_hash)
            review_message = "本地开发完成，已提交到本地 main，等待人工审核。"
            codex_result_output = codex_output
            if ai_commit_output:
                codex_result_output = f"{codex_output}\n\nAI 本地提交补充输出：\n{ai_commit_output}".strip()
            finished_fields: dict[str, Any] = {
                status_field: config["statuses"]["review"],
                result_field: format_review_result(
                    review_message,
                    commit_hash=commit_hash,
                    codex_output=codex_result_output,
                    static_output=static_output,
                    local_git_record_text=local_git_record_text,
                ),
                local_commit_field: commit_hash,
            }
            if local_git_record_field:
                finished_fields[local_git_record_field] = local_git_record_text
            if ai_end_time_field:
                finished_fields[ai_end_time_field] = now_millis()
            update_record(config, token, record.record_id, finished_fields)
            task["finished"] = True
            task["retry_requested"] = False
            task["retry_commit_only"] = False
            write_json(state_path, task)
            log_info(config, f"任务已完成本地提交，等待人工审核：{title or record.record_id}，commit：{commit_hash}")
        except Exception as exc:
            original_error = str(exc)
            task["failed"] = True
            task["error"] = original_error
            if commit_failure_text(original_error):
                task["retry_commit_only"] = True
            write_json(state_path, task)
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
        log_debug(config, "没有审核通过的任务需要推送。")
        return
    log_info(config, f"发现 {len(records)} 个审核通过任务，开始推送远程。")

    remote = config["workspace"].get("remote", "origin")
    main_branch = config["workspace"].get("main_branch", "main")

    status_field = config["fields"]["status"]
    remote_push_field = config["fields"]["remote_push"]
    deployment_result_field = config["fields"].get("deployment_result") or remote_push_field
    commit_by_record = approved_record_commits(config, records, repo_path)
    pushed_commits = list(commit_by_record.values())

    try:
        push_output = git_output(["push", remote, main_branch], cwd=repo_path)
        deployment_output = ""
        if deployment_enabled(config):
            push_summary = push_output or f"pushed {remote}/{main_branch}"
            for record in records:
                update_fields = deployment_update_fields(
                    remote_push_field,
                    deployment_result_field,
                    push_summary,
                    "远程推送完成，开始自动部署。",
                )
                update_record(
                    config,
                    token,
                    record.record_id,
                    update_fields,
                )
            deployment_timeout = int(
                config.get("deployment", {}).get(
                    "timeout_seconds",
                    config.get("runner", {}).get("command_timeout_seconds", 3600),
                )
            )
            log_info(config, f"远程推送完成，开始自动部署，最长等待 {deployment_timeout} 秒。")
            deployment_output = run_deployment(config, repo_path, pushed_commits, deployment_timeout)
            log_info(config, "自动部署完成。")
    except Exception as exc:
        error_message = str(exc)
        for record in records:
            update_fields = {
                status_field: config["statuses"]["failed"],
                deployment_result_field: f"推送或部署失败：{error_message}",
            }
            if deployment_result_field == remote_push_field:
                update_fields = {
                    status_field: config["statuses"]["failed"],
                    remote_push_field: f"推送或部署失败：{error_message}",
                }
            update_record(
                config,
                token,
                record.record_id,
                update_fields,
            )
        raise RunnerError(error_message) from exc

    for record in records:
        local_commit = commit_by_record.get(record.record_id, "")
        remote_result = push_output or f"pushed {remote}/{main_branch}"
        if local_commit:
            remote_result = f"{remote_result}\n已推送本地提交：{local_commit}"
        if deployment_enabled(config):
            deploy_result = deployment_output or "自动部署完成。"
            deployment_result = f"自动部署：{deploy_result}"
        else:
            deployment_result = ""
        update_fields = {
            status_field: config["statuses"]["pushed"],
            **deployment_update_fields(remote_push_field, deployment_result_field, remote_result, deployment_result),
        }
        update_record(
            config,
            token,
            record.record_id,
            update_fields,
        )
    if deployment_enabled(config):
        log_info(config, f"远程推送和自动部署完成，已回写 {len(records)} 条飞书记录为“已推送”。")
    else:
        log_info(config, f"远程推送完成，已回写 {len(records)} 条飞书记录为“已推送”。")
    for record in records:
        cleanup_task_workspace_by_record(config, record.record_id)
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
    workspace: dict[str, Any] = {}
    if worktree_mode_enabled(config):
        ensure_main_branch(config, repo_path)
        ensure_main_clean(config, repo_path)
        workspace = prepare_task_workspace(config, repo_path, record.record_id, current_commit(repo_path))

    status_field = config["fields"]["status"]
    ai_start_time_field = config["fields"].get("ai_start_time")
    update_fields: dict[str, Any] = {status_field: config["statuses"]["in_progress"]}
    if ai_start_time_field:
        update_fields[ai_start_time_field] = now_millis()
    update_record(config, token, record.record_id, update_fields)

    task_repo = Path(workspace.get("worktree_path") or repo_path)
    task = build_task_state(
        record,
        config,
        task_repo,
        token,
        task_branch=workspace.get("task_branch", ""),
        worktree_mode=bool(workspace),
    )
    for key in ("main_repo_path", "worktree_path", "task_branch"):
        if workspace.get(key):
            task[key] = workspace[key]
    if state_path is not None:
        write_json(state_path, task)
    return task


def finish_task(config: dict[str, Any], token: str, state_path: Path, message: str) -> None:
    task = read_json(state_path)
    if not task.get("claimed"):
        log_info(config, "状态文件中没有已领取任务，无需回写完成状态。")
        return
    resolve_status_mapping(config, token)

    repo_path = Path(
        task.get("main_repo_path")
        or task.get("repo_path")
        or config["workspace"]["repo_path"]
    ).expanduser().resolve()
    if task.get("main_repo_path") and task.get("worktree_path") and not task.get("integrated_commit"):
        final_commit = integrate_task_branch(
            config,
            task,
            state_path,
            str(task.get("title") or task["record_id"]),
            str(task.get("codex_output") or ""),
        )
        task["commit_hash"] = final_commit
    commit_hash = str(task.get("commit_hash") or current_commit(repo_path))
    status_field = config["fields"]["status"]
    result_field = config["fields"]["result"]
    local_commit_field = config["fields"]["local_commit"]
    ai_end_time_field = config["fields"].get("ai_end_time")
    local_git_record_text = local_git_record(config, repo_path, commit_hash)
    review_message = message or "本地开发完成，已提交到本地 main，等待人工审核。"
    codex_output = str(task.get("codex_output") or "")
    ai_commit_output = str(task.get("ai_commit_output") or "")
    if ai_commit_output:
        codex_output = f"{codex_output}\n\nAI 本地提交补充输出：\n{ai_commit_output}".strip()
    update_fields: dict[str, Any] = {
        status_field: config["statuses"]["review"],
        result_field: format_review_result(
            review_message,
            commit_hash=commit_hash,
            codex_output=codex_output,
            static_output=str(task.get("static_output") or ""),
            local_git_record_text=local_git_record_text,
        ),
        local_commit_field: commit_hash,
    }
    local_git_record_field = config["fields"].get("local_git_record")
    if local_git_record_field:
        update_fields[local_git_record_field] = local_git_record_text
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


def retry_failed_tasks(config: dict[str, Any], token: str, record_id: str = "", limit: int = 1) -> None:
    resolve_status_mapping(config, token)

    records = records_with_status(list_records(config, token), config, "failed")
    if record_id:
        records = [record for record in records if record.record_id == record_id]

    if not records:
        log_info(config, "没有执行失败的任务需要重新转待处理。")
        return

    selected = records if limit <= 0 else records[:limit]
    status_field = config["fields"]["status"]
    result_field = config["fields"]["result"]

    for record in selected:
        title = field_text(record.fields.get(config["fields"]["title"]))
        state_path = task_state_path(record.record_id)
        state = read_json_if_exists(state_path)
        state["record_id"] = record.record_id
        state["title"] = title
        state["retry_requested"] = True
        state["failed"] = False
        state["retry_requested_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        previous_result = field_text(record.fields.get(result_field))
        if previous_result:
            state["previous_result"] = previous_result
        previous_error = str(state.get("error") or previous_result)
        retry_commit_only = commit_failure_text(previous_error)
        retry_merge_only = bool(state.get("retry_merge_only")) or "合并任务分支失败" in previous_error
        if retry_commit_only:
            state["retry_commit_only"] = True
        if retry_merge_only:
            state["retry_merge_only"] = True
        write_json(state_path, state)

        session_id = str(state.get("codex_session_id") or "") or recent_codex_session_id_by_title(title)
        if session_id:
            state["codex_session_id"] = session_id
            write_json(state_path, state)
        retry_message = "已重新转为待处理。"
        if retry_merge_only:
            retry_message += "\n上次失败发生在合并到本地 main 阶段，下一次将只重试合并，不重新执行 Codex。"
        elif retry_commit_only:
            retry_message += "\n上次失败发生在 Git 本地提交阶段，下一次将只重试提交和回写，不重新执行 Codex。"
        elif session_id:
            retry_message += f"\n将继续上次 Codex 会话：{session_id}"
        else:
            retry_message += "\n本地没有找到上次 Codex 会话 id，下一次会按新会话执行。"

        update_record(
            config,
            token,
            record.record_id,
            {
                status_field: config["statuses"]["pending"],
                result_field: retry_message,
            },
        )
        log_info(config, f"已把失败任务重新转为待处理：{title or record.record_id}")


def run_static_check_for_task(config: dict[str, Any], task: dict[str, Any], repo_path: Path, timeout_seconds: int, state_path: Path) -> str:
    static_check = task.get("static_check_command") or config["runner"].get("static_check_command")
    if not static_check:
        return ""

    log_info(config, "开始运行静态校验。")
    static_output = run_checked(static_check, cwd=repo_path, timeout_seconds=timeout_seconds)
    task["static_output"] = static_output
    write_json(state_path, task)
    log_info(config, "静态校验通过。")
    log_command_output(config, "静态校验", static_output)
    return static_output


def watch_recovery_prompt(task: dict[str, Any]) -> str:
    original_prompt = str(task.get("task_prompt") or "").strip()
    commit_line = (
        f"5. 完成后提交到当前任务分支 {task.get('task_branch')}，不要推送远程。"
        if task.get("task_branch")
        else "5. 完成后提交到本地 main，不要推送远程。"
    )
    lines = [
        "这是一个飞书开发中任务的 worker 重启恢复检查。",
        "请回到当前会话上下文，先检查上一轮是否已经完成开发，不要把任务当成全新需求重做。",
        "",
        "恢复要求：",
        "1. 查看 git status、已修改文件、版本需求文档和原任务要求，判断开发是否已经完成。",
        "2. 如果已经完成，只做必要的静态校验和本地提交收尾。",
        "3. 如果还没完成，只继续缺失部分；不要重复实现已经存在的改动。",
        "4. 可以本地跑静态校验，不要本地跑服务测试。",
        commit_line,
    ]
    if task.get("task_branch"):
        lines.append(
            "6. 不要直接修改版本需求文档文件；如需补充内容，在最终回复末尾用 [VERSION_DOC_BEGIN] 和 [VERSION_DOC_END] 包裹。"
        )
    if original_prompt:
        lines.extend(["", "原任务要求：", original_prompt])
    return "\n".join(lines)


def process_claimed_task(config: dict[str, Any], state_path: Path) -> str:
    task = read_json(state_path)
    if not task.get("claimed"):
        return "没有已领取任务"

    repo_path = Path(task["repo_path"]).expanduser().resolve()
    command_timeout = int(config.get("runner", {}).get("command_timeout_seconds", 3600))
    codex_output = ""
    static_output = ""

    try:
        if task.get("retry_merge_only"):
            log_info(config, f"上次失败在合并阶段，本轮只重试合并到本地 main：{task.get('title') or task['record_id']}")
            codex_output = str(task.get("codex_output") or "")
            static_output = str(task.get("static_output") or "")
            title = str(task.get("title") or task["record_id"])
            final_commit = integrate_task_branch(config, task, state_path, title, codex_output)
            task["codex_output"] = codex_output
            task["static_output"] = static_output
            task["commit_hash"] = final_commit
            task["retry_merge_only"] = False
            task["resume_after_watch_restart"] = False
            task["failed"] = False
            write_json(state_path, task)
            token = get_tenant_access_token(config)
            finish_task(config, token, state_path, "本地开发完成，已合并到本地 main，等待人工审核。")
            return final_commit
        elif task.get("retry_commit_only"):
            log_info(config, f"上次已完成 Codex 执行，本轮只重试本地提交：{task.get('title') or task['record_id']}")
            codex_output = str(task.get("codex_output") or "")
            static_output = str(task.get("static_output") or "")
        elif task.get("resume_after_watch_restart") and task.get("codex_output"):
            log_info(config, f"恢复重启前已领取任务，本轮从静态校验/提交收尾继续：{task.get('title') or task['record_id']}")
            codex_output = str(task.get("codex_output") or "")
            static_output = str(task.get("static_output") or "")
            if not static_output:
                static_output = run_static_check_for_task(config, task, repo_path, command_timeout, state_path)
        elif task.get("resume_after_watch_restart") and task.get("codex_session_id"):
            log_info(config, f"恢复重启前已领取任务，使用原 Codex 会话检查完成度：{task.get('title') or task['record_id']}")
            codex_output = run_codex_followup(config, task, repo_path, watch_recovery_prompt(task), command_timeout)
            task["codex_output"] = codex_output
            task["retry_requested"] = False
            write_json(state_path, task)
            log_info(config, "Codex 会话恢复检查完成。")
            log_command_output(config, "Codex 恢复检查", codex_output)
            static_output = run_static_check_for_task(config, task, repo_path, command_timeout, state_path)
        else:
            log_info(config, f"后台异步执行 Codex 任务：{task.get('title') or task['record_id']}，最长等待 {command_timeout} 秒。")
            codex_output = run_codex_task(config, task, repo_path, command_timeout)
            task["codex_output"] = codex_output
            task["retry_requested"] = False
            write_json(state_path, task)
            log_info(config, "Codex 执行完成。")
            log_command_output(config, "Codex", codex_output)
            static_output = run_static_check_for_task(config, task, repo_path, command_timeout, state_path)

        title = str(task.get("title") or task["record_id"])
        ensure_ai_local_commit(config, task, repo_path, command_timeout, state_path)
        commit_hash = complete_local_commit(config, repo_path, title, str(task.get("base_commit") or ""))
        if task.get("main_repo_path"):
            final_commit = integrate_task_branch(config, task, state_path, title, codex_output)
            task["codex_output"] = codex_output
            task["static_output"] = static_output
            task["commit_hash"] = final_commit
            task["retry_requested"] = False
            task["retry_commit_only"] = False
            task["resume_after_watch_restart"] = False
            task["failed"] = False
            write_json(state_path, task)
            token = get_tenant_access_token(config)
            finish_task(config, token, state_path, "本地开发完成，已合并到本地 main，等待人工审核。")
            return final_commit
        task["codex_output"] = codex_output
        task["static_output"] = static_output
        task["commit_hash"] = commit_hash
        task["retry_requested"] = False
        task["retry_commit_only"] = False
        task["resume_after_watch_restart"] = False
        write_json(state_path, task)
        token = get_tenant_access_token(config)
        finish_task(config, token, state_path, "本地开发完成，已提交到本地 main，等待人工审核。")
        return commit_hash
    except Exception as exc:
        task["failed"] = True
        task["error"] = str(exc)
        if commit_failure_text(str(exc)):
            task["retry_commit_only"] = True
        write_json(state_path, task)
        try:
            token = get_tenant_access_token(config)
            fail_task(config, token, state_path, str(exc))
        except Exception as update_exc:
            log_error(
                "任务失败后回写飞书也失败；"
                f"任务：{task.get('title') or task['record_id']}；原始原因：{exc}；回写失败原因：{update_exc}"
            )
        raise


def recover_watch_tasks(
    config: dict[str, Any],
    token: str,
    executor: ThreadPoolExecutor,
    active_futures: dict[Future[str], Path],
    max_parallel: int,
) -> None:
    available_slots = max_parallel - len(active_futures)
    if available_slots <= 0:
        return

    task_dir = Path("state/tasks")
    if not task_dir.exists():
        return

    resolve_status_mapping(config, token)
    in_progress_records = {
        record.record_id: record for record in records_with_status(list_records(config, token), config, "in_progress")
    }
    if not in_progress_records:
        return

    configured_repo_path = Path(config["workspace"]["repo_path"]).expanduser().resolve()
    active_paths = {path.resolve() for path in active_futures.values()}
    recovered = 0
    for state_file in sorted(task_dir.glob("*.json"), key=lambda path: path.stat().st_mtime):
        if recovered >= available_slots:
            break
        if state_file.resolve() in active_paths:
            continue

        task = read_json_if_exists(state_file)
        record_id = str(task.get("record_id") or state_file.stem)
        if record_id not in in_progress_records:
            continue
        if not task.get("claimed") or task.get("finished") or task.get("failed"):
            continue

        task_main_repo = Path(
            task.get("main_repo_path")
            or task.get("repo_path")
            or configured_repo_path
        ).expanduser().resolve()
        if task_main_repo != configured_repo_path:
            continue

        if worktree_mode_enabled(config) and task.get("task_branch"):
            try:
                if not task.get("worktree_path") or not Path(task["worktree_path"]).exists():
                    workspace = prepare_task_workspace(
                        config,
                        configured_repo_path,
                        record_id,
                        task.get("base_commit") or current_commit(configured_repo_path),
                    )
                    task.update(workspace)
                    write_json(state_file, task)
            except Exception as exc:
                log_error(f"恢复任务 worktree 失败：{task.get('title') or record_id}；原因：{exc}")
                continue

        can_recover = bool(
            task.get("retry_commit_only")
            or task.get("retry_merge_only")
            or task.get("codex_output")
            or task.get("codex_session_id")
        )
        if not can_recover:
            if not task.get("recovery_skipped_reason"):
                task["recovery_skipped_reason"] = "开发中任务缺少 codex_session_id 和 codex_output，已跳过自动恢复以避免重复执行。"
                write_json(state_file, task)
                log_error(f"跳过开发中任务恢复：{task.get('title') or record_id}；缺少 Codex 会话 ID 和输出，无法判断是否完成。", config)
            continue

        if (
            (task.get("codex_output") or task.get("codex_session_id"))
            and not task.get("retry_requested")
            and not task.get("retry_commit_only")
            and not task.get("retry_merge_only")
        ):
            task["resume_after_watch_restart"] = True
            write_json(state_file, task)

        future = executor.submit(process_claimed_task, config, state_file)
        active_futures[future] = state_file
        recovered += 1
        log_info(config, f"已恢复重启前的开发中任务，后台继续处理：{task.get('title') or record_id}")


def run_watch(config: dict[str, Any]) -> None:
    automation = config.get("automation", {})
    interval = max(10, int(automation.get("poll_interval_seconds", 60)))
    execute_pending = bool(automation.get("execute_pending", True))
    push_after_approval = bool(automation.get("push_approved", True))
    requested_parallel = max(1, min(int(automation.get("max_parallel_tasks", 1)), 3))
    allow_parallel_same_repo = bool(automation.get("allow_parallel_same_repo", False))
    if worktree_mode_enabled(config):
        max_parallel = requested_parallel
    elif allow_parallel_same_repo:
        max_parallel = requested_parallel
        log_info(config, "allow_parallel_same_repo=true 但未启用 worktree 隔离，多个任务仍共享同一工作区，请谨慎使用。")
    else:
        max_parallel = 1
        if requested_parallel > 1:
            log_info(config, "同一目标仓库默认串行执行，已把本轮并行数限制为 1。启用 automation.parallel_mode=worktree 后可按 worktree 并行处理。")
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

        log_debug(config, "开始轮询飞书多维表。")
        try:
            token = get_tenant_access_token(config)
            recover_watch_tasks(config, token, executor, active_futures, max_parallel)
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
                        log_debug(config, "本轮没有需要异步处理的任务。")
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
            "retry-failed",
        ],
    )
    parser.add_argument("--state-file", type=Path, default=default_state_path())
    parser.add_argument("--message", default="")
    parser.add_argument("--record-id", default="")
    parser.add_argument("--limit", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    global ACTIVE_CONFIG
    args = parse_args()
    ACTIVE_CONFIG = {"logging": {"enabled": True, "file_enabled": True}}
    try:
        config = load_config(args.config)
        ACTIVE_CONFIG = config
        log_info(config, f"runner 启动：mode={args.mode}, config={args.config}")
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
        elif args.mode == "retry-failed":
            retry_failed_tasks(config, token, args.record_id, args.limit)
        log_info(config, f"runner 完成：mode={args.mode}")
        return 0
    except KeyboardInterrupt:
        log_error("已停止。")
        return 0
    except Exception as exc:
        log_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
