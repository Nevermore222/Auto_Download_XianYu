"""使用 yt-dlp 解析并下载视频（支持 B 站、抖音等多平台）。"""
from pathlib import Path
from typing import Any, Optional

import yt_dlp

from src.common.config import load_config, get_download_dir


def get_video_info(video_url: str) -> dict[str, Any]:
    """
    仅拉取视频元数据（不下载），用于报价。
    返回: { "duration": 秒数, "height": 分辨率高度(px), "title": 标题, "ok": True } 或 { "ok": False, "error": "..." }
    """
    ydl_opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
        if not info:
            return {"ok": False, "error": "无法解析该链接"}
        duration = info.get("duration") or 0
        height = 0
        for f in (info.get("formats") or []):
            h = f.get("height") or 0
            if isinstance(h, int) and h > height:
                height = h
        return {
            "ok": True,
            "duration": int(duration) if duration else 0,
            "height": height,
            "title": (info.get("title") or "").strip() or "未命名视频",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def download_video(
    video_url: str,
    output_dir: Optional[str] = None,
    out_filename: Optional[str] = None,
    prefer_small_format: bool = False,
    max_duration_seconds: Optional[int] = None,
    batch_index: Optional[int] = None,
) -> Path:
    """
    下载视频到本地，返回下载好的文件路径。
    batch_index: 批量下载时传入序号（从 1 起），用于生成唯一文件名，避免同目录下多条链接因标题相同而互相覆盖。
    """
    cfg = load_config().get("download", {})
    base_dir = output_dir or get_download_dir()
    Path(base_dir).mkdir(parents=True, exist_ok=True)

    if out_filename:
        outtmpl = str(Path(base_dir) / out_filename)
    else:
        suffix = f"_{batch_index}" if batch_index is not None else ""
        if max_duration_seconds:
            base_name = f"%(title).80s{suffix}_partial_{max_duration_seconds}s.%(ext)s"
        else:
            base_name = f"%(title).80s{suffix}.%(ext)s"
        outtmpl = str(Path(base_dir) / base_name)

    format_str = cfg.get("format", "bestvideo+bestaudio/best")
    if prefer_small_format:
        format_str = cfg.get("format_small") or "bestvideo[height<=480]+bestaudio/best"

    ydl_opts = {
        "format": format_str,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "merge_output_format": cfg.get("merge_format", "mp4"),
        "quiet": False,
        "no_warnings": True,
    }
    # 可选：按站点传入 extractor 参数（如 B 站等），详见 yt-dlp 文档
    extractor_args = cfg.get("extractor_args")
    if extractor_args and isinstance(extractor_args, dict):
        ydl_opts["extractor_args"] = extractor_args

    if max_duration_seconds is not None and max_duration_seconds > 0:
        end = max_duration_seconds
        ydl_opts["download_ranges"] = lambda *_: [{"start_time": 0, "end_time": end}]
    base_path = Path(base_dir)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    # 下载/合并后找最新生成的视频文件
    video_exts = (".mp4", ".mkv", ".webm", ".flv", ".avi")
    candidates = [
        p for p in base_path.iterdir()
        if p.is_file() and p.suffix.lower() in video_exts and ".part" not in p.name
    ]
    if not candidates:
        raise FileNotFoundError(f"下载后未在 {base_dir} 找到视频文件")
    return max(candidates, key=lambda p: p.stat().st_mtime)
