"""
核销码：生成（您发给客户）、校验与次数扣减（客户在下载器内绑定与核销）。
码格式：WD + base64url( 2字节次数 + 6字节随机 + 8字节HMAC )，防伪造。
"""
import base64
import hashlib
import json
import os
import struct
from pathlib import Path

# 与 main.py 一致的 APP_DIR
import sys
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent

CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "redeem_state.json"
CODE_PREFIX = "WD"
HMAC_LEN = 8


def _get_secret() -> str:
    if not CONFIG_PATH.exists():
        return ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return (cfg.get("code_secret") or "").strip()
    except Exception:
        return ""


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def generate_code(times: int) -> str:
    """
    生成一枚核销码（您本地运行，发给客户）。
    times: 可下载次数，1～65535。
    """
    if times < 1 or times > 65535:
        raise ValueError("次数需在 1～65535 之间")
    secret = _get_secret()
    if not secret:
        raise ValueError("请在 config.json 中配置 code_secret")
    payload = struct.pack(">H", times) + os.urandom(6)
    sig = hashlib.new("sha256", (secret.encode("utf-8") + payload)).digest()[:HMAC_LEN]
    raw = payload + sig
    return CODE_PREFIX + _b64_encode(raw)


def verify_code(code: str) -> tuple[bool, int | None, str]:
    """
    校验核销码，返回 (是否有效, 次数, 错误信息)。
    """
    code = (code or "").strip()
    if not code.startswith(CODE_PREFIX) or len(code) <= len(CODE_PREFIX):
        return False, None, "核销码格式不正确"
    secret = _get_secret()
    if not secret:
        return False, None, "应用未配置 code_secret，无法校验"
    try:
        raw = _b64_decode(code[len(CODE_PREFIX):])
    except Exception:
        return False, None, "核销码无法解析"
    if len(raw) < 2 + 6 + HMAC_LEN:
        return False, None, "核销码无效"
    payload = raw[:8]
    sig = raw[8:8 + HMAC_LEN]
    expected = hashlib.new("sha256", (secret.encode("utf-8") + payload)).digest()[:HMAC_LEN]
    if sig != expected:
        return False, None, "核销码无效或已损坏"
    times, = struct.unpack(">H", payload[:2])
    if times < 1:
        return False, None, "核销码无效"
    return True, times, ""


def _state_hash(code: str, remaining: int) -> str:
    secret = _get_secret()
    return hashlib.sha256((secret + code + str(remaining)).encode()).hexdigest()[:16]


def load_state() -> dict | None:
    """读取本地核销状态：{ code, total, remaining, h }，无效或不存在返回 None。"""
    if not STATE_PATH.exists():
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        code = data.get("code") or ""
        remaining = int(data.get("remaining", 0))
        h = data.get("h") or ""
        if _state_hash(code, remaining) != h:
            return None
        return data
    except Exception:
        return None


def save_state(code: str, total: int, remaining: int) -> None:
    """保存核销状态（含校验 hash 防篡改）。"""
    h = _state_hash(code, remaining)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"code": code, "total": total, "remaining": remaining, "h": h}, f, ensure_ascii=False)


def consume_one() -> tuple[bool, int | None, str]:
    """
    扣减 1 次。返回 (成功, 剩余次数, 错误信息)。
    若当前无有效绑定或次数已为 0，返回失败。
    """
    state = load_state()
    if not state:
        return False, None, "请先绑定核销码"
    remaining = int(state["remaining"])
    if remaining <= 0:
        return False, None, "该码次数已用尽"
    remaining -= 1
    save_state(state["code"], state["total"], remaining)
    return True, remaining, ""
