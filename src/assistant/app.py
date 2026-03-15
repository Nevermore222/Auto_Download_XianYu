"""
助手服务：接收「客户发来的视频链接」→ 转发给下载系统 → 将云盘链接返回。
提供 HTTP API + 简单 Web 页，便于对接闲鱼自动化或人工复制。
"""
import json
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.common.config import load_config


class OrderIn(BaseModel):
    """来自闲鱼或前端的订单：客户发的视频链接。"""
    video_url: str
    order_id: Optional[str] = None
    skip_quark: bool = False
    prefer_small_format: bool = False  # 小体积格式，避免超过夸克 10MB 限制


class OrderOut(BaseModel):
    success: bool
    share_url: str
    order_id: Optional[str] = None
    error: Optional[str] = None


def get_system_base_url() -> str:
    cfg = load_config().get("assistant", {})
    return cfg.get("system_base_url", "http://127.0.0.1:8766").rstrip("/")


app = FastAPI(title="闲鱼视频代下载-助手")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.post("/order", response_model=OrderOut)
def create_order(in_body: OrderIn) -> OrderOut:
    """
    接单：把客户提供的视频链接转发给下载系统，拿到云盘链接后返回。
    闲鱼侧或 Web 页可调用此接口；回传的 share_url 由您再发回给客户。
    """
    url = in_body.video_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="video_url 不能为空")

    base = get_system_base_url()
    try:
        r = requests.post(
            f"{base}/task",
            json={
                "video_url": url,
                "skip_quark": in_body.skip_quark,
                "prefer_small_format": in_body.prefer_small_format,
            },
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return OrderOut(success=False, share_url="", order_id=in_body.order_id, error=str(e))

    return OrderOut(
        success=data.get("success", False),
        share_url=data.get("share_url", ""),
        order_id=in_body.order_id,
        error=data.get("error"),
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """简单 Web 页：输入视频链接，提交后显示云盘链接（可复制给客户）。"""
    html = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>视频代下载 - 助手</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: "Microsoft YaHei", sans-serif; max-width: 560px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; color: #333; }
    label { display: block; margin-top: 0.75rem; color: #555; }
    input[type="url"], input[type="text"] { width: 100%; padding: 0.5rem; margin-top: 0.25rem; border: 1px solid #ccc; border-radius: 4px; }
    button { margin-top: 1rem; padding: 0.5rem 1.5rem; background: #ff6700; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
    button:disabled { background: #ccc; cursor: not-allowed; }
    .result { margin-top: 1.5rem; padding: 1rem; background: #f5f5f5; border-radius: 4px; white-space: pre-wrap; word-break: break-all; }
    .err { color: #c00; }
    .ok { color: #080; }
  </style>
</head>
<body>
  <h1>视频代下载助手</h1>
  <p>输入客户发来的视频链接，提交后等待系统下载并上传夸克，将云盘链接复制给客户即可。</p>
  <form id="f">
    <label>视频链接</label>
    <input type="url" name="video_url" id="video_url" placeholder="https://www.bilibili.com/video/..." required>
    <label>订单号（选填）</label>
    <input type="text" name="order_id" id="order_id" placeholder="闲鱼订单号">
    <label><input type="checkbox" name="skip_quark" id="skip_quark"> 仅下载测试（不上传夸克）</label><br>
    <label><input type="checkbox" name="prefer_small_format" id="prefer_small_format"> 小体积格式（避免超过夸克 10MB 限制）</label>
    <button type="submit" id="btn">提交</button>
  </form>
  <div id="out" class="result" style="display:none;"></div>
  <script>
    document.getElementById("f").onsubmit = async (e) => {
      e.preventDefault();
      var btn = document.getElementById("btn");
      var out = document.getElementById("out");
      out.style.display = "block";
      out.className = "result";
      out.textContent = "处理中，请稍候…";
      btn.disabled = true;
      try {
        var r = await fetch("/order", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            video_url: document.getElementById("video_url").value.trim(),
            order_id: document.getElementById("order_id").value.trim() || null,
            skip_quark: document.getElementById("skip_quark").checked,
            prefer_small_format: document.getElementById("prefer_small_format").checked
          })
        });
        var d = await r.json();
        if (d.success) {
          out.className = "result ok";
          out.textContent = "云盘链接（请复制给客户）：\\n" + d.share_url;
        } else {
          out.className = "result err";
          out.textContent = "失败：" + (d.error || "未知错误");
        }
      } catch (err) {
        out.className = "result err";
        out.textContent = "请求失败：" + err.message;
      }
      btn.disabled = false;
    };
  </script>
</body>
</html>
"""
    return HTMLResponse(html)


def main():
    import uvicorn
    cfg = load_config().get("assistant", {})
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8765))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
