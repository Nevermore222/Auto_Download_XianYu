"""
客户自助网站：输入视频链接 → 查价 → 下单 → 获得夸克下载链接。
定价按时长与分辨率；下单异步处理，支持轮询状态，便于负载均衡扩展。
"""
import re
import threading
import uuid
from contextlib import asynccontextmanager
import io
from typing import List

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.common.config import load_config, save_config, set_cfg_value
from src.download.runner import get_video_info
from src.pipeline.task import run_batch_pipeline, run_pipeline

# 内存任务表：task_id -> { status, share_url?, error? }（可后续改为 Redis 做多机负载均衡）
_task_store: dict = {}
_store_lock = threading.Lock()


_URL_RE = re.compile(r"(https?://\S+)")


def _extract_urls(raw_list: list[str]) -> list[str]:
    urls: list[str] = []
    for raw in raw_list:
        if not raw:
            continue
        for m in _URL_RE.finditer(raw):
            # 使用第一个捕获组返回完整 URL
            urls.append(m.group(1))
    return urls


def _extract_urls_from_text(text: str) -> list[str]:
    if not text:
        return []
    return _extract_urls([text])


def _calc_price(duration_seconds: int, height: int) -> tuple[float, list[str]]:
    """根据配置计算价格（元）及说明。"""
    cfg = load_config().get("store", {}).get("pricing", {})
    duration_rules = cfg.get("duration") or [
        {"max_minutes": 1, "price": 0.5},
        {"max_minutes": 5, "price": 1.5},
        {"max_minutes": 30, "price": 3},
        {"max_minutes": 9999, "price": 6},
    ]
    res_add = cfg.get("resolution_add") or [
        {"min_height": 721, "add": 0.5},
        {"min_height": 1081, "add": 1},
    ]
    minutes = max(0, duration_seconds) / 60.0
    price = 0.0
    breakdown = []

    for r in sorted(duration_rules, key=lambda x: x["max_minutes"]):
        if minutes <= r["max_minutes"]:
            price = float(r["price"])
            breakdown.append(f"时长 {minutes:.1f} 分钟 → {price} 元")
            break
    for r in sorted(res_add, key=lambda x: x["min_height"], reverse=True):
        if height >= r["min_height"]:
            add = float(r["add"])
            price += add
            breakdown.append(f"分辨率 ≥{r['min_height']}p 加价 {add} 元")
            break

    return round(price, 2), breakdown


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    yield


