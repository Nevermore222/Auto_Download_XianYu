"""
完整流程测试：下载 + 夸克上传（需在 config 中配置夸克 Cookie）。
若未配置 Cookie，可先设 skip_quark=True 只测下载。
运行：在项目根目录执行  python tests/test_full_pipeline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.task import run_pipeline


def main():
    test_url = "https://www.bilibili.com/video/BV1xx411c7mD"  # 可替换
    skip_quark = False  # 使用夸克上传
    print("测试流水线:", test_url, "skip_quark=", skip_quark)
    result = run_pipeline(test_url, skip_quark=skip_quark)
    print("结果:", result)
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
