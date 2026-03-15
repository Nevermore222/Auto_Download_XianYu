"""
仅测试视频下载（不调用夸克），用于验证 yt-dlp 与 B 站链接。
运行：在项目根目录执行  python tests/test_download.py
"""
import sys
from pathlib import Path

# 保证能导入 src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.download.runner import download_video


def main():
    # 使用 B 站一个短视频测试（可替换为任意支持链接）
    test_url = "https://www.bilibili.com/video/BV1xx411c7mD"  # 示例 BV 号，可换
    print("测试下载:", test_url)
    try:
        path = download_video(test_url)
        print("下载成功:", path)
    except Exception as e:
        print("下载失败:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
