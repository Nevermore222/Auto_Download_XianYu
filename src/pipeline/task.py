"""
单任务流水线：解析视频链接 → 下载 → 上传夸克 → 返回分享链接。
支持单条与批量；批量时下载到同一文件夹、上传到夸克同一文件夹、生成一个分享链接。
"""
import re
from pathlib import Path
from typing import Callable, List, Optional

from src.common.config import get_download_dir
from src.download.runner import download_video
from src.quark.client import (
    create_batch_folder,
    create_share_link,
    upload_and_share,
    upload_file,
)

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]+')


def _safe_folder_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    name = _INVALID_FS_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    return name[:80]


def _ts() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d_%H%M%S")



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


def run_batch_pipeline(
    task_id: str,
    video_urls: List[str],
    folder_name: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    批量流程：多条链接下载到同一本地文件夹 → 在夸克创建同一批次文件夹 →
    全部上传到该文件夹 → 对该文件夹生成一个分享链接。
    返回: { "success": bool, "share_url": str, "error": str|None }
    """
    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    urls = [u.strip() for u in video_urls if u.strip()]
    if not urls:
        return {"success": False, "share_url": "", "error": "没有有效的视频链接"}

    base_dir = Path(get_download_dir())
    safe = _safe_folder_name(folder_name or "")
    if safe:
        folder = f"{safe}_{_ts()}"
        batch_dir = base_dir / folder
        batch_name = folder
    else:
        batch_dir = base_dir / f"batch_{task_id}"
        batch_name = f"视频批次_{task_id[:8]}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    downloaded: List[Path] = []
    try:
        for i, url in enumerate(urls):
            log(f"正在下载第 {i + 1}/{len(urls)} 条…")
            path = download_video(
                url,
                output_dir=str(batch_dir),
                prefer_small_format=False,
                batch_index=i + 1,
            )
            downloaded.append(path)
            log(f"第 {i + 1} 条下载完成")
    except Exception as e:
        return {"success": False, "share_url": "", "error": f"下载失败: {e}"}

    try:
        log("正在创建夸克文件夹…")
        folder_fid = create_batch_folder(batch_name)
        log(f"正在上传到夸克（共 {len(downloaded)} 个文件）…")
        for i, path in enumerate(downloaded):
            log(f"正在上传第 {i + 1}/{len(downloaded)} 个文件…")
            upload_file(path, pdir_id=folder_fid)
        log("正在生成分享链接…")
        share_url = create_share_link(folder_fid, title=batch_name)
        return {"success": True, "share_url": share_url, "error": None}
    except Exception as e:
        return {"success": False, "share_url": "", "error": f"上传或分享失败: {e}"}
