import os
# ── ffmpeg path (comment out jika system ffmpeg sudah tersedia) ──
# FFMPEG_BIN_DIR = "/app/ffmpeg-7.0.2-amd64-static/"
# os.environ["PATH"] = FFMPEG_BIN_DIR + os.pathsep + os.environ.get("PATH", "")

import subprocess
import requests
import uvicorn
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI()

MAX_WORKERS = 4  # Parallel threads untuk download & render


# ── Download helper ──────────────────────────────────────────────────────────
def download_file(url: str, dest_path: str) -> bool:
    if not url:
        return False
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, stream=True, timeout=30, headers=headers)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        with open(dest_path, "rb") as f:
            head = f.read(1024)
        if b"<html" in head.lower() or b"<!doctype" in head.lower():
            print(f"[!] HTML error page: {dest_path}", flush=True)
            os.remove(dest_path)
            return False
        return True
    except Exception as e:
        print(f"[!] Download failed ({url}): {e}", flush=True)
        return False


# ── Audio duration ───────────────────────────────────────────────────────────
def get_audio_duration(audio_path: str, fallback: float = 5.0) -> float:
    if not audio_path or not os.path.exists(audio_path):
        return fallback
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    try:
        result = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
        return float(result)
    except Exception:
        return fallback


# ── Download 1 scene (media + audio) ────────────────────────────────────────
def download_scene(i, media_url, audio_url, work_dir):
    """Download media dan audio untuk 1 scene secara parallel."""
    result = {"i": i, "media": None, "audio": None}

    if media_url:
        local_media = os.path.join(work_dir, f"raw_media_{i}.jpg")
        if download_file(media_url, local_media):
            result["media"] = local_media
        else:
            print(f"[!] Scene {i}: media download failed", flush=True)

    if audio_url:
        local_audio = os.path.join(work_dir, f"raw_audio_{i}.wav")
        if download_file(audio_url, local_audio):
            result["audio"] = local_audio
        else:
            print(f"[!] Scene {i}: audio download failed", flush=True)

    return result


# ── Render 1 clip ────────────────────────────────────────────────────────────
def render_clip(i, local_media, local_audio, media_url, effects, W, H, fps, work_dir):
    """Render 1 clip dari media + audio. Dipanggil parallel."""
    clip_out      = os.path.join(work_dir, f"clip_{i}.mp4")
    is_video      = media_url.lower().split("?")[0].endswith((".mp4", ".mov"))
    clip_duration = get_audio_duration(local_audio) if local_audio else 5.0
    frames        = int(clip_duration * fps)

    # Video filter chain
    vf = (
        f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,"
        f"crop={W*2}:{H*2},format=yuv420p"
    )

    if not is_video:
        if "ken_burns" in effects:
            vf += (
                f",zoompan=z='min(zoom+0.0015,1.15)':d={frames}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}"
            )
        elif "cinematic_pan" in effects:
            vf += (
                f",zoompan=z='1.15':d={frames}"
                f":x='x+1':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}"
            )
        else:
            vf += f",scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        vf += f",scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"

    if "crt_glitch" in effects:
        vf += ",noise=alls=15:allf=t+u,rgbashift=rh=2:bv=2"
    if "vignette" in effects:
        vf += ",vignette=PI/4"

    if clip_duration > 1.6:
        fade_out_start = max(0, clip_duration - 0.8)
        vf += f",fade=t=in:st=0:d=0.8,fade=t=out:st={fade_out_start:.2f}:d=0.8"

    # Gunakan ultrafast saat parallel untuk kurangi CPU contention
    cmd = ["ffmpeg", "-y", "-threads", "4"]
    if is_video:
        cmd.extend(["-stream_loop", "-1", "-t", str(clip_duration), "-i", local_media])
    else:
        cmd.extend(["-loop", "1", "-t", str(clip_duration), "-i", local_media])

    if local_audio:
        cmd.extend(["-i", local_audio])
    else:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"])

    cmd.extend([
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        clip_out,
    ])

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"[OK] Clip {i} selesai", flush=True)
        return {"i": i, "path": clip_out, "audio": local_audio}
    except subprocess.CalledProcessError as e:
        print(f"[!] ffmpeg failed clip {i}:\n{e.stderr[-300:]}", flush=True)
        return {"i": i, "path": None, "audio": None}


