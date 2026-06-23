import os

# Konfigurasi Path FFmpeg (Wajib sebelum impor pustaka lain yang menggunakan ffmpeg)
FFMPEG_BIN_DIR = "/home/stable-diffusion-webui/ffmpeg-bin/ffmpeg-7.0.2-amd64-static/"
os.environ["PATH"] = FFMPEG_BIN_DIR + os.pathsep + os.environ.get("PATH", "")

import subprocess
import shutil
import zipfile
import httpx
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
import uvicorn
app = FastAPI(title="Sharx Dedicated HLS Parser")

# Ruang kerja spesifik di dalam direktori saat ini
WORKSPACE = "/home/stable-diffusion-webui/sharx_workspace"
os.makedirs(WORKSPACE, exist_ok=True)

def process_and_forward(input_file_path: str, folder_name: str, target_api_url: str):
    output_dir = os.path.join(WORKSPACE, folder_name)
    os.makedirs(output_dir, exist_ok=True)
    
    playlist_file = os.path.join(output_dir, "index.m3u8")
    segment_pattern = os.path.join(output_dir, "segment_%03d.ts")

    # Konfigurasi standar YouTube: H.264 High Profile, Closed GOP 2 Detik, Segmen 2 Detik
    command = [
        "ffmpeg", "-y", "-i", input_file_path,
        "-threads", "24",             # Manfaatkan 24 dari 28 thread CPU server
        "-c:v", "libx264", 
        "-preset", "fast",            # Keseimbangan kecepatan dan kompresi
        "-profile:v", "high",
        "-r", "30",                   # Paksa 30 FPS untuk konsistensi GOP
        "-g", "60",                   # Keyframe setiap 60 frame (2 detik)
        "-keyint_min", "60",          # Kunci interval GOP
        "-sc_threshold", "0",         # Matikan deteksi pergantian scene
        "-c:a", "aac", "-b:a", "128k",
        "-f", "hls", 
        "-hls_time", "10",             # Potong segmen TS setiap 2 detik
        "-hls_list_size", "0",
        "-hls_segment_filename", segment_pattern, 
        playlist_file
    ]
    
    print(f"[+] Memulai render HLS GOP 2 detik untuk: {folder_name}", flush=True)
    result = subprocess.run(command, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"[-] FFmpeg Error: {result.stderr}", flush=True)
        return

    print("[+] Membungkus segmen ke ZIP", flush=True)
    zip_path = os.path.join(WORKSPACE, f"{folder_name}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(output_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, output_dir)
                zipf.write(file_path, arcname)

    print(f"[+] Mengirim ZIP ke {target_api_url}", flush=True)
    try:
        with open(zip_path, "rb") as f:
            files = {"file": (f"{folder_name}.zip", f, "application/zip")}
            data = {"folder_name": folder_name}
            # Transfer IP Tailscale ke tujuan
            response = httpx.post(target_api_url, files=files, data=data, timeout=600.0)
            print(f"[+] Respons tujuan: {response.status_code} - {response.text}", flush=True)
    except Exception as e:
        print(f"[-] Gagal mengirim ZIP: {str(e)}", flush=True)

    # Pembersihan ruang kerja otomatis
    if os.path.exists(input_file_path):
        os.remove(input_file_path)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    print(f"[+] Pembersihan {folder_name} selesai.", flush=True)


@app.post("/parse-and-forward")
async def parse_and_forward(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    folder_name: str = Form(...),
    target_api_url: str = Form(...)
):
    input_file_path = os.path.join(WORKSPACE, file.filename)
    
    # Simpan file biner dari PHP Bridge (via IP Tailscale) ke disk lokal
    with open(input_file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    background_tasks.add_task(process_and_forward, input_file_path, folder_name, target_api_url)
    
    return {
        "status": "processing_started",
        "file": file.filename,
        "folder": folder_name,
        "message": "File diterima peladen render. FFmpeg berjalan di latar belakang."
    }

if __name__ == "__main__":
    # Binding ke 0.0.0.0 agar bisa diakses via IP Tailscale pada port 4784
    uvicorn.run(app, host="0.0.0.0", port=4784)
