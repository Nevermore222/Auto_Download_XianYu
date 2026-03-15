"""
夸克网盘上传与创建分享链接。
因夸克无官方开放 API，本模块基于 Cookie 调用网页端接口；
单次请求限制约 1MB，大于此尺寸使用分片上传。
若 Cookie 失效请按 docs/夸克Cookie获取.md 重新获取。

实现思路可参考：https://blog.csdn.net/fghjbjhgb/article/details/145800274 （夸克网盘上传接口实现）
"""
import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from src.common.config import load_config

# 夸克单次上传大小限制（实测约 1MB），大于需分片
CHUNK_SIZE = 1024 * 1024  # 1MB 分片
SIMPLE_UPLOAD_MAX = 1024 * 1024  # 小于 1MB 用整块上传

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://pan.quark.cn/",
    "Origin": "https://pan.quark.cn",
}


def _get_cookie() -> str:
    c = load_config().get("quark", {}).get("cookie", "").strip()
    if not c:
        raise ValueError("未配置夸克 Cookie，请填写 config/config.yaml 中 quark.cookie")
    return c


def _get_pdir_id(headers: dict, api_base: str, dir_name: str) -> str:
    pdir_id = "0"
    if not dir_name:
        return pdir_id
    create_dir_url = f"{api_base}/file"
    create_payload = {
        "pr": "ucpro",
        "pro": False,
        "pdir_fid": pdir_id,
        "dir_name": dir_name,
    }
    try:
        r = requests.post(create_dir_url, json=create_payload, headers=headers, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if data.get("data", {}).get("fid"):
                pdir_id = data["data"]["fid"]
    except Exception:
        pass
    return pdir_id


def _upload_simple(local_path: Path, pdir_id: str, headers: dict, api_base: str) -> str:
    """小于 1MB 时整块上传；大于 1MB 走分片上传。"""
    with open(local_path, "rb") as f:
        file_data = f.read()
    upload_url = f"{api_base}/upload?pr=ucpro&fr=pc"
    files = {"file": (local_path.name, file_data, "application/octet-stream")}
    form = {"pdir_fid": pdir_id, "file_name": local_path.name, "pr": "ucpro", "fr": "pc"}
    resp = requests.post(upload_url, data=form, files=files, headers=headers, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"夸克上传失败: HTTP {resp.status_code}, {resp.text[:500]}")
    j = resp.json()
    if j.get("status") not in (0, None):
        raise RuntimeError(f"夸克上传失败: {j}")
    fid = (j.get("data") or {}).get("fid") or (j.get("data") or {}).get("id") or ""
    if not fid:
        raise RuntimeError(f"未返回 fid: {j}")
    return fid


def _req(app_headers: dict, api_base: str, path: str, method: str, json_data: Optional[dict] = None) -> dict:
    """请求夸克 API（JSON），返回解析后的 JSON。"""
    url = f"{api_base}{path}"
    params = {"pr": "ucpro", "fr": "pc"}
    h = {**app_headers, "Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
    if method.upper() == "GET":
        r = requests.get(url, headers=h, params=params, timeout=30)
    else:
        r = requests.post(url, headers=h, params=params, json=json_data or {}, timeout=60)
    r.raise_for_status()
    j = r.json()
    if j.get("status", 0) not in (0, None) and j.get("status", 0) >= 400:
        raise RuntimeError(j.get("message", j))
    return j


def _upload_pre(local_path: Path, pdir_id: str, headers: dict, api_base: str, mime: str) -> dict:
    """预上传，返回 pre 响应（含 bucket、obj_key、upload_id、task_id、auth_info、callback 等）。"""
    size = local_path.stat().st_size
    now = int(time.time() * 1000)
    payload = {
        "ccp_hash_update": True,
        "dir_name": "",
        "file_name": local_path.name,
        "format_type": mime,
        "l_created_at": now,
        "l_updated_at": now,
        "pdir_fid": pdir_id,
        "size": size,
    }
    j = _req(headers, api_base, "/file/upload/pre", "POST", payload)
    data = (j.get("data") or {})
    if not data.get("task_id") or not data.get("upload_id"):
        raise RuntimeError(f"预上传未返回 task_id/upload_id: {j}")
    return j


def _upload_hash(md5_hex: str, sha1_hex: str, task_id: str, headers: dict, api_base: str) -> bool:
    """提交 MD5/SHA1，若服务器已有相同文件则返回 True（秒传完成）。"""
    j = _req(headers, api_base, "/file/update/hash", "POST", {"md5": md5_hex, "sha1": sha1_hex, "task_id": task_id})
    return (j.get("data") or {}).get("finish", False)


def _upload_part(pre: dict, mime: str, part_number: int, chunk: bytes, headers: dict, api_base: str) -> str:
    """上传单个分片，返回 ETag。"""
    data = pre.get("data") or {}
    bucket = data.get("bucket")
    obj_key = data.get("obj_key")
    upload_id = data.get("upload_id")
    task_id = data.get("task_id")
    auth_info = data.get("auth_info")
    if not all([bucket, obj_key, upload_id, task_id, auth_info]):
        raise RuntimeError("预上传数据缺少分片上传所需字段")
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    auth_meta = (
        f"PUT\n\n{mime}\n{now}\n"
        f"x-oss-date:{now}\nx-oss-user-agent:aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit\n"
        f"/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
    )
    auth_payload = {"auth_info": auth_info, "auth_meta": auth_meta, "task_id": task_id}
    auth_j = _req(headers, api_base, "/file/upload/auth", "POST", auth_payload)
    auth_key = (auth_j.get("data") or {}).get("auth_key")
    if not auth_key:
        raise RuntimeError(f"未返回 auth_key: {auth_j}")
    upload_domain = (data.get("upload_url") or "").replace("https://", "").replace("http://", "").strip("/")
    if not upload_domain:
        upload_domain = "oss-cn-hangzhou.aliyuncs.com"
    upload_url = f"https://{bucket}.{upload_domain}/{obj_key}"
    part_headers = {
        "Authorization": auth_key,
        "Content-Type": mime,
        "Referer": "https://pan.quark.cn/",
        "x-oss-date": now,
        "x-oss-user-agent": "aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit",
    }
    r = requests.put(
        upload_url,
        headers=part_headers,
        params={"partNumber": str(part_number), "uploadId": upload_id},
        data=chunk,
        timeout=120,
    )
    r.raise_for_status()
    etag = r.headers.get("ETag", "").strip('"')
    if not etag:
        raise RuntimeError("分片响应未返回 ETag")
    return etag


def _upload_commit(pre: dict, etags: list, headers: dict, api_base: str) -> None:
    """提交分片列表，完成 OSS 侧合并。"""
    data = pre.get("data") or {}
    bucket = data.get("bucket")
    obj_key = data.get("obj_key")
    upload_id = data.get("upload_id")
    task_id = data.get("task_id")
    auth_info = data.get("auth_info")
    callback = data.get("callback")
    if not all([bucket, obj_key, upload_id, task_id, auth_info]):
        raise RuntimeError("预上传数据缺少 commit 所需字段")
    parts_xml = "".join(f"<Part><PartNumber>{i}</PartNumber><ETag>{etag}</ETag></Part>" for i, etag in enumerate(etags, 1))
    xml_body = f'<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n{parts_xml}\n</CompleteMultipartUpload>'
    content_md5 = base64.b64encode(hashlib.md5(xml_body.encode()).digest()).decode()
    callback_b64 = base64.b64encode(json.dumps(callback).encode()).decode()
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    auth_meta = (
        f"POST\n{content_md5}\napplication/xml\n{now}\n"
        f"x-oss-callback:{callback_b64}\nx-oss-date:{now}\nx-oss-user-agent:aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit\n"
        f"/{bucket}/{obj_key}?uploadId={upload_id}"
    )
    auth_payload = {"auth_info": auth_info, "auth_meta": auth_meta, "task_id": task_id}
    auth_j = _req(headers, api_base, "/file/upload/auth", "POST", auth_payload)
    auth_key = (auth_j.get("data") or {}).get("auth_key")
    if not auth_key:
        raise RuntimeError("commit 未返回 auth_key")
    upload_domain = (data.get("upload_url") or "").replace("https://", "").replace("http://", "").strip("/")
    if not upload_domain:
        upload_domain = "oss-cn-hangzhou.aliyuncs.com"
    upload_url = f"https://{bucket}.{upload_domain}/{obj_key}"
    commit_headers = {
        "Authorization": auth_key,
        "Content-MD5": content_md5,
        "Content-Type": "application/xml",
        "Referer": "https://pan.quark.cn/",
        "x-oss-callback": callback_b64,
        "x-oss-date": now,
        "x-oss-user-agent": "aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit",
    }
    r = requests.post(upload_url, headers=commit_headers, params={"uploadId": upload_id}, data=xml_body.encode(), timeout=60)
    r.raise_for_status()


def _upload_finish(pre: dict, headers: dict, api_base: str) -> str:
    """通知夸克完成上传，返回文件 fid。"""
    data = pre.get("data") or {}
    obj_key = data.get("obj_key")
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError("预上传数据缺少 task_id")
    j = _req(headers, api_base, "/file/upload/finish", "POST", {"obj_key": obj_key, "task_id": task_id})
    fid = (j.get("data") or {}).get("fid") or data.get("fid") or ""
    if not fid:
        raise RuntimeError(f"完成上传未返回 fid: {j}")
    return fid


def _upload_chunked(local_path: Path, pdir_id: str, headers: dict, api_base: str) -> str:
    """大文件分片上传，返回 fid。"""
    import mimetypes
    mime, _ = mimetypes.guess_type(local_path.name)
    if not mime:
        mime = "application/octet-stream"
    pre = _upload_pre(local_path, pdir_id, headers, api_base, mime)
    data = pre.get("data") or {}
    task_id = data.get("task_id")
    meta = pre.get("metadata") or {}
    part_size = meta.get("part_size") or CHUNK_SIZE
    size = local_path.stat().st_size
    md5_h = hashlib.md5()
    sha1_h = hashlib.sha1()
    with open(local_path, "rb") as f:
        while True:
            blk = f.read(65536)
            if not blk:
                break
            md5_h.update(blk)
            sha1_h.update(blk)
    if _upload_hash(md5_h.hexdigest(), sha1_h.hexdigest(), task_id, headers, api_base):
        return _upload_finish(pre, headers, api_base)
    etags = []
    part_number = 1
    with open(local_path, "rb") as f:
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            etags.append(_upload_part(pre, mime, part_number, chunk, headers, api_base))
            part_number += 1
    _upload_commit(pre, etags, headers, api_base)
    time.sleep(1)
    return _upload_finish(pre, headers, api_base)


def upload_file(local_path: Path, remote_dir: Optional[str] = None) -> str:
    """
    上传本地文件到夸克网盘，返回网盘中的文件 id（用于后续分享）。
    小于 1MB 整块上传，大于 1MB 自动分片上传。
    """
    cookie = _get_cookie()
    cfg = load_config().get("quark", {})
    dir_name = remote_dir if remote_dir is not None else cfg.get("upload_dir", "")
    api_base = (cfg.get("api_base") or "https://drive.quark.cn/1/clouddrive").rstrip("/")
    headers = {**DEFAULT_HEADERS, "Cookie": cookie}
    pdir_id = _get_pdir_id(headers, api_base, dir_name)
    size = local_path.stat().st_size

    # 小文件可走整块上传；若整块接口 404 则统一走分片流程（pre -> hash/parts -> commit -> finish）
    if size <= SIMPLE_UPLOAD_MAX:
        try:
            return _upload_simple(local_path, pdir_id, headers, api_base)
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                return _upload_chunked(local_path, pdir_id, headers, api_base)
            raise
    return _upload_chunked(local_path, pdir_id, headers, api_base)


def create_share_link(fid: str, title: Optional[str] = None) -> str:
    """根据文件 fid 创建分享链接，返回可发给客户的 URL。"""
    cookie = _get_cookie()
    cfg = load_config().get("quark", {})
    api_base = (cfg.get("api_base") or "https://drive.quark.cn/1/clouddrive").rstrip("/")
    headers = {**DEFAULT_HEADERS, "Cookie": cookie}

    # 创建分享任务（fid_list + title，与网页端一致）
    payload = {
        "fid_list": [fid],
        "title": title or "分享",
        "url_type": 1,
        "expired_type": 1,
    }
    j = _req(headers, api_base, "/share", "POST", payload)
    data = j.get("data") or {}

    # 若直接返回分享链接
    share_url = data.get("share_url") or data.get("url")
    if share_url:
        return share_url if share_url.startswith("http") else f"https://pan.quark.cn/s/{share_url}"

    # 异步任务：轮询 task 再取分享链接
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError(f"未返回分享链接或 task_id: {j}")

    for _ in range(30):
        time.sleep(2)
        task_r = requests.get(
            f"{api_base}/task",
            headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
            params={"pr": "ucpro", "fr": "pc", "task_id": task_id, "retry_index": 0},
            timeout=30,
        )
        task_r.raise_for_status()
        task_j = task_r.json()
        task_data = (task_j.get("data") or {})
        status = task_data.get("status")

        if status == 2:
            share_id = task_data.get("share_id")
            if not share_id:
                raise RuntimeError(f"分享成功但无 share_id: {task_j}")
            info_j = _req(headers, api_base, "/share/password", "POST", {"share_id": share_id})
            info_data = (info_j.get("data") or {})
            share_url = info_data.get("share_url") or info_data.get("url") or ""
            if share_url:
                return share_url if share_url.startswith("http") else f"https://pan.quark.cn/s/{share_url}"
            raise RuntimeError(f"未返回分享链接: {info_j}")
        if status in (3, 4):
            raise RuntimeError(f"分享任务失败或已取消: status={status}, {task_j}")

    raise RuntimeError("创建分享超时：轮询未在限定时间内完成")


def upload_and_share(local_path: Path, remote_dir: Optional[str] = None) -> str:
    """上传文件并生成分享链接，返回可直接发给客户的链接。"""
    fid = upload_file(local_path, remote_dir)
    return create_share_link(fid, title=local_path.name)
