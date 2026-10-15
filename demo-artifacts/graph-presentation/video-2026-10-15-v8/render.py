"""Capture scene.html frame by frame in headless Chrome and encode the film.

python render.py preview 12 60 ...   still frames at those seconds -> preview/
python render.py film                 full 24 fps render in parallel segments, then mux
Run with the repository's .venv (Playwright is a dev dependency).
"""
import json
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FPS, WORKERS = 24, 4
FFMPEG = shutil.which("ffmpeg")
OUT = HERE / "carepath-one-rule-five-files.mp4"


def timeline():
    data = json.loads((HERE / "timeline.json").read_text(encoding="utf-8"))
    (HERE / "timeline.js").write_text(
        "window.TIMELINE=" + json.dumps({"total": data["total"], "scenes": data["scenes"]}) + ";\n"
        "window.CAPTIONS=" + json.dumps(data["captions"]) + ";\n", encoding="utf-8")
    return data


def open_page(p):
    browser = p.chromium.launch(channel="chrome", args=["--force-color-profile=srgb", "--hide-scrollbars"])
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto((HERE / "scene.html").as_uri())
    page.evaluate("window.READY")
    if errors:
        raise RuntimeError(errors)
    return browser, page


def preview(seconds):
    timeline()
    (HERE / "preview").mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser, page = open_page(p)
        for s in seconds:
            page.evaluate(f"seek({float(s)})")
            page.screenshot(path=str(HERE / "preview" / f"t{float(s):07.2f}.png"))
        browser.close()


def segment(args):
    first, last, path = args
    cmd = [FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS), path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    with sync_playwright() as p:
        browser, page = open_page(p)
        for f in range(first, last):
            page.evaluate(f"seek({f / FPS})")
            proc.stdin.write(page.screenshot(type="jpeg", quality=95))
        browser.close()
    proc.stdin.close()
    assert proc.wait() == 0, path
    return path


def film():
    data = timeline()
    frames = round(data["total"] * FPS)
    size = -(-frames // WORKERS)
    jobs = [(k * size, min(frames, (k + 1) * size), str(HERE / f"segment-{k}.mp4")) for k in range(WORKERS)]
    with ProcessPoolExecutor(WORKERS) as pool:
        for path in pool.map(segment, jobs):
            print("done", Path(path).name, flush=True)
    (HERE / "segments.txt").write_text("\n".join(f"file '{Path(j[2]).name}'" for j in jobs), encoding="utf-8")
    subprocess.run([FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "segments.txt", "-c", "copy", "video-only.mp4"], cwd=HERE, check=True)
    subprocess.run([FFMPEG, "-nostdin", "-y", "-v", "error", "-i", "video-only.mp4", "-i", "narration.wav", "-i", "captions.srt", "-i", "chapters.ffmeta",
                    "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map_metadata", "3", "-map_chapters", "3",
                    "-c:v", "copy", "-video_track_timescale", str(FPS), "-af", "aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
                    "-c:s", "mov_text", "-disposition:s:0", "0", "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", OUT.name],
                   cwd=HERE, check=True)
    for j in jobs:
        Path(j[2]).unlink()
    (HERE / "video-only.mp4").unlink()
    print("wrote", OUT.name, flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "preview":
        preview(sys.argv[2:])
    else:
        film()
