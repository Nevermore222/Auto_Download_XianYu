"""
客户自助网站：输入视频链接 → 查价 → 下单 → 获得夸克下载链接。
定价按时长与分辨率；下单异步处理，支持轮询状态，便于负载均衡扩展。
"""
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.common.config import load_config
from src.download.runner import get_video_info
from src.pipeline.task import run_batch_pipeline, run_pipeline

# 内存任务表：task_id -> { status, share_url?, error? }（可后续改为 Redis 做多机负载均衡）
_task_store: dict = {}
_store_lock = threading.Lock()


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


class OrderOut(BaseModel):
    task_id: str


class OrderStatusOut(BaseModel):
    status: str  # pending | processing | success | failed
    step: str | None = None  # 当前步骤说明，便于显示进度日志
    share_url: str | None = None
    error: str | None = None


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
    urls = [u.strip() for u in (req.video_urls or []) if u.strip()]
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


def _run_task(task_id: str, video_urls: list[str]) -> None:
    def progress_step(msg: str) -> None:
        with _store_lock:
            _task_store[task_id]["step"] = msg

    with _store_lock:
        _task_store[task_id]["status"] = "processing"
        _task_store[task_id]["step"] = "准备中…"
    try:
        if len(video_urls) == 1:
            result = run_pipeline(
                video_urls[0],
                skip_quark=False,
                prefer_small_format=False,
                progress_callback=progress_step,
            )
        else:
            result = run_batch_pipeline(task_id, video_urls, progress_callback=progress_step)
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
    if req.video_urls:
        urls = [u.strip() for u in req.video_urls if u.strip()]
    if req.video_url and not urls:
        urls = [req.video_url.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="请填写至少一条视频链接")
    task_id = str(uuid.uuid4())
    with _store_lock:
        _task_store[task_id] = {"status": "pending", "step": None, "share_url": None, "error": None}
    t = threading.Thread(target=_run_task, args=(task_id, urls), daemon=True)
    t.start()
    return OrderOut(task_id=task_id)


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
      <div class="row">
        <button type="button" class="btn btn-primary" id="btnQuote">查询价格（报给客户）</button>
        <button type="button" class="btn btn-secondary" id="btnOrder">提交并处理（下载→上传夸克→生成一个链接）</button>
      </div>
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
      var text = document.getElementById('video_urls').value;
      return text.split(/[\\r\\n]+/).map(function(s) { return s.trim(); }).filter(Boolean);
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
      fetch('/api/quote_batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_urls: urls })
      })
        .then(function(r) { return r.json(); })
        .then(function(d) { showQuoteBatch(d); })
        .catch(function(e) { showQuoteBatch({ items: [], total_price: 0, count: 0 }); document.getElementById('quoteErr').textContent = e.message || '网络错误'; document.getElementById('quoteErr').classList.remove('hidden'); })
        .finally(function() { document.getElementById('btnQuote').disabled = false; });
    };

    document.getElementById('btnOrder').onclick = function() {
      var urls = getUrls();
      if (!urls.length) { alert('请先输入至少一条视频链接（每行一条）'); return; }
      this.disabled = true;
      fetch('/api/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_urls: urls })
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
    cfg = load_config().get("store", {})
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8770))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
