"""Checks the encoded film and writes verification.json plus a contact sheet of one frame per chapter."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
movie = HERE / "carepath-one-rule-five-files.mp4"
ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
data = json.loads((HERE / "timeline.json").read_text(encoding="utf-8"))
probe = json.loads(subprocess.check_output([ffprobe, "-v", "error", "-show_streams", "-show_format", "-show_chapters", "-of", "json", str(movie)], text=True))
video = next(s for s in probe["streams"] if s["codec_type"] == "video")
audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
duration = float(probe["format"]["duration"])
assert abs(duration - data["total"]) < 0.2, (duration, data["total"])
assert (video["width"], video["height"]) == (1920, 1080)
assert video["codec_name"] == "h264" and audio["codec_name"] == "aac"
assert video["avg_frame_rate"] == "24/1"
assert len(probe["chapters"]) == len(data["scenes"])
assert any(s["codec_type"] == "subtitle" for s in probe["streams"])
decode = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(movie), "-f", "null", "-"], stderr=subprocess.PIPE, text=True)
assert decode.returncode == 0 and not decode.stderr.strip(), decode.stderr
volume = subprocess.run([ffmpeg, "-nostdin", "-i", str(movie), "-af", "volumedetect", "-vn", "-f", "null", "-"], stderr=subprocess.PIPE, text=True).stderr
mean = float(volume.split("mean_volume:")[1].split("dB")[0])
assert mean > -35, mean

sheet = Image.new("RGB", (1920, 1080 // 2 * 5), "black")
frames = HERE / "frames"
frames.mkdir(exist_ok=True)
for i, scene in enumerate(data["scenes"]):
    at = scene["start"] + scene["duration"] * 0.8
    out = frames / f"chapter-{i + 1:02d}.png"
    subprocess.run([ffmpeg, "-nostdin", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(movie), "-frames:v", "1", str(out)], check=True)
    sheet.paste(Image.open(out).resize((960, 540)), ((i % 2) * 960, (i // 2) * 540))
sheet.save(HERE / "contact-sheet.png")

record = {
    "file": movie.name, "duration_seconds": round(duration, 2), "resolution": [1920, 1080], "frame_rate": 24,
    "chapters": len(probe["chapters"]), "video_codec": "H.264", "audio_codec": "AAC", "mean_volume_db": mean,
    "voice": "Microsoft Heera (en-IN), engine speaking rate 1.2, no time-stretch",
    "captions": "Burned in, by clause, plus a selectable SRT track",
    "full_file_decode": "passed", "sha256": hashlib.sha256(movie.read_bytes()).hexdigest(), "bytes": movie.stat().st_size,
    "scope": ("Real CarePath files, scores and dates; quality scores from the platform's own rubric; benchmark figure "
              "from the scorecard baseline. AI proposals, theme names, index terms, vector values and the answer "
              "wording are illustrative and footnoted on screen. Not a live screen recording."),
}
(HERE / "verification.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps(record, indent=2))
