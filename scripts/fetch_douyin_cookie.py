from pathlib import Path
import json
import sys
import os

from playwright.sync_api import sync_playwright

# 确保可以以脚本方式运行（python scripts/fetch_douyin_cookie.py）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import load_config, save_config, set_cfg_value


OUT_JSON = PROJECT_ROOT / "douyin_cookies.json"


def build_cookie_header(cookies: list[dict]) -> str:
    """
    将 cookies 列表拼成 name=value; name2=value2; ... 字符串，只保留 douyin 域名相关 Cookie。
    """
    parts: list[str] = []
    for c in cookies:
        domain = (c.get("domain") or "").lower()
        if "douyin.com" not in domain:
            continue
        name = c.get("name") or ""
        value = c.get("value") or ""
        if not name:
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://www.douyin.com/", wait_until="load")
        print("请在弹出的浏览器窗口中扫码登录抖音（或账号密码登录）...")
        # 登录完成后停留在抖音域名下任意页面即可
        page.wait_for_url("**douyin.com**", timeout=180_000)
        cookies = page.context.cookies()
        browser.close()

    # 1) 保存原始 cookies 备查
    OUT_JSON.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2) 拼一条可直接用的 Cookie 头
    header = build_cookie_header(cookies)
    header = " ".join(header.split()).strip()  # 去掉多余空白

    # 3) 写回 config/config.yaml 的 download.douyin_cookie，并关闭自动浏览器模式
    cfg = load_config() or {}
    set_cfg_value(cfg, ["download", "douyin_cookies_from_browser"], "")
    set_cfg_value(cfg, ["download", "douyin_cookie"], header)
    save_config(cfg)

    print("已从抖音获取最新 Cookie，并写入 config/config.yaml 的 download.douyin_cookie。")
    print("重启服务后，卖家工作台查询抖音价格会自动使用最新 Cookie。")


if __name__ == "__main__":
    main()

