"""
视频收费下载 - 电脑端桌面应用。
用户输入视频链接（优先适配 B 站），解析后显示时长/分辨率与价格，下载到本地。
"""
import json
import os
import queue
import sys
import threading
from pathlib import Path

import customtkinter as ctk
import yt_dlp

try:
    from desktop_app.redeem_code import verify_code, load_state, save_state, consume_one
except ImportError:
    from redeem_code import verify_code, load_state, save_state, consume_one

# 应用目录：打包成 exe 后为 exe 所在目录，否则为当前脚本目录
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {"default_save_dir": "", "pricing": {"duration": [], "resolution_add": []}}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_default_save_dir():
    cfg = load_config()
    d = (cfg.get("default_save_dir") or "").strip()
    if d and os.path.isabs(d):
        return d
    if d:
        return os.path.abspath(d)
    # 默认：用户视频文件夹
    home = os.path.expanduser("~")
    for name in ["Videos", "视频", "我的视频"]:
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return p
    return os.path.join(home, "Videos")


def get_video_info(video_url: str):
    """仅解析元数据，不下载。返回 dict: ok, title, duration, height, error."""
    opts = {"noplaylist": True, "quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
        if not info:
            return {"ok": False, "error": "无法解析该链接"}
        duration = int(info.get("duration") or 0)
        height = 0
        for f in (info.get("formats") or []):
            h = f.get("height")
            if isinstance(h, int) and h > height:
                height = h
        return {
            "ok": True,
            "title": (info.get("title") or "").strip() or "未命名视频",
            "duration": duration,
            "height": height,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calc_price(duration_sec: int, height: int) -> tuple[float, list]:
    cfg = load_config()
    pricing = cfg.get("pricing") or {}
    dr = pricing.get("duration") or [
        {"max_minutes": 1, "price": 0.5},
        {"max_minutes": 5, "price": 1.5},
        {"max_minutes": 30, "price": 3},
        {"max_minutes": 9999, "price": 6},
    ]
    res = pricing.get("resolution_add") or [
        {"min_height": 721, "add": 0.5},
        {"min_height": 1081, "add": 1},
    ]
    minutes = max(0, duration_sec) / 60.0
    price = 0.0
    breakdown = []
    for r in sorted(dr, key=lambda x: x["max_minutes"]):
        if minutes <= r["max_minutes"]:
            price = float(r["price"])
            breakdown.append(f"时长 {minutes:.1f} 分钟 → {price} 元")
            break
    for r in sorted(res, key=lambda x: x["min_height"], reverse=True):
        if height >= r["min_height"]:
            price += float(r["add"])
            breakdown.append(f"分辨率 ≥{r['min_height']}p 加价 {r['add']} 元")
            break
    return round(price, 2), breakdown


def format_duration(sec: int) -> str:
    if sec < 60:
        return f"{sec} 秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} 分 {s} 秒"
    h, m = divmod(m, 60)
    return f"{h} 时 {m} 分"


def download_video(video_url: str, save_dir: str, progress_callback=None, log_callback=None):
    """
    下载视频到 save_dir。progress_callback(percent, status) 与 log_callback(msg) 可选。
    返回 (success, path_or_error)。
    """
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    out_tmpl = str(Path(save_dir) / "%(title).100s.%(ext)s")

    def progress_hook(d):
        if progress_callback and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            if total and total > 0:
                progress_callback(100 * done / total, "下载中…")
        elif progress_callback and d.get("status") == "finished":
            progress_callback(100, "合并中…")

    def log(msg):
        if log_callback:
            log_callback(msg)

    opts = {
        "outtmpl": out_tmpl,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "progress_hooks": [progress_hook],
        "quiet": not log_callback,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
        # 查找最新生成的文件
        exts = (".mp4", ".mkv", ".webm", ".flv")
        candidates = [
            Path(save_dir) / f
            for f in os.listdir(save_dir)
            if Path(f).suffix.lower() in exts and ".part" not in f
        ]
        if not candidates:
            return False, "未找到下载文件"
        out_path = max(candidates, key=lambda p: os.path.getmtime(p))
        return True, str(out_path)
    except Exception as e:
        return False, str(e)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("视频收费下载 - 支持 B 站等多平台")
        self.geometry("640x520")
        self.minsize(520, 420)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.video_url = ""
        self.video_info = None
        self.save_dir = get_default_save_dir()
        self.download_thread = None
        self.msg_queue = queue.Queue()
        self.redeem_state = load_state()  # { code, total, remaining } or None

        self._build_ui()
        self._refresh_redeem_ui()
        self.after(100, self._process_queue)

    def _build_ui(self):
        pad = 12
        # 顶部：链接输入
        f_url = ctk.CTkFrame(self, fg_color="transparent")
        f_url.pack(fill="x", padx=pad, pady=(pad, 6))
        ctk.CTkLabel(f_url, text="视频链接（B站 / 抖音 / 等）").pack(anchor="w")
        self.entry_url = ctk.CTkEntry(
            f_url, placeholder_text="https://www.bilibili.com/video/...", height=36
        )
        self.entry_url.pack(fill="x", pady=(4, 0))

        # 按钮行：解析 + 下载
        f_btn = ctk.CTkFrame(self, fg_color="transparent")
        f_btn.pack(fill="x", padx=pad, pady=6)
        self.btn_parse = ctk.CTkButton(f_btn, text="解析视频", command=self._on_parse, width=100)
        self.btn_parse.pack(side="left", padx=(0, 8))
        self.btn_download = ctk.CTkButton(
            f_btn, text="开始下载", command=self._on_download, width=100, state="disabled"
        )
        self.btn_download.pack(side="left")

        # 核销码：绑定后按次下载，次数用尽自动失效（下载器内不单独收费）
        f_redeem = ctk.CTkFrame(self, fg_color=("gray90", "gray18"), corner_radius=8)
        f_redeem.pack(fill="x", padx=pad, pady=6)
        ctk.CTkLabel(f_redeem, text="核销码（由卖家提供，绑定后按次下载）").pack(anchor="w", padx=10, pady=(10, 4))
        f_redeem_row = ctk.CTkFrame(f_redeem, fg_color="transparent")
        f_redeem_row.pack(fill="x", padx=10, pady=4)
        self.entry_redeem = ctk.CTkEntry(f_redeem_row, placeholder_text="输入核销码如 WDxxxxxxxx", width=260, height=32)
        self.entry_redeem.pack(side="left", padx=(0, 8))
        self.btn_redeem = ctk.CTkButton(f_redeem_row, text="绑定核销码", command=self._on_bind_redeem, width=90)
        self.btn_redeem.pack(side="left")
        self.lbl_redeem = ctk.CTkLabel(f_redeem, text="", anchor="w", text_color="gray")
        self.lbl_redeem.pack(fill="x", padx=10, pady=(2, 10))

        # 信息区：标题、时长、分辨率（不显示价格，下载器内不单独收费）
        self.f_info = ctk.CTkFrame(self, fg_color=("gray85", "gray20"), corner_radius=8)
        self.f_info.pack(fill="x", padx=pad, pady=6)
        self.lbl_title = ctk.CTkLabel(self.f_info, text="解析后显示视频标题与时长、分辨率", anchor="w")
        self.lbl_title.pack(fill="x", padx=10, pady=(10, 4))
        self.lbl_meta = ctk.CTkLabel(self.f_info, text="", anchor="w", text_color="gray")
        self.lbl_meta.pack(fill="x", padx=10, pady=(2, 10))

        # 保存路径
        f_path = ctk.CTkFrame(self, fg_color="transparent")
        f_path.pack(fill="x", padx=pad, pady=6)
        ctk.CTkLabel(f_path, text="保存到").pack(anchor="w")
        self.entry_path = ctk.CTkEntry(f_path, height=32)
        self.entry_path.pack(fill="x", pady=(4, 0))
        self.entry_path.insert(0, self.save_dir)
        ctk.CTkButton(f_path, text="选择文件夹", width=90, command=self._choose_dir).pack(
            anchor="w", pady=(6, 0)
        )

        # 进度条
        self.progress = ctk.CTkProgressBar(self)
        self.progress.pack(fill="x", padx=pad, pady=6)
        self.progress.set(0)
        self.lbl_progress = ctk.CTkLabel(self, text="", text_color="gray")
        self.lbl_progress.pack(anchor="w", padx=pad)

        # 日志
        ctk.CTkLabel(self, text="运行日志").pack(anchor="w", padx=pad, pady=(8, 2))
        self.log = ctk.CTkTextbox(self, height=140, font=("Consolas", 11))
        self.log.pack(fill="both", expand=True, padx=pad, pady=(0, pad))

    def _refresh_redeem_ui(self):
        self.redeem_state = load_state()
        if self.redeem_state and self.redeem_state.get("remaining", 0) > 0:
            code = self.redeem_state.get("code", "")[:20]
            if len(self.redeem_state.get("code", "")) > 20:
                code += "..."
            self.lbl_redeem.configure(
                text=f"当前码: {code}  剩余 {self.redeem_state['remaining']} 次",
                text_color=("gray20", "gray90"),
            )
        else:
            self.lbl_redeem.configure(text="请先绑定核销码（由卖家提供）", text_color="gray")
        self._refresh_download_button()

    def _refresh_download_button(self):
        can = bool(self.video_info) and self.redeem_state and self.redeem_state.get("remaining", 0) > 0
        self.btn_download.configure(state="normal" if can else "disabled")

    def _on_bind_redeem(self):
        code = self.entry_redeem.get().strip()
        if not code:
            self._log("请输入核销码")
            return
        ok, times, err = verify_code(code)
        if not ok:
            self._log("绑定失败: " + (err or "核销码无效"))
            return
        save_state(code, times, times)
        self.redeem_state = load_state()
        self._refresh_redeem_ui()
        self._log(f"绑定成功，可下载 {times} 次")

    def _choose_dir(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(initialdir=self.save_dir, title="选择保存目录")
        if path:
            self.save_dir = path
            self.entry_path.delete(0, "end")
            self.entry_path.insert(0, path)

    def _log(self, msg: str):
        self.msg_queue.put(("log", msg))

    def _process_queue(self):
        try:
            while True:
                cmd, arg = self.msg_queue.get_nowait()
                if cmd == "log":
                    self.log.insert("end", arg + "\n")
                    self.log.see("end")
                elif cmd == "progress":
                    pct, text = arg
                    self.progress.set(pct / 100.0)
                    self.lbl_progress.configure(text=text)
                elif cmd == "done":
                    ok, path_or_err = arg
                    self.progress.set(1.0)
                    self.lbl_progress.configure(text="下载完成" if ok else "下载失败")
                    if ok:
                        consume_one()
                        self._refresh_redeem_ui()
                        st = load_state()
                        r = st.get("remaining", 0) if st else 0
                        if r == 0:
                            self._log("已核销 1 次，该码次数已用尽")
                        else:
                            self._log(f"已核销 1 次，剩余 {r} 次")
                        self._log("完成: " + path_or_err)
                    else:
                        self.btn_download.configure(state="normal")
                        self._log("完成: " + path_or_err)
        except queue.Empty:
            pass
        self.after(200, self._process_queue)

    def _on_parse(self):
        url = self.entry_url.get().strip()
        if not url:
            self._log("请先输入视频链接")
            return
        self.video_url = url
        self.btn_parse.configure(state="disabled", text="解析中…")
        self._log(f"正在解析: {url[:50]}...")

        def run():
            info = get_video_info(url)
            self.after(0, lambda: self._apply_parse_result(info))

        threading.Thread(target=run, daemon=True).start()

    def _apply_parse_result(self, info: dict):
        self.btn_parse.configure(state="normal", text="解析视频")
        if not info.get("ok"):
            self.lbl_title.configure(text="解析失败")
            self.lbl_meta.configure(text=info.get("error", "未知错误"))
            self.video_info = None
            self.btn_download.configure(state="disabled")
            self._log("解析失败: " + info.get("error", ""))
            return
        self.video_info = info
        title = info.get("title", "未命名")[:60]
        dur = info.get("duration") or 0
        h = info.get("height") or 0
        self.lbl_title.configure(text=title)
        self.lbl_meta.configure(text=f"{format_duration(dur)} · {h}p")
        self._refresh_download_button()
        self._log("解析成功，可点击「开始下载」")

    def _on_download(self):
        url = self.entry_url.get().strip()
        if not url:
            self._log("请先输入视频链接")
            return
        self.redeem_state = load_state()
        if not self.redeem_state or self.redeem_state.get("remaining", 0) <= 0:
            self._log("请先绑定核销码或该码次数已用尽")
            return
        save_dir = self.entry_path.get().strip() or self.save_dir
        self.save_dir = save_dir
        self.btn_download.configure(state="disabled")
        self.progress.set(0)
        self.lbl_progress.configure(text="准备下载…")
        self._log("开始下载…")

        def run():
            def on_progress(pct, status):
                self.msg_queue.put(("progress", (pct, status)))

            def on_log(msg):
                self.msg_queue.put(("log", msg))

            ok, result = download_video(url, save_dir, progress_callback=on_progress, log_callback=on_log)
            self.msg_queue.put(("done", (ok, result)))

        self.download_thread = threading.Thread(target=run, daemon=True)
        self.download_thread.start()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
