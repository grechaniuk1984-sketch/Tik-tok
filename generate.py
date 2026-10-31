"""TikTok / Shorts video factory (v2).

60+ second vertical videos: Gemini writes a scene-by-scene script, edge-tts voices it,
stock clips (Pixabay / Pexels) are cut in sync with the narration, word-highlight
subtitles sit at the bottom, an on-screen hook shows at the start.
Usage: python generate.py            # all channels
       python generate.py <name>     # one channel (for testing)
"""
import asyncio
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
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
MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.1-flash-lite", "gemini-flash-latest"] if m]

MIN_SECONDS = float(os.environ.get("MIN_SECONDS", "62"))  # Creator Rewards needs > 60 s
FONT = "DejaVu Sans"
SUB_SIZE = 76
SUB_MARGIN = 240     # px from the bottom edge (TikTok's own UI covers the lowest ~200 px)
HOOK_MARGIN = 260    # px from the top edge
HIGHLIGHT = "&H0000F0FF"   # active word colour (BGR) = yellow
LAYOUT = "auto"      # "auto": landscape clips sit on a blurred background (sharp); "fill": always crop to full screen
FORMATS = [
    "a surprising myth versus the reality",
    "a short real-life story with a twist",
    "3 mistakes most people make, counted down",
    "a 'what happens if' scenario",
    "a before/after case study with real numbers",
    "a head-to-head comparison of two options",
    "a step-by-step mini guide anyone can follow today",
    "a little-known fact that changes how people think",
    "a shocking statistic and what it really means",
    "the answer to a question every beginner asks",
]
STOP = {"the", "a", "an", "of", "to", "and", "in", "for", "your", "you", "why", "how", "what", "is",
        "are", "on", "with", "that", "this", "it", "most", "people", "about", "from", "can", "do"}
FALLBACK_QUERIES = ["city lights night", "nature landscape", "ocean waves", "people walking street", "clouds timelapse"]


# ---------------------------------------------------------------- Gemini
def gemini_json(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }
    last = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)
            if r.status_code == 200:
                try:
                    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                except Exception as e:
                    last = f"{model}: bad JSON {e}"
                    break
            last = f"{model}: {r.status_code} {r.text[:300]}"
            print("Gemini error ->", last)
            if r.status_code in (429, 500, 503):
                time.sleep(20 * (attempt + 1))
                continue
            break
    raise RuntimeError(last)


def write_script(ch, recent, today, fmt, longer=False):
    extra = "The previous attempt was too short: make it clearly LONGER (at least 240 words). " if longer else ""
    prompt = f"""You write viral, high-retention TikTok / YouTube Shorts voiceover scripts in English.
Channel niche: {ch['niche']}
Channel style: {ch['style']}
Today's date: {today}
Topics already used (do NOT repeat, rephrase or revisit them): {recent or 'none'}

Pick ONE fresh, specific and surprising topic in this niche that is clearly different in subject AND angle
from every topic listed above. Video format for this one: {fmt}. {extra}
Structure: (1) a shocking or curiosity-driven hook in the first sentence, (2) an open loop that
promises a payoff, (3) 3-4 concrete points with real numbers, names or examples, a new idea every
2-3 sentences, (4) a clear payoff, (5) a one-line call to follow.
Length: 75-90 seconds read aloud = 220-260 words in total, split into 20-24 scenes.
Rules: short punchy sentences, conversational tone, facts must be accurate, no guarantees of
profit or medical results, no filler.

Return JSON only:
{{"topic": str,
 "hook_text": str (max 6 words, shown on screen during the first seconds),
 "scenes": [{{"line": str (one sentence, 8-14 words),
              "keyword": str (2-3 word English stock-video search query describing a CONCRETE visual:
                              people, objects, places; no abstract words, no brand names; vary the shots)}}],
 "caption": str (max 120 chars, no hashtags),
 "hashtags": [5 strings starting with #]}}"""
    data = gemini_json(prompt)
    scenes = [s for s in data.get("scenes", []) if s.get("line") and s.get("keyword")]
    if len(scenes) < 10:
        raise RuntimeError("Gemini returned too few scenes")
    data["scenes"] = scenes
    return data


def too_similar(topic, recent):
    a = set(re.findall(r"[a-z0-9]+", topic.lower())) - STOP
    for old in recent:
        b = set(re.findall(r"[a-z0-9]+", old.lower())) - STOP
        if a and b and len(a & b) / min(len(a), len(b)) >= 0.6:
            return True
    return False


