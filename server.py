#!/usr/bin/env python3
"""格式工坊 · PDF / Word 转换服务（云托管版，无需域名）

小程序通过 wx.cloud.callContainer 走微信私有协议调用，不经过公网域名：
1. POST /api/jobs            创建任务（文件名、大小、目标格式）
2. POST /api/jobs/<id>/chunk 分片上传原始文件（offset 从 0 递增）
3. POST /api/jobs/<id>/start 开始转换（后台线程）
4. GET  /api/jobs/<id>       轮询状态
5. GET  /api/jobs/<id>/file  分片下载结果（每次不超过 800KB）

保留 /convert 与 /files/<token>/<name> 仅用于本机 curl 调试，不再供小程序使用。
所有转换结果默认 30 分钟后自动删除。
"""
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(BASE, "files")
os.makedirs(FILES, exist_ok=True)

MAX_SIZE = int(os.environ.get("MAX_SIZE_MB", "20")) * 1024 * 1024
TTL = int(os.environ.get("FILE_TTL_MINUTES", "30")) * 60
SOFFICE = os.environ.get("SOFFICE", "soffice")

MAX_UPLOAD_CHUNK = 2 * 1024 * 1024
MAX_DOWNLOAD_CHUNK = 800 * 1024

ALLOWED = {
    "docx": {".pdf"},
    "pdf": {".doc", ".docx"},
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_SIZE

JOBS = {}
JOBS_LOCK = threading.Lock()
CONVERT_LOCK = threading.Lock()


def cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        with JOBS_LOCK:
            jobs = list(JOBS.values())
            known = set(JOBS.keys())

        for job in jobs:
            try:
                if job["status"] == "uploading":
                    stale = now - job["created"] > TTL
                elif job["status"] in ("queued", "converting"):
                    stale = False
                else:
                    stale = now - os.path.getmtime(job["dir"]) > TTL
                if stale:
                    shutil.rmtree(job["dir"], ignore_errors=True)
                    with JOBS_LOCK:
                        JOBS.pop(job["id"], None)
            except OSError:
                pass

        # 清理进程重启后遗留的目录，以及 /convert 调试产生的临时目录
        try:
            names = os.listdir(FILES)
        except OSError:
            names = []
        for name in names:
            path = os.path.join(FILES, name)
            if name in known or not os.path.isdir(path):
                continue
            try:
                if now - os.path.getmtime(path) > TTL:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


threading.Thread(target=cleanup_loop, daemon=True).start()


def safe_stem(name):
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = re.sub(r"[^\w一-鿿-]+", "_", stem).strip("_")
    return (stem or "converted")[:60]


def valid_job_id(job_id):
    return bool(re.fullmatch(r"[a-f0-9]{32}", job_id or ""))


def get_job(job_id):
    if not valid_job_id(job_id):
        abort(404)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return job


def run_conversion(src, target, out_path):
    """执行一次转换，失败时抛异常。调用方负责加 CONVERT_LOCK。"""
    work = os.path.dirname(out_path)

    if target == "docx":
        from pdf2docx import Converter

        cv = Converter(src)
        try:
            cv.convert(out_path)
        finally:
            cv.close()
    else:
        proc = subprocess.run(
            [
                SOFFICE,
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                work,
                src,
            ],
            capture_output=True,
            timeout=110,
        )
        produced = os.path.join(work, "source.pdf")
        if proc.returncode != 0 or not os.path.exists(produced):
            detail = (proc.stderr or b"").decode("utf-8", "ignore").strip()
            raise RuntimeError(detail or "LibreOffice 转换失败")
        os.replace(produced, out_path)

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("转换结果为空，请确认文件未加密且内容完整")


def process_job(job_id):
    try:
        with CONVERT_LOCK:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job:
                    return
                job["status"] = "converting"
            run_conversion(job["src"], job["target"], job["out"])
            result_size = os.path.getsize(job["out"])
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            job["result_size"] = result_size
            job["status"] = "done"
    except subprocess.TimeoutExpired:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["error"] = "转换超时，请尝试较小的文件"
                job["status"] = "error"
    except Exception as exc:  # noqa: BLE001
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["error"] = f"转换失败：{exc}"
                job["status"] = "error"
    finally:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job:
            try:
                os.utime(job["dir"], None)
            except OSError:
                pass


def read_json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.post("/api/jobs")
def create_job():
    data = read_json_body()
    target = str(data.get("target") or "").lower().strip()
    name = str(data.get("name") or "")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0

    if target not in ALLOWED:
        return jsonify(ok=False, error="目标格式不受支持"), 400
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED[target]:
        return jsonify(ok=False, error="文件类型与目标格式不匹配"), 400
    if size <= 0:
        return jsonify(ok=False, error="文件内容为空"), 400
    if size > MAX_SIZE:
        limit_mb = max(1, MAX_SIZE // (1024 * 1024))
        return jsonify(ok=False, error=f"文件过大，最大支持 {limit_mb}MB"), 413

    job_id = uuid.uuid4().hex
    work = os.path.join(FILES, job_id)
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "source" + ext)
    open(src, "wb").close()

    out_name = f"{safe_stem(name)}.{target}"
    out_path = os.path.join(work, out_name)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "dir": work,
            "src": src,
            "out": out_path,
            "out_name": out_name,
            "name": name,
            "target": target,
            "size": size,
            "uploaded": 0,
            "status": "uploading",
            "result_size": 0,
            "error": "",
            "created": time.time(),
        }

    return jsonify(ok=True, jobId=job_id, chunkSize=1024 * 1024)


