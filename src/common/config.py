"""加载 config/config.yaml 配置。"""
import os
from pathlib import Path
from typing import Any

import yaml

# 项目根目录（auto_download）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"请创建配置文件: {CONFIG_PATH}（可复制 config/config.yaml.example）")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_download_dir() -> str:
    cfg = load_config()
    d = cfg.get("download", {}).get("output_dir", "./downloads")
    if not os.path.isabs(d):
        d = str(PROJECT_ROOT / d)
    return d
