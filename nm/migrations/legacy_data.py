"""将旧运行目录安全迁移到 .nm，源数据永不删除。

一次性兼容迁移层——旧运行目录（legacy dirs）只允许在本包出现，识别名见 LEGACY_DIR_NAMES。
迁移完成后旧目录可手动删除，运行时一律读 .nm。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

LEGACY_DIR_NAMES = (".harness", ".workbuddy")
SCHEMA_VERSION = 1
# 迁移文件后缀：JSON/JSONL 数据 + MD 笔记（.workbuddy/memory/ 下的记忆）
MIGRATE_SUFFIXES = {".json", ".jsonl", ".md"}


def _migratable_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in MIGRATE_SUFFIXES
        and not p.name.startswith(".")
    )


def _validate_json(path: Path) -> None:
    if path.suffix == ".json":
        with path.open(encoding="utf-8") as fh:
            json.load(fh)
    elif path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    json.loads(line)
    # .md 不校验，直接拷贝


def migrate_legacy_data(workspace: str, *, target_dir: str | None = None) -> dict[str, Any]:
    """复制旧目录到 .nm，校验文件和 JSON；失败时保留源数据并返回错误。"""
    root = Path(workspace)
    target = Path(target_dir) if target_dir else root / ".nm"
    sources = [root / name for name in LEGACY_DIR_NAMES if (root / name).is_dir()]
    if not sources:
        return {"status": "noop", "files": 0, "sources": []}

    target.mkdir(parents=True, exist_ok=True)
    backup = target / "migration-backup"
    copied = 0
    errors: list[str] = []
    try:
        for source in sources:
            for path in _migratable_files(source):
                relative = path.relative_to(source)
                destination = target / relative
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                _validate_json(destination)
                copied += 1
        marker = target / "schema.json"
        if not marker.exists():
            marker.write_text(json.dumps({"schema": "nm-runtime", "version": SCHEMA_VERSION}, indent=2), encoding="utf-8")
        return {"status": "migrated", "files": copied, "sources": [str(p) for p in sources], "backup": str(backup)}
    except Exception as exc:
        errors.append(str(exc))
        return {"status": "failed", "files": copied, "sources": [str(p) for p in sources], "errors": errors}
