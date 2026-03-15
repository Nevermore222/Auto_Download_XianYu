"""
单任务流水线：解析视频链接 → 下载 → 上传夸克 → 返回分享链接。
支持 progress_callback 上报当前步骤，便于前端显示进度日志。
"""
from pathlib import Path
from typing import Callable, Optional

from src.download.runner import download_video
from src.quark.client import upload_and_share


def run_pipeline(
    video_url: str,
    skip_quark: bool = False,
    prefer_small_format: bool = False,
    max_duration_seconds: Optional[int] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    执行完整流程：下载视频 → 上传夸克 → 返回云盘链接。
    progress_callback(step_msg): 可选，每进行到一步时调用，便于前端显示进度。
    返回: { "success": bool, "share_url": str, "local_path": str|None, "error": str|None }
    """
    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    local_path: Optional[Path] = None
    try:
        log("正在解析并下载视频…")
        local_path = download_video(
            video_url,
            prefer_small_format=prefer_small_format,
            max_duration_seconds=max_duration_seconds,
        )
        log("视频下载完成")
    except Exception as e:
        return {"success": False, "share_url": "", "local_path": None, "error": str(e)}

    if skip_quark:
        return {
            "success": True,
            "share_url": f"[仅下载测试] 本地文件: {local_path}",
            "local_path": str(local_path),
            "error": None,
        }

    try:
        log("正在上传到夸克网盘…")
        share_url = upload_and_share(local_path)
        log("已生成分享链接")
        return {"success": True, "share_url": share_url, "local_path": str(local_path), "error": None}
    except Exception as e:
        err_msg = str(e)
        # 若因文件过大失败，可提示使用小体积格式重试
        if "文件过大" in err_msg or "exceeds the configured maximum" in err_msg:
            err_msg += " 可在助手页勾选「小体积格式」或使用较短视频重试。"
        return {
            "success": False,
            "share_url": "",
            "local_path": str(local_path),
            "error": f"上传夸克失败: {e}",
        }
