import os
import subprocess
import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

def download_file(url, dest_path):
    if not url: return False
    try:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"Download failed: {e}", flush=True)
        return False

def get_audio_duration(audio_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    try:
        return float(subprocess.check_output(cmd, text=True).strip())
    except:
        return 5.0

@app.post("/assemble_media")
async def assemble_media(request: Request):
    payload = await request.json()
    project_id = payload.get("project_id", "default_proj")
    media_urls = payload.get("media", [])
    audio_urls = payload.get("audios", [])
    effects = payload.get("effects", ["cinematic_pan"])
    ratio = payload.get("ratio", "16:9")
    callback_url = payload.get("callback_url") 

    print(f"\n[+] JOB DITERIMA: {project_id}", flush=True)

    RATIO_MAP = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
    W, H = RATIO_MAP.get(ratio, (1920, 1080))

    work_dir = f"/tmp/storyforge_{project_id}"
    os.makedirs(work_dir, exist_ok=True)
    output_filename = "final_render.mp4"
    final_output_path = os.path.join(work_dir, output_filename)

    if isinstance(effects, str): effects = [effects]
    processed_clips = []
    fps = 30
    
    try:
        for i, media_url in enumerate(media_urls):
            local_media = os.path.join(work_dir, f"raw_media_{i}.jpg")
            if not download_file(media_url, local_media): continue
            
            local_audio = None
            scene_audio_url = audio_urls[i] if i < len(audio_urls) else None
            if scene_audio_url:
                local_audio = os.path.join(work_dir, f"raw_audio_{i}.wav")
                if not download_file(scene_audio_url, local_audio):
                    local_audio = None

            clip_out = os.path.join(work_dir, f"clip_{i}.mp4")
            is_video = media_url.lower().split("?")[0].endswith((".mp4", ".mov"))
            clip_duration = get_audio_duration(local_audio) if local_audio else 5.0
            frames = int(clip_duration * fps)

            current_vf = f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},format=yuv420p"

            if not is_video:
                if "ken_burns" in effects:
                    current_vf += f",zoompan=z='min(zoom+0.0015,1.15)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}"
                elif "cinematic_pan" in effects:
                    current_vf += f",zoompan=z='1.15':d={frames}:x='x+1':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}"
                else:
                    current_vf += f",scale={W}:{H}:force_original_aspect_ratio=crop,crop={W}:{H}"
            else:
                current_vf += f",scale={W}:{H}:force_original_aspect_ratio=crop,crop={W}:{H}"

            if "crt_glitch" in effects: current_vf += ",noise=alls=15:allf=t+u,rgbashift=rh=2:bv=2"
            if "vignette" in effects: current_vf += ",vignette=PI/4"

            if clip_duration > 1.6:
                fade_out_start = max(0, clip_duration - 0.8)
                current_vf += f",fade=t=in:st=0:d=0.8,fade=t=out:st={fade_out_start:.2f}:d=0.8"

            cmd = ["ffmpeg", "-y", "-threads", "28"]
            if is_video:
                cmd.extend(["-stream_loop", "-1", "-t", str(clip_duration), "-i", local_media])
            else:
                cmd.extend(["-loop", "1", "-t", str(clip_duration), "-i", local_media])

            if local_audio:
                cmd.extend(["-i", local_audio])
            else:
                cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"])

            cmd.extend([
                "-vf", current_vf, 
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k", "-shortest", clip_out
            ])
            
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            processed_clips.append(clip_out)

        list_file_path = os.path.join(work_dir, "concat_list.txt")
        with open(list_file_path, "w") as f:
            for clip in processed_clips: f.write(f"file '{clip}'\n")

        concat_cmd = ["ffmpeg", "-y", "-threads", "28", "-f", "concat", "-safe", "0", "-i", list_file_path, "-c:v", "copy", "-c:a", "aac", final_output_path]
        subprocess.run(concat_cmd, check=True, capture_output=True, text=True)

        print("[!] Push video ke main server via webhook...", flush=True)
        upload_res = None
        if callback_url and os.path.exists(final_output_path):
            with open(final_output_path, "rb") as f:
                r = requests.post(callback_url, data={"project_id": project_id}, files={"video": f})
                upload_res = r.json()

        print("[OK] Job Selesai.", flush=True)
        return JSONResponse({
            "status": "success", 
            "video_url": upload_res.get("path") if upload_res else f"project_data/{project_id}/{output_filename}", 
            "duration": sum([get_audio_duration(a) for a in audio_urls if os.path.exists(a)]) if audio_urls else 0
        })

    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8596)