# ---------------------------------------------------------------- voice + timing
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


def scene_times(scenes, words, total):
    """(start, end) of every scene, aligned to the spoken words when possible."""
    counts = [max(1, len(s["line"].split())) for s in scenes]
    n = sum(counts)
    use_words = words and abs(len(words) - n) <= 0.1 * n
    starts, cum = [], 0
    for c in counts:
        starts.append(words[min(cum, len(words) - 1)][0] if use_words else total * cum / n)
        cum += c
    starts[0] = 0.0
    ends = starts[1:] + [total]
    return list(zip(starts, ends))


# ---------------------------------------------------------------- subtitles (ASS)
def ass_time(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs % 360000 // 6000:02}:{cs % 6000 // 100:02}.{cs % 100:02}"


def clean(w):
    return w.replace("{", "").replace("}", "").replace("\\", "").upper()


def write_ass(words, hook, path, group=3):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT},{SUB_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,70,70,{SUB_MARGIN},1
Style: Hook,{FONT},80,{HIGHLIGHT},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,8,70,70,{HOOK_MARGIN},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0.2)},{ass_time(3.4)},Hook,,0,0,0,,{{\\fad(200,350)}}{clean(hook)}")
    for g in range(0, len(words), group):
        chunk = words[g:g + group]
        for i, (start, end, _) in enumerate(chunk):
            nxt = g + i + 1
            stop = words[nxt][0] if nxt < len(words) else end
            parts = []
            for j, w in enumerate(chunk):
                colour = HIGHLIGHT if j == i else "&H00FFFFFF"
                parts.append(f"{{\\c{colour}&}}{clean(w[2])}")
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(stop)},Sub,,0,0,0,," + " ".join(parts))
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- ffmpeg helpers
def run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip())


def dims(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(path)]).decode().strip().split(",")
    return int(out[0]), int(out[1])


def seg_filter(w, h):
    grade = "fps=30,setsar=1,eq=contrast=1.06:saturation=1.12"
    if LAYOUT == "auto" and w > h:
        return ("[0:v]split[a][b];"
                "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5[bg];"
                "[b]scale=1080:-2:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{grade}[v]")
    return (f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop=1080:1920,{grade}[v]")


# ---------------------------------------------------------------- stock footage
def download(link, dest):
    with requests.get(link, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for part in resp.iter_content(1 << 20):
                fh.write(part)


def fetch_pexels(query, dest, used):
    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_KEY},
        params={"query": query, "orientation": "portrait", "per_page": 15},
        timeout=30,
    )
    r.raise_for_status()
    vids = [v for v in r.json().get("videos", []) if f"px:{v['id']}" not in used]
    if not vids:
        return None
    v = random.choice(vids[:6])
    files = [f for f in v["video_files"] if f.get("width") and f.get("height")]
    hd = [f for f in files if f["width"] >= 1080 and f["height"] > f["width"]]
    f = min(hd, key=lambda x: x["width"]) if hd else max(files, key=lambda x: x["width"])
    used[f"px:{v['id']}"] = True
    download(f["link"], dest)
    return dest


def fetch_pixabay(query, dest, used):
    time.sleep(0.7)  # free limit: 100 requests per minute
    params = {"key": PIXABAY_KEY, "q": query[:100], "per_page": 20, "safesearch": "true"}
    r = requests.get("https://pixabay.com/api/videos/", params=params, timeout=30)
    if r.status_code == 429:
        time.sleep(30)
        r = requests.get("https://pixabay.com/api/videos/", params=params, timeout=30)
    r.raise_for_status()
    hits = [h for h in r.json().get("hits", []) if f"pb:{h['id']}" not in used]
    large = [h for h in hits if h["videos"].get("large", {}).get("url")]
    pool = large or hits
    if not pool:
        return None
    h = random.choice(pool[:6])
    for size in ("large", "medium", "small"):
        link = h["videos"].get(size, {}).get("url")
        if link:
            used[f"pb:{h['id']}"] = True
            download(link, dest)
            return dest
    return None


def fetch_clip(query, dest, used):
    providers = []
    if PEXELS_KEY:
        providers.append(fetch_pexels)
    if PIXABAY_KEY:
        providers.append(fetch_pixabay)
    words = query.split()
    queries = [query] + ([" ".join(words[:2])] if len(words) > 2 else [])
    for q in queries:
        for fn in providers:
            try:
                got = fn(q, dest, used)
            except Exception as e:
                print(f"  {fn.__name__} '{q}' failed: {e}")
                got = None
            if got:
                return got
    return None


