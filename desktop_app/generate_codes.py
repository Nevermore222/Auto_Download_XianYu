"""
生成核销码（您运行后把码发给客户，客户在下载器内绑定并按次核销）。
在项目根目录执行: python -m desktop_app.generate_codes [次数]
或: python desktop_app/generate_codes.py [次数]
"""
import sys
from pathlib import Path

# 保证能导入 redeem_code（开发时从项目根或 desktop_app 运行）
if __name__ == "__main__":
    _root = Path(__file__).resolve().parent.parent
    if _root not in sys.path:
        sys.path.insert(0, str(_root))

try:
    from desktop_app.redeem_code import generate_code
except ImportError:
    from redeem_code import generate_code


def main():
    if len(sys.argv) >= 2:
        try:
            times = int(sys.argv[1])
        except ValueError:
            times = None
    else:
        times = None
    while times is None or times < 1 or times > 65535:
        s = input("请输入可下载次数 (1～65535，直接回车默认 10): ").strip()
        times = 10 if s == "" else (int(s) if s.isdigit() else None)
    code = generate_code(times)
    print(f"\n核销码（{times} 次）:\n  {code}\n\n请将上述核销码发给客户，客户在下载器中「绑定核销码」后即可按次下载，次数用尽后自动失效。\n")


if __name__ == "__main__":
    main()
