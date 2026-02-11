import os
import uuid
import re
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

progress_store: dict[str, float] = {}
file_store: dict[str, str] = {}


class DownloadRequest(BaseModel):
    url: str
    type: str        # video | audio
    quality: str     # best | 1080 | 720 | 192 | 128


# ---------- helpers ----------

def safe_filename(name: str) -> str:
    """Remove characters that break Android / filesystems"""
    return re.sub(r'[\\/:*?"<>|]', '_', name)


def progress_hook(task_id: str):
    def hook(d):
        if d["status"] == "downloading":
            percent_str = d.get("_percent_str", "0%").replace("%", "").strip()
            try:
                progress_store[task_id] = float(percent_str)
            except:
                progress_store[task_id] = progress_store.get(task_id, 0.0)

        elif d["status"] == "finished":
            # finished downloading streams, merge still running
            progress_store[task_id] = 99.0
    return hook


# ---------- core download ----------

def start_download(task_id: str, req: DownloadRequest):
    try:
        ydl_opts = {
            "outtmpl": f"{DOWNLOAD_DIR}/{task_id}_%(title)s.%(ext)s",

            "noplaylist": True,
            "concurrent_fragment_downloads": 1,
            "retries": 5,
            "fragment_retries": 5,

            "progress_hooks": [progress_hook(task_id)],

            # 🔒 Force Android-friendly output
            "merge_output_format": "mp4",
            "format_sort": ["res", "fps", "codec:h264"],

            # 🔧 Fix A/V sync + streaming
            "postprocessor_args": [
                "-map_metadata", "0",
                "-movflags", "+faststart",
            ],
        }

        if req.type == "video":
            if req.quality == "1080":
                ydl_opts["format"] = "bestvideo[height<=1080]+bestaudio/best"
            elif req.quality == "720":
                ydl_opts["format"] = "bestvideo[height<=720]+bestaudio/best"
            else:
                ydl_opts["format"] = "bestvideo+bestaudio/best"
        else:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": req.quality,
            }]

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)

            base_path = ydl.prepare_filename(info)
            base_path = os.path.splitext(base_path)[0]

            if req.type == "video":
                final_path = base_path + ".mp4"
            else:
                final_path = base_path + ".mp3"


            file_store[task_id] = final_path
            progress_store[task_id] = 100.0

    except Exception as e:
        progress_store[task_id] = -1
        file_store.pop(task_id, None)
        raise e


# ---------- API ----------

@app.post("/download")
def download(req: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    progress_store[task_id] = 0.0
    background_tasks.add_task(start_download, task_id, req)
    return {"task_id": task_id}


@app.get("/progress/{task_id}")
def progress(task_id: str):
    return {"progress": progress_store.get(task_id, 0.0)}


@app.get("/download-file/{task_id}")
def download_file(task_id: str, background_tasks: BackgroundTasks):
    path = file_store.get(task_id)

    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not available")

    background_tasks.add_task(os.remove, path)
    file_store.pop(task_id, None)
    progress_store.pop(task_id, None)

    return FileResponse(
        path=path,
        filename=os.path.basename(path),
        media_type="application/octet-stream",
    )