# ---------------------------------------------------------------- assemble
def build_video(work, scenes, times, out_path, used):
    clips = []
    for i, sc in enumerate(scenes):
        dest = work / f"raw{i}.mp4"
        c = fetch_clip(sc["keyword"], dest, used)
        if not c:
            c = fetch_clip(random.choice(FALLBACK_QUERIES), dest, used)
        if not c and clips:
            c = random.choice(clips)
        if not c:
            raise RuntimeError("no stock clips found")
        clips.append(c)

    names = []
    for i, (c, (start, end)) in enumerate(zip(clips, times)):
        d = max(1.2, end - start) + (0.4 if i == len(clips) - 1 else 0)
        cd = duration(c)
        w, h = dims(c)
        pre = ["-ss", f"{random.uniform(0, cd - d - 0.2):.2f}"] if cd >= d + 0.4 else ["-stream_loop", "-1"]
        p = work / f"seg{i}.mp4"
        run(["ffmpeg", "-y", *pre, "-i", str(c), "-t", f"{d:.2f}",
             "-filter_complex", seg_filter(w, h), "-map", "[v]", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", str(p)])
        names.append(p.name)

    (work / "list.txt").write_text("".join(f"file '{n}'\n" for n in names))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=work)
    run(["ffmpeg", "-y", "-i", "joined.mp4", "-i", "voice.mp3", "-vf", "ass=subs.ass",
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
         str(out_path.resolve())], cwd=work)


def main():
    if not (PEXELS_KEY or PIXABAY_KEY):
        sys.exit("Set PIXABAY_API_KEY (or PEXELS_API_KEY)")
    cfg = yaml.safe_load((ROOT / "channels.yaml").read_text(encoding="utf-8"))
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    today = datetime.date.today().isoformat()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    min_sec = float(cfg.get("min_seconds", MIN_SECONDS))
    used_clips = dict.fromkeys(history.get("_clips", []))
    done = failed = 0

    for ch in cfg["channels"]:
        if only and ch["name"] != only:
            continue
        for n in range(ch.get("videos_per_day", cfg.get("videos_per_day", 1))):
            print(f"[{ch['name']}] video {n + 1}")
            work = OUT / ch["name"] / f"_work{n + 1}"
            try:
                work.mkdir(parents=True, exist_ok=True)
                recent = history.get(ch["name"], [])[-60:]
                fmt = random.choice(FORMATS)
                short = False
                for attempt in range(5):
                    data = write_script(ch, recent, today, fmt, longer=short)
                    if too_similar(data["topic"], recent):
                        print(f"  topic too similar to an old one: {data['topic']}")
                        fmt = random.choice(FORMATS)
                        continue
                    script = " ".join(s["line"].strip() for s in data["scenes"])
                    words = asyncio.run(make_voice(
                        script, ch.get("voice", "en-US-GuyNeural"), ch.get("rate", "+5%"), work / "voice.mp3"))
                    total = duration(work / "voice.mp3")
                    if total >= min_sec:
                        break
                    print(f"  too short ({total:.0f}s), retrying")
                    short = True
                else:
                    raise RuntimeError("no fresh script of the required length after 5 attempts")

                hook = data.get("hook_text") or " ".join(data["scenes"][0]["line"].split()[:6])
                write_ass(words, hook, work / "subs.ass")
                times = scene_times(data["scenes"], words, total)

                slug = re.sub(r"[^a-z0-9]+", "-", data["topic"].lower()).strip("-")[:40]
                folder = OUT / ch["name"] / f"{today}_{n + 1}_{slug}"
                folder.mkdir(parents=True, exist_ok=True)
                build_video(work, data["scenes"], times, folder / "video.mp4", used_clips)

                (folder / "caption.txt").write_text(
                    f"{data['caption']}\n\n{' '.join(data['hashtags'])}\n", encoding="utf-8")
                (folder / "script.txt").write_text(script, encoding="utf-8")
                history.setdefault(ch["name"], []).append(data["topic"])
                print(f"  ok: {total:.0f}s, {len(data['scenes'])} scenes")
                done += 1
            except Exception:
                failed += 1
                traceback.print_exc()
            finally:
                shutil.rmtree(work, ignore_errors=True)

    history["_clips"] = list(used_clips)[-600:]
    HISTORY.write_text(json.dumps(history, indent=1), encoding="utf-8")
    print(f"Done: {done} videos, {failed} failed")
    if done == 0 and failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