# ── Main render job ──────────────────────────────────────────────────────────
def run_render_job(payload: dict) -> dict:
    project_id   = payload.get("project_id", "default_proj")
    media_urls   = payload.get("media", [])
    audio_urls   = payload.get("audios", [])
    effects      = payload.get("effects", ["cinematic_pan"])
    ratio        = payload.get("ratio", "16:9")
    callback_url = payload.get("callback_url")

    if isinstance(effects, str):
        effects = [effects]

    print(f"\n[+] JOB DITERIMA: {project_id} | {len(media_urls)} scenes | parallel={MAX_WORKERS}", flush=True)

    RATIO_MAP = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
    W, H = RATIO_MAP.get(ratio, (1920, 1080))

    work_dir = f"/tmp/storyforge_{project_id}"
    os.makedirs(work_dir, exist_ok=True)

    output_filename   = "final_render.mp4"
    final_output_path = os.path.join(work_dir, output_filename)
    fps = 30

    try:
        # ── PHASE 1: PARALLEL DOWNLOAD ───────────────────────────────────────
        print(f"[~] Phase 1: Downloading {len(media_urls)} scenes parallel...", flush=True)
        t0 = time.time()
        scene_data = {}  # i -> {media, audio}

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            for i, media_url in enumerate(media_urls):
                audio_url = audio_urls[i] if i < len(audio_urls) else None
                f = executor.submit(download_scene, i, media_url, audio_url, work_dir)
                futures[f] = i

            for f in as_completed(futures):
                result = f.result()
                scene_data[result["i"]] = result

        print(f"[OK] Download selesai dalam {time.time()-t0:.1f}s", flush=True)

        # ── PHASE 2: PARALLEL RENDER ─────────────────────────────────────────
        print(f"[~] Phase 2: Rendering clips parallel...", flush=True)
        t1 = time.time()
        clip_results = {}  # i -> {path, audio}

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            for i, media_url in enumerate(media_urls):
                sd = scene_data.get(i, {})
                local_media = sd.get("media")
                local_audio = sd.get("audio")

                if not local_media:
                    print(f"[!] Skipping scene {i}: no media", flush=True)
                    continue

                f = executor.submit(
                    render_clip,
                    i, local_media, local_audio, media_url,
                    effects, W, H, fps, work_dir
                )
                futures[f] = i

            for f in as_completed(futures):
                result = f.result()
                clip_results[result["i"]] = result

        print(f"[OK] Render selesai dalam {time.time()-t1:.1f}s", flush=True)

        # ── PHASE 3: CONCAT (sequential, urutan harus benar) ─────────────────
        # Sort by index untuk jaga urutan scene
        sorted_clips = [
            clip_results[i]["path"]
            for i in sorted(clip_results.keys())
            if clip_results[i]["path"]
        ]
        local_audio_paths = [
            clip_results[i]["audio"]
            for i in sorted(clip_results.keys())
            if clip_results[i].get("audio")
        ]

        if not sorted_clips:
            return {"status": "error", "message": "No clips were rendered successfully"}

        print(f"[~] Phase 3: Concatenating {len(sorted_clips)} clips...", flush=True)
        list_file_path = os.path.join(work_dir, "concat_list.txt")
        with open(list_file_path, "w") as f:
            for clip in sorted_clips:
                f.write(f"file '{clip}'\n")

        concat_cmd = [
            "ffmpeg", "-y", "-threads", "28",
            "-f", "concat", "-safe", "0", "-i", list_file_path,
            "-c:v", "copy", "-c:a", "aac",
            final_output_path,
        ]
        subprocess.run(concat_cmd, check=True, capture_output=True, text=True)
        print(f"[OK] Total waktu: {time.time()-t0:.1f}s", flush=True)

        # ── PHASE 4: WEBHOOK CALLBACK ────────────────────────────────────────
        total_duration   = sum(get_audio_duration(p) for p in local_audio_paths)
        video_public_url = f"http://100.126.68.60:7810/download/{project_id}"

        upload_res: dict | None = None
        if callback_url and os.path.exists(final_output_path):
            print("[~] Notifying webhook...", flush=True)
            webhook_headers = {
                "X-Webhook-Token": os.environ.get("WEBHOOK_SECRET", "sf_webhook_fork_me"),
                "User-Agent": "Mozilla/5.0 StoryForge/2.0"
            }
            for attempt in range(1, 7):
                try:
                    r = requests.post(
                        callback_url,
                        json={
                            "status":     "success",
                            "project_id": project_id,
                            "video_url":  video_public_url,
                            "duration":   total_duration,
                        },
                        headers=webhook_headers,
                        timeout=300,
                    )
                    r.raise_for_status()
                    upload_res = r.json()
                    print(f"[OK] Webhook OK: {upload_res}", flush=True)
                    break
                except requests.exceptions.ReadTimeout:
                    print("[OK] Webhook triggered, PHP downloading in background.", flush=True)
                    break
                except Exception as e:
                    print(f"[!] Webhook attempt {attempt}/6: {e}", flush=True)
                    if attempt < 6:
                        time.sleep(5)
                    else:
                        print("[!] All webhook retries failed.", flush=True)

        video_url = (
            upload_res.get("path")
            if upload_res and isinstance(upload_res, dict) and "path" in upload_res
            else f"project_data/{project_id}/{output_filename}"
        )

        print("[OK] Job Selesai.", flush=True)
        return {
            "status":        "success",
            "video_url":     video_url,
            "duration":      total_duration,
            "clips_rendered": len(sorted_clips),
        }

    except subprocess.CalledProcessError as e:
        err_msg = e.stderr if e.stderr else str(e)
        print(f"[!] Fatal ffmpeg error:\n{err_msg}", flush=True)
        return {"status": "error", "message": f"ffmpeg error: {err_msg[-500:]}"}

    except Exception as e:
        print(f"[!] Unexpected error: {e}", flush=True)
        return {"status": "error", "message": str(e)}


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/download/{project_id}")
async def download_video(project_id: str):
    path = f"/tmp/storyforge_{project_id}/final_render.mp4"
    if not os.path.exists(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename="final_render.mp4")


@app.post("/assemble_media")
async def assemble_media(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    if payload.get("callback_url"):
        background_tasks.add_task(run_render_job, payload)
        return JSONResponse(
            {"status": "accepted", "project_id": payload.get("project_id", "default_proj")},
            status_code=202,
        )
    result = run_render_job(payload)
    status_code = 200 if result.get("status") == "success" else 500
    return JSONResponse(result, status_code=status_code)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7810)