app = FastAPI(title="视频代下载 - 客户自助", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class QuoteIn(BaseModel):
    video_url: str


class QuoteOut(BaseModel):
    ok: bool
    title: str
    duration_seconds: int
    duration_text: str
    height: int
    resolution_text: str
    price: float
    price_breakdown: list[str]
    error: str | None = None


class QuoteBatchIn(BaseModel):
    video_urls: list[str]


class QuoteItemOut(BaseModel):
    ok: bool
    title: str
    duration_text: str
    resolution_text: str
    price: float
    error: str | None = None


class QuoteBatchOut(BaseModel):
    items: list[QuoteItemOut]
    total_price: float
    count: int


class OrderIn(BaseModel):
    video_url: str | None = None  # 单条时可用
    video_urls: list[str] | None = None  # 多条时使用，与 video_url 二选一
    folder_name: str | None = None  # 选填：若填写则创建“文件夹名_时间戳”作为批次目录与夸克文件夹名


class OrderOut(BaseModel):
    task_id: str


class OrderStatusOut(BaseModel):
    status: str  # pending | processing | success | failed
    step: str | None = None  # 当前步骤说明，便于显示进度日志
    share_url: str | None = None
    error: str | None = None


class WebConfigOut(BaseModel):
    quark_cookie: str
    douyin_cookie: str


class WebConfigIn(BaseModel):
    quark_cookie: str | None = None
    douyin_cookie: str | None = None


@app.get("/api/config", response_model=WebConfigOut)
def get_web_config() -> WebConfigOut:
    cfg = load_config() or {}
    return WebConfigOut(
        quark_cookie=(cfg.get("quark", {}) or {}).get("cookie", "") or "",
        douyin_cookie=(cfg.get("download", {}) or {}).get("douyin_cookie", "") or "",
    )


@app.post("/api/config", response_model=WebConfigOut)
def save_web_config(req: WebConfigIn) -> WebConfigOut:
    cfg = load_config() or {}
    if req.quark_cookie is not None:
        set_cfg_value(cfg, ["quark", "cookie"], (req.quark_cookie or "").strip())
    if req.douyin_cookie is not None:
        set_cfg_value(cfg, ["download", "douyin_cookie"], (req.douyin_cookie or "").strip())
    save_config(cfg)
    return WebConfigOut(
        quark_cookie=(cfg.get("quark", {}) or {}).get("cookie", "") or "",
        douyin_cookie=(cfg.get("download", {}) or {}).get("douyin_cookie", "") or "",
    )


def _format_duration(sec: int) -> str:
    if sec < 60:
        return f"{sec} 秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} 分 {s} 秒"
    h, m = divmod(m, 60)
    return f"{h} 时 {m} 分"


@app.post("/api/quote", response_model=QuoteOut)
def quote(req: QuoteIn) -> QuoteOut:
    """根据链接返回视频信息与报价。"""
    url = (req.video_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请填写视频链接")
    info = get_video_info(url)
    if not info.get("ok"):
        return QuoteOut(
            ok=False,
            title="",
            duration_seconds=0,
            duration_text="",
            height=0,
            resolution_text="",
            price=0,
            price_breakdown=[],
            error=info.get("error", "解析失败"),
        )
    duration = info.get("duration") or 0
    height = info.get("height") or 0
    price, breakdown = _calc_price(duration, height)
    res_text = f"{height}p" if height else "未知"
    return QuoteOut(
        ok=True,
        title=info.get("title", "未命名"),
        duration_seconds=duration,
        duration_text=_format_duration(duration),
        height=height,
        resolution_text=res_text,
        price=price,
        price_breakdown=breakdown,
        error=None,
    )


@app.post("/api/quote_batch", response_model=QuoteBatchOut)
def quote_batch(req: QuoteBatchIn) -> QuoteBatchOut:
    """批量报价：多条链接统一返回每条信息与总价。"""
    # 允许一行里包含标题+URL，这里自动从文本中提取出所有 URL
    urls = _extract_urls(req.video_urls or [])
    if not urls:
        raise HTTPException(status_code=400, detail="请填写至少一条视频链接")
    items: list[QuoteItemOut] = []
    total = 0.0
    for url in urls:
        info = get_video_info(url)
        if not info.get("ok"):
            items.append(QuoteItemOut(ok=False, title="", duration_text="", resolution_text="", price=0, error=info.get("error", "解析失败")))
            continue
        dur = info.get("duration") or 0
        h = info.get("height") or 0
        price, _ = _calc_price(dur, h)
        total += price
        items.append(QuoteItemOut(
            ok=True,
            title=(info.get("title") or "未命名")[:50],
            duration_text=_format_duration(dur),
            resolution_text=f"{h}p" if h else "未知",
            price=round(price, 2),
            error=None,
        ))
    return QuoteBatchOut(items=items, total_price=round(total, 2), count=len(items))


def _run_task(task_id: str, video_urls: list[str], folder_name: str | None = None) -> None:
    def progress_step(msg: str) -> None:
        with _store_lock:
            _task_store[task_id]["step"] = msg

    with _store_lock:
        _task_store[task_id]["status"] = "processing"
        _task_store[task_id]["step"] = "准备中…"
    try:
        # 若填写了 folder_name，则强制按批次模式：即使只有 1 条，也创建同名文件夹并分享文件夹链接
        if (not (folder_name or "").strip()) and len(video_urls) == 1:
            result = run_pipeline(
                video_urls[0],
                skip_quark=False,
                prefer_small_format=False,
                progress_callback=progress_step,
            )
        else:
            result = run_batch_pipeline(task_id, video_urls, folder_name=folder_name, progress_callback=progress_step)
        with _store_lock:
            if result.get("success"):
                _task_store[task_id]["status"] = "success"
                _task_store[task_id]["step"] = "处理完成"
                _task_store[task_id]["share_url"] = result.get("share_url", "")
            else:
                _task_store[task_id]["status"] = "failed"
                _task_store[task_id]["step"] = None
                _task_store[task_id]["error"] = result.get("error", "未知错误")
    except Exception as e:
        with _store_lock:
            _task_store[task_id]["status"] = "failed"
            _task_store[task_id]["step"] = None
            _task_store[task_id]["error"] = str(e)


@app.post("/api/order", response_model=OrderOut)
def create_order(req: OrderIn) -> OrderOut:
    """提交订单（单条或批量），立即返回 task_id；批量时下载到同一文件夹、上传到夸克同一文件夹、生成一个分享链接。"""
    urls: list[str] = []
    # 统一用 _extract_urls，从文本中提取 http/https 链接；支持“标题 + URL”格式
    if req.video_urls:
        urls = _extract_urls(req.video_urls)
    if req.video_url and not urls:
        urls = _extract_urls([req.video_url])
    if not urls:
        raise HTTPException(status_code=400, detail="请填写至少一条视频链接")
    task_id = str(uuid.uuid4())
    with _store_lock:
        _task_store[task_id] = {"status": "pending", "step": None, "share_url": None, "error": None}
    t = threading.Thread(target=_run_task, args=(task_id, urls, req.folder_name), daemon=True)
    t.start()
    return OrderOut(task_id=task_id)


@app.post("/api/extract_links")
async def extract_links_from_file(file: UploadFile = File(...)) -> dict:
    """
    从上传的文档中提取所有 http/https 链接。
    支持：
    - 文本：.txt（utf-8/gbk 等，自动忽略无法解码字符）
    - Word：.docx
    - Excel：.xlsx
    返回: { "urls": [ ... ] }
    """
    filename = (file.filename or "").lower()
    content = await file.read()

    urls: List[str] = []

    try:
        if filename.endswith(".txt") or not filename:
            # 尝试多种常见编码解码文本
            for enc in ("utf-8", "gbk", "gb2312"):
                try:
                    text = content.decode(enc, errors="ignore")
                    break
                except Exception:
                    text = ""
            urls = _extract_urls_from_text(text)
        elif filename.endswith(".docx"):
            from docx import Document

            doc = Document(io.BytesIO(content))
            parts: List[str] = []
            for p in doc.paragraphs:
                if p.text:
                    parts.append(p.text)
            text = "\n".join(parts)
            urls = _extract_urls_from_text(text)
        elif filename.endswith(".xlsx"):
            import openpyxl
            import io as _io

            wb = openpyxl.load_workbook(_io.BytesIO(content), data_only=True)
            parts: List[str] = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for cell in row:
                        if isinstance(cell, str):
                            parts.append(cell)
            text = "\n".join(parts)
            urls = _extract_urls_from_text(text)
        else:
            # 其他扩展名按文本尝试处理
            text = content.decode("utf-8", errors="ignore")
            urls = _extract_urls_from_text(text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析文件失败: {e}")

    # 去重，保持原顺序
    seen = set()
    uniq: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return {"urls": uniq}


@app.get("/api/order/{task_id}", response_model=OrderStatusOut)
def get_order_status(task_id: str) -> OrderStatusOut:
    """轮询订单状态与结果。"""
    with _store_lock:
        rec = _task_store.get(task_id)
    if not rec:
        raise HTTPException(status_code=404, detail="订单不存在")
    return OrderStatusOut(
        status=rec["status"],
        step=rec.get("step"),
        share_url=rec.get("share_url"),
        error=rec.get("error"),
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """自助页：输入链接 → 查价 → 下单 → 获取夸克链接。"""
    html = _INDEX_HTML
    return HTMLResponse(html)


_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <meta name="description" content="卖家工作台：输入链接查价，提交后自动下载并上传夸克生成链接">
  <title>视频代下载 - 卖家工作台</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0f0f12;
      --card: #1a1a1f;
      --border: #2a2a32;
      --text: #e8e8ed;
      --muted: #8888a0;
      --accent: #ff8c42;
      --accent-hover: #ffa366;
      --success: #4ade80;
      --err: #f87171;
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body {
      font-family: "Noto Sans SC", sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      margin: 0;
      padding: 1rem;
      padding-bottom: 2rem;
      background-image: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(255,140,66,0.12), transparent),
                      radial-gradient(ellipse 60% 40% at 80% 50%, rgba(100,100,140,0.08), transparent);
    }
    .container { max-width: 520px; margin: 0 auto; width: 100%; }
    h1 { font-size: 1.35rem; font-weight: 700; text-align: center; margin-bottom: 0.4rem; }
    .sub {
      text-align: center; color: var(--muted); font-size: 0.85rem; margin-bottom: 1.5rem;
      line-height: 1.5; padding: 0 0.5rem;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1.25rem;
      margin-bottom: 1rem;
    }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 0.35rem; }
    input[type="url"], input[type="text"] {
      width: 100%;
      padding: 0.85rem 1rem;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--bg);
      color: var(--text);
      font-size: 16px;
    }
    input::placeholder { color: var(--muted); }
    input:focus { outline: none; border-color: var(--accent); }
    .btn {
      display: inline-block;
      padding: 0.85rem 1.25rem;
      min-height: 48px;
      border: none;
      border-radius: 10px;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.2s, transform 0.05s;
      font-family: inherit;
    }
    .btn:active { transform: scale(0.98); }
    .btn-primary { background: var(--accent); color: #1a1a1f; }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
    .btn-secondary { background: var(--border); color: var(--text); margin-top: 0.5rem; }
    .btn-secondary:hover { background: #3a3a45; }
    .row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; }
    .row .btn { flex: 1; min-width: 120px; }
    textarea { width: 100%; min-height: 100px; padding: 0.85rem 1rem; border: 1px solid var(--border); border-radius: 10px; background: var(--bg); color: var(--text); font-size: 14px; resize: vertical; font-family: inherit; }
    textarea::placeholder { color: var(--muted); }
    textarea:focus { outline: none; border-color: var(--accent); }
    .quote-card .title { font-weight: 600; margin-bottom: 0.4rem; }
    .quote-card .meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 0.4rem; }
    .quote-card .price { font-size: 1.35rem; font-weight: 700; color: var(--accent); margin: 0.5rem 0; }
    .quote-card .breakdown { font-size: 0.8rem; color: var(--muted); }
    .quote-batch-list { max-height: 200px; overflow-y: auto; margin: 0.5rem 0; font-size: 0.85rem; }
    .quote-batch-list li { padding: 0.35rem 0; border-bottom: 1px solid var(--border); list-style: none; }
    .quote-batch-total { font-weight: 700; color: var(--accent); margin-top: 0.5rem; }
    .result-card .status { font-weight: 600; margin-bottom: 0.5rem; }
    .result-card .status.processing { color: var(--accent); }
    .result-card .status.success { color: var(--success); }
    .result-card .status.failed { color: var(--err); }
    .result-card .link-wrap {
      margin-top: 0.75rem;
      padding: 0.85rem;
      background: var(--bg);
      border-radius: 10px;
      word-break: break-all;
      font-size: 0.9rem;
    }
    .result-card .copy-btn { margin-top: 0.6rem; width: 100%; min-height: 48px; }
    .err-msg { color: var(--err); font-size: 0.9rem; margin-top: 0.5rem; }
    .hidden { display: none !important; }
    #outQuote, #outOrder, #outResult { margin-top: 0.75rem; }
    .mini { font-size: 0.8rem; color: var(--muted); line-height: 1.5; }
    .cfg-actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
    .cfg-actions .btn { flex: 1; }
    @media (max-width: 400px) {
      body { padding: 0.75rem; }
      .card { padding: 1rem; }
      .btn { padding: 0.75rem 1rem; min-height: 44px; }
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>卖家工作台</h1>
    <p class="sub">支持多条链接：每行一条，统一估价；批量时下载到同一文件夹并上传到夸克同一文件夹，生成一个链接发给客户</p>

    <div class="card">
      <label for="video_urls">客户视频链接（每行一条，B站 / 抖音 / 等）</label>
      <textarea id="video_urls" placeholder="https://www.bilibili.com/video/...&#10;https://...&#10;（可粘贴多条）" autocomplete="off"></textarea>
      <label for="file_input" style="margin-top:0.75rem;">或上传包含链接的文档（txt / docx / xlsx）</label>
      <input type="file" id="file_input" accept=".txt,.docx,.xlsx">
      <label for="folder_name" style="margin-top:0.75rem;">批次文件夹名（选填：填写后会自动追加时间戳，用于本地下载目录与夸克网盘文件夹名）</label>
      <input type="text" id="folder_name" placeholder="例如：客户张三_订单123（不填则默认单文件）" autocomplete="off">
      <div class="row">
        <button type="button" class="btn btn-primary" id="btnQuote">查询价格（报给客户）</button>
        <button type="button" class="btn btn-secondary" id="btnOrder">提交并处理（下载→上传夸克→生成一个链接）</button>
      </div>
    </div>

    <div class="card">
      <div style="font-weight:700;">Cookie 配置（保存后立即生效）</div>
      <div class="mini">抖音近期开启了 Cookie 校验：若查询价格提示需要 Fresh cookies，请在此粘贴浏览器里复制的 Cookie 字符串。</div>
      <label for="cfg_quark_cookie" style="margin-top:0.75rem;">夸克 Cookie（用于上传与生成分享链接）</label>
      <textarea id="cfg_quark_cookie" placeholder="粘贴完整 Cookie（如 __puus=...; ...）" autocomplete="off" style="min-height:86px;"></textarea>
      <label for="cfg_douyin_cookie" style="margin-top:0.75rem;">抖音 Cookie（用于解析抖音链接报价/下载）</label>
      <textarea id="cfg_douyin_cookie" placeholder="粘贴抖音网页版 Cookie（不一定要登录，但需要新鲜）" autocomplete="off" style="min-height:86px;"></textarea>
      <div class="cfg-actions">
        <button type="button" class="btn btn-secondary" id="btnCfgReload">读取当前配置</button>
        <button type="button" class="btn btn-primary" id="btnCfgSave">保存配置</button>
      </div>
      <div class="err-msg hidden" id="cfgMsg"></div>
    </div>

    <div id="outQuote" class="card quote-card hidden">
      <div class="title" id="quoteTitle">批量报价</div>
      <ul class="quote-batch-list" id="quoteBatchList"></ul>
      <div class="quote-batch-total" id="quoteTotal"></div>
      <div class="err-msg hidden" id="quoteErr"></div>
    </div>

    <div id="outOrder" class="card hidden">
      <div class="status processing" id="orderStatus">准备中…</div>
      <p class="meta" id="orderStepHint">请稍候，完成后将显示夸克链接。</p>
    </div>

    <div id="outResult" class="card result-card hidden">
      <div class="status success" id="resultStatus">处理完成，请复制下方夸克链接发给客户</div>
      <div class="link-wrap" id="resultLink"></div>
      <button type="button" class="btn btn-primary copy-btn" id="btnCopy">复制链接</button>
      <div class="err-msg hidden" id="resultErr"></div>
    </div>
  </div>

  <script>
    function getUrls() {
      var text = document.getElementById('video_urls').value || '';
      // 保守实现：先把换行统一成 \\n，再按空白拆分取 URL
      text = text.replaceAll('\\r\\n', '\\n').replaceAll('\\r', '\\n');
      // 再按空白字符切分（这里不用正则，逐层 split 合并即可）
      var parts = [];
      text.split('\\n').forEach(function(line) {
        (line || '').split('\\t').forEach(function(seg) {
          (seg || '').split(' ').forEach(function(tok) {
            tok = (tok || '').trim();
            if (tok) parts.push(tok);
          });
        });
      });
      var urls = [];
      parts.forEach(function(p) {
        p = (p || '').trim();
        if (!p) return;
        if (p.indexOf('http://') === 0 || p.indexOf('https://') === 0) {
          urls.push(p);
        }
      });
      return urls;
    }

    function showQuoteBatch(data) {
      var out = document.getElementById('outQuote');
      out.classList.remove('hidden');
      document.getElementById('quoteErr').classList.add('hidden');
      var list = document.getElementById('quoteBatchList');
      list.innerHTML = '';
      if (!data || !data.items || data.items.length === 0) {
        document.getElementById('quoteTitle').textContent = '未解析到有效链接';
        document.getElementById('quoteTotal').textContent = '';
        return;
      }
      document.getElementById('quoteTitle').textContent = '共 ' + data.count + ' 条，统一报价';
      data.items.forEach(function(it, i) {
        var li = document.createElement('li');
        if (it.ok) {
          li.textContent = (i + 1) + '. ' + (it.title || '未命名') + ' — ' + it.duration_text + ' · ' + it.resolution_text + ' — ¥' + it.price.toFixed(2);
        } else {
          li.innerHTML = (i + 1) + '. <span style="color:var(--err)">' + (it.error || '解析失败') + '</span>';
        }
        list.appendChild(li);
      });
      document.getElementById('quoteTotal').textContent = '合计：¥ ' + (data.total_price || 0).toFixed(2);
    }

    function showOrderPolling(taskId) {
      document.getElementById('outOrder').classList.remove('hidden');
      document.getElementById('outResult').classList.add('hidden');
      var statusEl = document.getElementById('orderStatus');
      var hintEl = document.getElementById('orderStepHint');
      statusEl.textContent = '准备中…';
      statusEl.className = 'status processing';
      hintEl.textContent = '请稍候，完成后将显示夸克链接。';

      function poll() {
        fetch('/api/order/' + taskId)
          .then(function(r) { return r.json(); })
          .then(function(d) {
            if (d.status === 'processing' && d.step) {
              statusEl.textContent = d.step;
              hintEl.textContent = '正在执行：解析 → 下载 → 上传夸克 → 生成链接';
            }
            if (d.status === 'success') {
              document.getElementById('outOrder').classList.add('hidden');
              document.getElementById('outResult').classList.remove('hidden');
              document.getElementById('resultStatus').textContent = '处理完成，下方为夸克文件夹链接（内含本批全部视频），复制发给客户即可';
              document.getElementById('resultStatus').className = 'status success';
              document.getElementById('resultLink').textContent = d.share_url || '';
              document.getElementById('resultLink').dataset.url = d.share_url || '';
              document.getElementById('resultErr').classList.add('hidden');
              document.getElementById('btnCopy').classList.remove('hidden');
              return;
            }
            if (d.status === 'failed') {
              document.getElementById('outOrder').classList.add('hidden');
              document.getElementById('outResult').classList.remove('hidden');
              document.getElementById('resultStatus').textContent = '处理失败';
              document.getElementById('resultStatus').className = 'status failed';
              document.getElementById('resultErr').textContent = d.error || '未知错误';
              document.getElementById('resultErr').classList.remove('hidden');
              document.getElementById('btnCopy').classList.add('hidden');
              return;
            }
            setTimeout(poll, 2500);
          })
          .catch(function() { setTimeout(poll, 3000); });
      }
      poll();
    }

    document.getElementById('btnQuote').onclick = function() {
      var urls = getUrls();
      if (!urls.length) { alert('请先输入至少一条视频链接（每行一条）'); return; }
      this.disabled = true;
      // 先给出可见反馈，避免“无反应”的感觉
      document.getElementById('outQuote').classList.remove('hidden');
      document.getElementById('quoteTitle').textContent = '正在查询价格…';
      document.getElementById('quoteBatchList').innerHTML = '';
      document.getElementById('quoteTotal').textContent = '';
      document.getElementById('quoteErr').classList.add('hidden');

      var ctrl = (window.AbortController ? new AbortController() : null);
      var timer = ctrl ? setTimeout(function(){ ctrl.abort(); }, 30000) : null; // 30s 超时提示
      fetch('/api/quote_batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_urls: urls }),
        signal: ctrl ? ctrl.signal : undefined
      })
        .then(function(r) {
          if (!r.ok) {
            return r.text().then(function(t){ throw new Error(t || ('HTTP ' + r.status)); });
          }
          return r.json();
        })
        .then(function(d) { showQuoteBatch(d); })
        .catch(function(e) {
          showQuoteBatch({ items: [], total_price: 0, count: 0 });
          var msg = (e && e.name === 'AbortError') ? '查询超时（30秒）。可能链接解析较慢/网络不通，建议分批查询或稍后重试。' : (e.message || '网络错误');
          document.getElementById('quoteErr').textContent = msg;
          document.getElementById('quoteErr').classList.remove('hidden');
        })
        .finally(function() {
          if (timer) clearTimeout(timer);
          document.getElementById('btnQuote').disabled = false;
        });
    };

    function setCfgMsg(text, isErr) {
      var el = document.getElementById('cfgMsg');
      if (!text) { el.classList.add('hidden'); el.textContent = ''; return; }
      el.textContent = text;
      el.classList.remove('hidden');
      el.style.color = isErr ? 'var(--err)' : 'var(--success)';
    }

    function reloadConfig() {
      setCfgMsg('正在读取配置…', false);
      return fetch('/api/config')
        .then(function(r) {
          if (!r.ok) {
            return r.text().then(function(t){ throw new Error(t || ('HTTP ' + r.status)); });
          }
          return r.json();
        })
        .then(function(d) {
          document.getElementById('cfg_quark_cookie').value = (d.quark_cookie || '');
          document.getElementById('cfg_douyin_cookie').value = (d.douyin_cookie || '');
          setCfgMsg('已读取当前配置。', false);
        })
        .catch(function(e) {
          setCfgMsg('读取配置失败：' + (e.message || e), true);
        });
    }

    document.getElementById('btnCfgReload').onclick = function() {
      reloadConfig();
    };

    document.getElementById('btnCfgSave').onclick = function() {
      var quark = (document.getElementById('cfg_quark_cookie').value || '').trim();
      var douyin = (document.getElementById('cfg_douyin_cookie').value || '').trim();
      setCfgMsg('正在保存…', false);
      this.disabled = true;
      fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quark_cookie: quark, douyin_cookie: douyin })
      })
        .then(function(r) {
          if (!r.ok) {
            return r.text().then(function(t){ throw new Error(t || ('HTTP ' + r.status)); });
          }
          return r.json();
        })
        .then(function() {
          setCfgMsg('保存成功，已生效。', false);
        })
        .catch(function(e) {
          setCfgMsg('保存失败：' + (e.message || e), true);
        })
        .finally(function() {
          document.getElementById('btnCfgSave').disabled = false;
        });
    };

    // 页面加载时自动拉取一次配置
    reloadConfig();

    // 从文档中识别链接并填充到文本框
    document.getElementById('file_input').addEventListener('change', function(e) {
      var file = e.target.files[0];
      if (!file) return;
      var formData = new FormData();
      formData.append('file', file);
      fetch('/api/extract_links', {
        method: 'POST',
        body: formData
      })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (!d || !d.urls || !d.urls.length) {
            alert('未在文档中识别到链接');
            return;
          }
          var textarea = document.getElementById('video_urls');
          // 先统一换行再拆分
          var existingText = (textarea.value || '').replaceAll('\\r\\n', '\\n').replaceAll('\\r', '\\n');
          var existing = existingText ? existingText.split('\\n') : [];
          var all = existing.concat(d.urls);
          // 去重
          var seen = {};
          var lines = [];
          all.forEach(function(u) {
            u = (u || '').trim();
            if (!u) return;
            if (!seen[u]) {
              seen[u] = true;
              lines.push(u);
            }
          });
          textarea.value = lines.join('\\n');
          alert('已从文档识别并填入 ' + d.urls.length + ' 条链接');
        })
        .catch(function(err) {
          alert('解析文档失败：' + (err.message || err));
        })
        .finally(function() {
          e.target.value = '';
        });
    });

    document.getElementById('btnOrder').onclick = function() {
      var urls = getUrls();
      if (!urls.length) { alert('请先输入至少一条视频链接（每行一条）'); return; }
      this.disabled = true;
      var folderName = (document.getElementById('folder_name').value || '').trim();
      fetch('/api/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_urls: urls, folder_name: folderName || null })
      })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          document.getElementById('btnOrder').disabled = false;
          showOrderPolling(d.task_id);
        })
        .catch(function(e) {
          document.getElementById('btnOrder').disabled = false;
          alert('提交失败：' + (e.message || '网络错误'));
        });
    };

    document.getElementById('btnCopy').onclick = function() {
      var url = document.getElementById('resultLink').dataset.url;
      if (!url) return;
      navigator.clipboard.writeText(url).then(function() { this.textContent = '已复制'; }.bind(this));
    };
  </script>
</body>
</html>
"""


def main():
    import uvicorn
    import os
    cfg = load_config().get("store", {})
    host = os.getenv("STORE_HOST") or cfg.get("host", "0.0.0.0")
    port = int(os.getenv("STORE_PORT") or cfg.get("port", 8770))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
