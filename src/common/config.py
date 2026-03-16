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


def save_config(cfg: dict[str, Any]) -> None:
    """
    保存配置到 config/config.yaml（原子写入）。
    注意：本项目面向单机部署，配置文件直接落盘即可生效（调用方负责必要的重启/热加载策略）。
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg or {}, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, CONFIG_PATH)


def set_cfg_value(cfg: dict[str, Any], keys: list[str], value: Any) -> dict[str, Any]:
    """
    将 value 写入 cfg 的嵌套路径 keys，例如 keys=["quark","cookie"]。
    会自动创建中间 dict。
    """
    cur: dict[str, Any] = cfg
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = value
    return cfg


def get_download_dir() -> str:
    cfg = load_config()
    d = cfg.get("download", {}).get("output_dir", "./downloads")
    if not os.path.isabs(d):
        d = str(PROJECT_ROOT / d)
    return d
