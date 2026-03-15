"""
测试助手 API：需先启动「下载系统」(run_system.bat) 再运行本脚本。
请求助手 /order 接口，检查是否返回云盘链接或正确错误信息。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests


def main():
    assistant_url = "http://127.0.0.1:8765"
    order = {
        "video_url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "order_id": "test-order-001",
        "skip_quark": True,
    }
    print("请求助手 /order", order)
    try:
        r = requests.post(f"{assistant_url}/order", json=order, timeout=120)
        r.raise_for_status()
        data = r.json()
        print("响应:", data)
        if data.get("success"):
            print("云盘链接:", data.get("share_url"))
        else:
            print("失败:", data.get("error"))
    except requests.RequestException as e:
        print("请求失败（请先启动 系统+助手）:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