@app.post("/api/jobs/<job_id>/chunk")
def upload_chunk(job_id):
    job = get_job(job_id)
    data = request.get_data(cache=False)
    if not data:
        return jsonify(ok=False, error="分片内容为空"), 400
    if len(data) > MAX_UPLOAD_CHUNK:
        return jsonify(ok=False, error="单个分片过大"), 413

    try:
        offset = int(request.args.get("offset", "-1"))
    except (TypeError, ValueError):
        offset = -1

    with JOBS_LOCK:
        if job["status"] != "uploading":
            return jsonify(ok=False, error="任务状态不允许上传"), 409
        if offset != job["uploaded"]:
            return jsonify(ok=False, error="分片偏移不连续", expected=job["uploaded"]), 409
        if job["uploaded"] + len(data) > job["size"]:
            return jsonify(ok=False, error="上传内容超过声明的文件大小"), 400
        with open(job["src"], "ab") as fh:
            fh.write(data)
        job["uploaded"] += len(data)
        uploaded = job["uploaded"]

    return jsonify(ok=True, uploaded=uploaded)


@app.post("/api/jobs/<job_id>/start")
def start_job(job_id):
    job = get_job(job_id)
    with JOBS_LOCK:
        if job["status"] in ("queued", "converting"):
            return jsonify(ok=True, status=job["status"])
        if job["status"] != "uploading":
            return jsonify(ok=False, error="任务状态不允许开始转换"), 409
        if job["uploaded"] != job["size"]:
            return jsonify(ok=False, error="文件尚未上传完整"), 400
        job["status"] = "queued"

    threading.Thread(target=process_job, args=(job_id,), daemon=True).start()
    return jsonify(ok=True, status="queued")


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = get_job(job_id)
    with JOBS_LOCK:
        payload = {
            "ok": True,
            "status": job["status"],
            "uploaded": job["uploaded"],
            "size": job["size"],
            "resultSize": job["result_size"],
            "error": job["error"],
            "name": job["out_name"],
        }
    return jsonify(payload)


@app.get("/api/jobs/<job_id>/file")
def job_file(job_id):
    job = get_job(job_id)
    try:
        offset = int(request.args.get("offset", "0"))
        size = int(request.args.get("size", str(512 * 1024)))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="分片参数无效"), 400

    if offset < 0 or size <= 0 or size > MAX_DOWNLOAD_CHUNK:
        return jsonify(ok=False, error="分片参数无效"), 400

    with JOBS_LOCK:
        if job["status"] != "done":
            return jsonify(ok=False, error="转换尚未完成"), 409
        total = job["result_size"]
        out_path = job["out"]

    with open(out_path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(size)

    if not data and offset < total:
        return jsonify(ok=False, error="读取结果失败"), 500

    return Response(
        data,
        mimetype="application/octet-stream",
        headers={
            "X-Total-Size": str(total),
            "X-File-Offset": str(offset),
            "Cache-Control": "no-store",
        },
    )


# ---------- 以下接口仅用于本机 curl 调试，小程序不使用 ----------

@app.post("/convert")
def convert_once():
    upload = request.files.get("file")
    target = (request.form.get("target") or "").strip().lower()
    if upload is None or target not in ALLOWED:
        return jsonify(ok=False, error="缺少文件，或目标格式不受支持"), 400

    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext not in ALLOWED[target]:
        return jsonify(ok=False, error="文件类型与目标格式不匹配"), 400

    token = uuid.uuid4().hex
    work = os.path.join(FILES, token)
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "source" + ext)
    upload.save(src)
    out_name = f"{safe_stem(upload.filename)}.{target}"
    out_path = os.path.join(work, out_name)

    try:
        with CONVERT_LOCK:
            run_conversion(src, target, out_path)
        url = f"{request.url_root.rstrip('/')}/files/{token}/{quote(out_name)}"
        return jsonify(ok=True, url=url, name=out_name, size=os.path.getsize(out_path))
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        return jsonify(ok=False, error="转换超时，请尝试较小的文件"), 504
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(work, ignore_errors=True)
        app.logger.exception("convert failed")
        return jsonify(ok=False, error=f"转换失败：{exc}"), 500


@app.get("/files/<token>/<path:name>")
def serve(token, name):
    if not valid_job_id(token):
        abort(404)
    work = os.path.join(FILES, token)
    if not os.path.isdir(work):
        abort(404)
    return send_from_directory(work, name, as_attachment=True)


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
