"""accounts.json 读写：兼容 JSONL、JSON 数组、多个格式化 JSON 对象拼接。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def _iter_concat_json_objects(text: str) -> Iterator[str]:
    """按顶层 { } 切分连续拼接的 JSON 对象（字符串内的括号不计入）。"""
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\n\r":
            i += 1
        if i >= n:
            break
        if text[i] != "{":
            raise json.JSONDecodeError(
                f"期望 '{{'，实际 {text[i]!r}", text, i
            )
        depth = 0
        in_string = False
        escape = False
        start = i
        while i < n:
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        yield text[start:i]
                        break
            i += 1
        else:
            raise json.JSONDecodeError("未闭合的 JSON 对象", text, start)


def _is_jsonl(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    return all(
        ln.startswith("{")
        and ln.endswith("}")
        and len(ln) > 2
        and not ln.startswith("  ")
        for ln in lines
    )


def parse_accounts_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path}: 根节点不是数组")
        return data

    if _is_jsonl(text):
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]

    if text.startswith("{"):
        accounts: list[dict[str, Any]] = []
        for chunk in _iter_concat_json_objects(text):
            obj = json.loads(chunk)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}: 解析结果不是对象")
            accounts.append(obj)
        return accounts

    raise ValueError(f"{path}: 无法识别的 accounts.json 格式")


def write_accounts_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
