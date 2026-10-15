"""Spoken text, then one timeline from the recorded sentences.

`python timeline.py spoken`   writes spoken.json for narrate.ps1
`python timeline.py assemble` measures each recorded sentence and writes narration.wav,
                              timeline.json, captions.srt and chapters.ffmeta.

Nothing is sped up: each scene lasts as long as its own narration plus pauses.
"""
import json
import re
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENES = json.loads((HERE / "storyboard.json").read_text(encoding="utf-8"))

# Written for the eye on screen, said for the ear.
SAY = [
    (r"\bAI\b", "A.I."),
    (r"\bBM25\b", "B M 25"),
    (r"\bTBD\b", "T B D"),
    (r"\bOCR\b", "O C R"),
    (r"\bMarkItDown\b", "Mark It Down"),
    (r"\bLeiden\b", "Lyden"),
    (r"\bAPI\b", "A P I"),
    (r"\bIT\b", "I T"),
]
LEAD, GAP, LONG_GAP, TAIL = 0.5, 0.28, 0.45, 0.8
RATE = 16000


def spoken():
    lines = []
    for s, scene in enumerate(SCENES):
        for k, sentence in enumerate(scene["sentences"]):
            say = sentence
            for pattern, replacement in SAY:
                say = re.sub(pattern, replacement, say)
            lines.append({"file": f"s{s:02d}-{k:02d}.wav", "say": say})
    (HERE / "spoken.json").write_text(json.dumps(lines, indent=1), encoding="utf-8")
    print(f"{len(lines)} sentences")


def clock(seconds, sep=","):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def pieces(sentence, start, end, limit=88):
    """Captions by clause: split long sentences at punctuation, timed by length."""
    parts = [sentence]
    while any(len(p) > limit for p in parts):
        out = []
        for p in parts:
            if len(p) <= limit:
                out.append(p)
                continue
            cuts = [m.end() for m in re.finditer(r"[,:;?] ", p)]
            if not cuts:
                words = p.split()
                half = len(words) // 2
                out += [" ".join(words[:half]), " ".join(words[half:])]
                continue
            cut = min(cuts, key=lambda c: abs(c - len(p) / 2))
            out += [p[:cut].strip(), p[cut:].strip()]
        parts = out
    total = sum(len(p) for p in parts)
    t, result = start, []
    for p in parts:
        length = (end - start) * len(p) / total
        result.append((t, t + length, p))
        t += length
    return result


def assemble():
    pcm, t = bytearray(), 0.0
    timeline, captions = [], []

    def silence(seconds):
        nonlocal t
        pcm.extend(b"\x00\x00" * round(seconds * RATE))
        t += seconds

    for s, scene in enumerate(SCENES):
        scene_start = t
        silence(LEAD)
        cues = []
        for k, sentence in enumerate(scene["sentences"]):
            with wave.open(str(HERE / "speech" / f"s{s:02d}-{k:02d}.wav")) as w:
                assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE)
                frames = w.readframes(w.getnframes())
            start = t
            pcm.extend(frames)
            t += len(frames) / 2 / RATE
            cues.append({"start": round(start - scene_start, 3), "end": round(t - scene_start, 3), "text": sentence})
            captions += pieces(sentence, start, t)
            if k < len(scene["sentences"]) - 1:
                silence(LONG_GAP if sentence.endswith(("?", ".")) and len(sentence) > 60 else GAP)
        silence(TAIL)
        # Whole frames at 24 fps keep scene boundaries exact.
        frames24 = round((t - scene_start) * 24)
        silence(frames24 / 24 - (t - scene_start))
        timeline.append({**{k: scene[k] for k in ("id", "title", "tech", "footnote")},
                         "start": round(scene_start, 4), "duration": round(frames24 / 24, 4),
                         "cues": cues})

    with wave.open(str(HERE / "narration.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(pcm))
    total = sum(scene["duration"] for scene in timeline)
    (HERE / "timeline.json").write_text(json.dumps({"total": round(total, 4), "scenes": timeline,
        "captions": [{"a": round(x, 3), "b": round(y, 3), "text": c} for x, y, c in captions]}, indent=1), encoding="utf-8")
    (HERE / "captions.srt").write_text(
        "\n".join(f"{n}\n{clock(a)} --> {clock(b)}\n{text}\n" for n, (a, b, text) in enumerate(captions, 1)),
        encoding="utf-8")
    meta = [";FFMETADATA1", "title=Digital Brain - one rule, five files, one story",
            "comment=Indian English narration (Microsoft Heera, en-IN), natural speed."]
    for scene in timeline:
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(scene['start'] * 1000)}",
                 f"END={round((scene['start'] + scene['duration']) * 1000)}", f"title={scene['title']}"]
    (HERE / "chapters.ffmeta").write_text("\n".join(meta), encoding="utf-8")
    for scene in timeline:
        print(f"{scene['start']:7.2f}  {scene['duration']:6.2f}s  {scene['title']}")
    print(f"total {total:.2f}s ({total / 60:.2f} min), {len(captions)} captions")


if __name__ == "__main__":
    {"spoken": spoken, "assemble": assemble}[sys.argv[1]]()
