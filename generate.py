"""TikTok video factory: makes ready-to-upload vertical videos for several channels.

Pipeline per video: Gemini (topic + script) -> edge-tts (voice + word timings)
-> Pixabay/Pexels (stock clips) -> ffmpeg (1080x1920 video with burned-in subtitles).
Usage: python generate.py            # all channels
       python generate.py <name>     # one channel (for testing)
"""
import asyncio
import datetime
import json
import os
import random
import re
import subprocess
import sys
import traceback
from pathlib import Path

import edge_tts
import requests
import yaml

ROOT = Path(__file__).parent
OUT = ROOT / "output"
HISTORY = ROOT / "history.json"
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SUB_STYLE = (
    "FontName=DejaVu Sans,Bold=1,FontSize=12,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=70"
)


MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.1-flash-lite", "gemini-flash-latest"] if m]


def gemini_json(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }
    last = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=90)
        if r.status_code == 200:
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        last = f"{model}: {r.status_code} {r.text[:300]}"
        print("Gemini error ->", last)
    raise RuntimeError(last)


def write_script(ch, recent, today):
    prompt = f"""You write viral TikTok voiceover scripts in English.
Channel niche: {ch['niche']}
Channel style: {ch['style']}
Today's date: {today}
Topics already used (do NOT repeat or rephrase): {recent or 'none'}

Pick ONE fresh, interesting, currently relevant topic in this niche and write a
40-55 second voiceover (110-150 words). Rules: the first sentence is a strong
hook; short punchy sentences; facts must be accurate; no guarantees of profit or
medical results; end with a one-line call to follow the channel.

Return JSON only:
{{"topic": str, "script": str,
 "keywords": [8 short English stock-footage search queries, in the order of the script],
 "caption": str (max 120 chars, no hashtags),
 "hashtags": [5 strings starting with #]}}"""
    return gemini_json(prompt)


async def make_voice(text, voice, rate, mp3):
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    words = []
    with open(mp3, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append((start, start + chunk["duration"] / 1e7, chunk["text"]))
    return words


def srt_time(t):
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h:02}:{m:02}:{int(s):02},{int(s % 1 * 1000):03}"


def write_srt(words, path, group=3):
    lines = []
    for i in range(0, len(words), group):
        chunk = words[i:i + group]
        end = words[i + group][0] if i + group < len(words) else chunk[-1][1]
        lines.append(f"{len(lines) + 1}\n{srt_time(chunk[0][0])} --> {srt_time(end)}\n"
                     f"{' '.join(w[2] for w in chunk).upper()}\n")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip())


def download(link, dest):
    with requests.get(link, stream=True, timeout=90) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for part in resp.iter_content(1 << 20):
                fh.write(part)


def fetch_pexels(query, dest, used):
    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_KEY},
        params={"query": query, "orientation": "portrait", "per_page": 10},
        timeout=30,
    )
    r.raise_for_status()
    vids = [v for v in r.json().get("videos", []) if ("px", v["id"]) not in used]
    if not vids:
        return None
    v = random.choice(vids[:5])
    files = [f for f in v["video_files"] if f.get("width") and f.get("height")]
    hd = [f for f in files if f["width"] >= 1080 and f["height"] > f["width"]]
    f = min(hd, key=lambda x: x["width"]) if hd else max(files, key=lambda x: x["width"])
    used.add(("px", v["id"]))
    download(f["link"], dest)
    return dest


def fetch_pixabay(query, dest, used):
    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": PIXABAY_KEY, "q": query, "per_page": 10, "safesearch": "true"},
        timeout=30,
    )
    r.raise_for_status()
    hits = [h for h in r.json().get("hits", []) if ("pb", h["id"]) not in used]
    if not hits:
        return None
    h = random.choice(hits[:5])
    for size in ("large", "medium", "small"):
        link = h["videos"].get(size, {}).get("url")
        if link:
            used.add(("pb", h["id"]))
            download(link, dest)
            return dest
    return None


def fetch_clip(query, dest, used):
    providers = []
    if PEXELS_KEY:
        providers.append(fetch_pexels)
    if PIXABAY_KEY:
        providers.append(fetch_pixabay)
    for fn in providers:
        try:
            got = fn(query, dest, used)
        except Exception as e:
            print(f"  {fn.__name__} '{query}' failed: {e}")
            got = None
        if got:
            return got
    return None


def build_video(work, keywords, out_path):
    total = duration(work / "voice.mp3") + 0.5
    used, clips = set(), []
    for i, k in enumerate(keywords):
        try:
            c = fetch_clip(k, work / f"raw{i}.mp4", used)
        except Exception as e:
            print(f"  clip '{k}' failed: {e}")
            c = None
        if c:
            clips.append(c)
    if not clips:
        raise RuntimeError("no stock clips found")
    seg = total / len(clips)
    names = []
    for i, c in enumerate(clips):
        p = work / f"seg{i}.mp4"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(c), "-t", f"{seg:.2f}",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(p)])
        names.append(p.name)
    (work / "list.txt").write_text("".join(f"file '{n}'\n" for n in names))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=work)
    run(["ffmpeg", "-y", "-i", "joined.mp4", "-i", "voice.mp3",
         "-vf", f"subtitles=subs.srt:force_style='{SUB_STYLE}'",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
         str(out_path.resolve())], cwd=work)


def main():
    if not (PEXELS_KEY or PIXABAY_KEY):
        sys.exit("Set PIXABAY_API_KEY (or PEXELS_API_KEY)")
    cfg = yaml.safe_load((ROOT / "channels.yaml").read_text(encoding="utf-8"))
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    today = datetime.date.today().isoformat()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    done = failed = 0

    for ch in cfg["channels"]:
        if only and ch["name"] != only:
            continue
        for n in range(ch.get("videos_per_day", cfg.get("videos_per_day", 1))):
            print(f"[{ch['name']}] video {n + 1}")
            try:
                recent = history.get(ch["name"], [])[-30:]
                data = write_script(ch, recent, today)
                slug = re.sub(r"[^a-z0-9]+", "-", data["topic"].lower()).strip("-")[:40]
                folder = OUT / ch["name"] / f"{today}_{n + 1}_{slug}"
                folder.mkdir(parents=True, exist_ok=True)
                work = folder / "_work"
                work.mkdir(exist_ok=True)

                words = asyncio.run(make_voice(
                    data["script"], ch.get("voice", "en-US-GuyNeural"), ch.get("rate", "+5%"),
                    work / "voice.mp3"))
                write_srt(words, work / "subs.srt")
                build_video(work, data["keywords"], folder / "video.mp4")

                (folder / "caption.txt").write_text(
                    f"{data['caption']}\n\n{' '.join(data['hashtags'])}\n", encoding="utf-8")
                (folder / "script.txt").write_text(data["script"], encoding="utf-8")
                for f in work.iterdir():
                    f.unlink()
                work.rmdir()
                history.setdefault(ch["name"], []).append(data["topic"])
                done += 1
            except Exception:
                failed += 1
                traceback.print_exc()

    HISTORY.write_text(json.dumps(history, indent=1), encoding="utf-8")
    print(f"Done: {done} videos, {failed} failed")
    if done == 0 and failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
