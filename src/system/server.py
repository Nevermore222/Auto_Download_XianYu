"""
下载系统 API：接收视频链接，执行下载+上传，返回云盘链接。
供助手或闲鱼侧调用。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.common.config import load_config
from src.pipeline.task import run_pipeline


class TaskRequest(BaseModel):
    video_url: str
    skip_quark: bool = False  # 仅下载不上传（测试用）
    prefer_small_format: bool = False  # 小体积格式，避免超过夸克单次 10MB 限制


class TaskResponse(BaseModel):
    success: bool
    share_url: str
    local_path: str | None
    error: str | None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时预加载配置
    load_config()
    yield
    # 关闭时清理可选
    pass


app = FastAPI(title="视频代下载系统", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.post("/task", response_model=TaskResponse)
def submit_task(req: TaskRequest) -> TaskResponse:
    """提交一条视频链接，返回下载并上传后的夸克分享链接。"""
    url = req.video_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="video_url 不能为空")
    result = run_pipeline(url, skip_quark=req.skip_quark, prefer_small_format=req.prefer_small_format)
    return TaskResponse(**result)


@app.get("/health")
def health():
    return {"status": "ok"}


def main():
    import uvicorn
    cfg = load_config().get("system", {})
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8766))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
