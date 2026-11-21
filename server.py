"""
Thai Voice Studio — เครื่องมือสร้างเสียงพากย์ AI ใช้งานในวงแลน
รัน:  python server.py        (เปิดเบราว์เซอร์ให้อัตโนมัติ)
      python server.py --no-browser --port 7860
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
import edge_tts
from edge_tts.srt_composer import Subtitle, compose
from datetime import timedelta

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
OUT = BASE / "outputs"
PREV = OUT / ".previews"
HIST_FILE = OUT / "history.json"
MAX_CHARS = 20000
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")        # ตั้งไว้ = ต้องใส่รหัสผ่านก่อนใช้
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "300"))  # เก็บไฟล์เสียงล่าสุดกี่รายการ
AUTH_TOKEN = hashlib.sha256(("tvs:" + APP_PASSWORD).encode()).hexdigest()
BITRATE = 48000  # edge-tts ส่งเสียงเป็น MP3 24kHz 48kbps mono

OUT.mkdir(exist_ok=True)
PREV.mkdir(exist_ok=True)

# ข้อมูลเสียงไทยที่รู้จัก (ชื่อ/คำอธิบายภาษาไทย)
THAI_INFO = {
    "th-TH-PremwadeeNeural": ("เปรมวดี", "หญิง", "ชัด สุภาพ เหมาะกับงานบรรยาย"),
    "th-TH-NiwatNeural": ("นิวัฒน์", "ชาย", "หนักแน่น เหมาะกับสารคดี"),
    "th-TH-AcharaNeural": ("อัจฉรา", "หญิง", "นุ่ม เป็นกันเอง"),
}
FALLBACK_THAI = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]
GENDER_TH = {"Female": "หญิง", "Male": "ชาย"}
PREVIEW_TEXT = "สวัสดีครับ รู้หรือไม่ว่าเสียงนี้สร้างขึ้นด้วยปัญญาประดิษฐ์ทั้งหมด"
PREVIEW_TEXT_NATURAL = "รู้หรือไม่? เสียงที่คุณกำลังฟังอยู่นี้… สร้างขึ้นด้วยปัญญาประดิษฐ์ทั้งหมด!"

_voices_cache = None
_hist_lock = asyncio.Lock()
_sem = asyncio.Semaphore(2)


async def get_voices():
    """ดึงรายชื่อเสียงจาก Microsoft ครั้งแรกแล้วเก็บไว้ ถ้าดึงไม่ได้ใช้รายชื่อสำรอง"""
    global _voices_cache
    if _voices_cache is not None:
        return _voices_cache
    thai, multi = [], []
    try:
        allv = await asyncio.wait_for(edge_tts.list_voices(), timeout=15)
        for v in allv:
            sn = v.get("ShortName", "")
            g = GENDER_TH.get(v.get("Gender", ""), "")
            if v.get("Locale") == "th-TH":
                name, gender, desc = THAI_INFO.get(sn, (sn.split("-")[-1].replace("Neural", ""), g, ""))
                thai.append({"id": sn, "name": name, "gender": gender or g, "desc": desc, "group": "thai"})
            elif "Multilingual" in sn:
                nm = sn.split("-")[-1].replace("MultilingualNeural", "")
                multi.append({"id": sn, "name": nm, "gender": g,
                              "desc": f"หลายภาษา ({v.get('Locale')}) อาจมีสำเนียงต่างชาติ", "group": "multi"})
        _voices_cache = {"thai": thai, "multi": multi, "online": True}
    except Exception as e:  # noqa: BLE001
        print(f"[!] ดึงรายชื่อเสียงจากอินเทอร์เน็ตไม่ได้ ({e.__class__.__name__}) ใช้รายชื่อสำรอง")
        thai = []
        for sn in FALLBACK_THAI:
            name, gender, desc = THAI_INFO[sn]
            thai.append({"id": sn, "name": name, "gender": gender, "desc": desc, "group": "thai"})
        return {"thai": thai, "multi": [], "online": False}  # ไม่ cache จะได้ลองใหม่ครั้งหน้า
    return _voices_cache


def fmt_params(rate, pitch, volume):
    rate = max(-50, min(100, int(rate)))
    pitch = max(-50, min(50, int(pitch)))
    volume = max(-50, min(50, int(volume)))
    return f"{rate:+d}%", f"{pitch:+d}Hz", f"{volume:+d}%"


def slugify(text):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text).strip()
    s = re.sub(r"\s+", "_", s)[:28].strip("_.")
    return s or "voice"


async def synth_bytes(text, voice, rate, pitch, volume):
    """สร้างเสียง 1 ท่อน ได้ (ไฟล์ MP3 เป็น bytes, รายการซับ)"""
    com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume, boundary="SentenceBoundary")
    sub = edge_tts.SubMaker()
    buf = bytearray()
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
        elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
            sub.feed(chunk)
    if not buf:
        raise RuntimeError("ไม่ได้รับเสียงกลับมา")
    return bytes(buf), sub.cues


def write_out(audio: bytes, cues, mp3_path: Path, srt_path: Path | None):
    tmp = mp3_path.with_suffix(".part")
    tmp.write_bytes(audio)
    os.replace(tmp, mp3_path)
    if srt_path is not None:
        srt_path.write_text(compose(cues), encoding="utf-8")
    return len(audio)


async def synthesize(text, voice, rate, pitch, volume, mp3_path: Path, srt_path: Path | None):
    audio, cues = await synth_bytes(text, voice, rate, pitch, volume)
    return write_out(audio, cues, mp3_path, srt_path)


# ================= โหมดเล่าเรื่องแบบธรรมชาติ + แท็กอารมณ์ =================
# edge-tts ปรับได้แค่ ความเร็ว / ระดับเสียง / ความดัง ต่อ 1 คำขอ
# เลยแบ่งข้อความเป็นท่อน (ประโยค) ให้แต่ละท่อนมีจังหวะต่างกันเล็กน้อย แล้วต่อด้วยช่วงเงียบที่จังหวะเหมาะสม

# เฟรม MP3 เงียบ 24 มิลลิวินาที (MPEG-2 Layer III 24kHz 48kbps mono รูปแบบเดียวกับที่ edge-tts ส่งมา)
SILENT_FRAME = bytes.fromhex("fff364c40000000348000000004c414d455555554c414d45332e313030" + "55" * 115)
assert len(SILENT_FRAME) == 144
BYTES_PER_SEC = BITRATE // 8


def silence(ms):
    return SILENT_FRAME * max(0, round(ms / 24))


# แท็กอารมณ์: ค่าที่บวกเพิ่มจากค่าที่ตั้งไว้ (ความเร็ว %, ระดับเสียง Hz, ความดัง %)
TAGS = {
    "ปกติ": (0, 0, 0),
    "สารคดี": (-6, -3, 0),
    "ลึกลับ": (-16, -10, -5),
    "ตื่นเต้น": (14, 6, 6),
    "กระซิบ": (-12, -4, -28),
    "จริงจัง": (-6, -7, 4),
    "เศร้า": (-16, -9, -10),
    "สดใส": (6, 12, 2),
    "นุ่มนวล": (-10, 2, -10),
    "โกรธ": (10, -4, 14),
    "ช้า": (-15, 0, 0),
    "เร็ว": (15, 0, 0),
    "ดัง": (0, 0, 15),
    "เบา": (0, 0, -15),
}
TAG_RE = re.compile(r"\[([^\[\]\n]{1,24})\]")
PAUSE_RE = re.compile(r"^\s*(?:หยุด|pause)\s*([\d.]+)?\s*(?:วิ|วินาที|s|sec)?\s*$", re.I)
WORDY = re.compile(r"[0-9A-Za-zก-ฮะ-์]")      # มีตัวอักษรที่อ่านออกเสียงได้
LOOKS_TAG = re.compile(r"^[฀-๿\s]{1,14}$")              # แท็กภาษาไทยที่พิมพ์ผิด จะไม่อ่านออกเสียง
SENT_END = re.compile(r"(?<=[?!？！…])\s+|(?<=\.\.\.)\s+|(?<=[^\d]\.)\s+")
MAX_SEGMENTS = 600


def has_tags(text):
    for m in TAG_RE.finditer(text):
        name = m.group(1).strip()
        if name in TAGS or PAUSE_RE.match(name) or LOOKS_TAG.match(name):
            return True
    return False


def _split_long(sent, limit=110):
    """ประโยคยาวมาก ตัดตามช่องว่างให้เป็นท่อนละ ~50–100 ตัวอักษร"""
    if len(sent) <= limit or " " not in sent:
        return [sent]
    parts, cur = [], ""
    for w in sent.split(" "):
        if cur and len(cur) + 1 + len(w) > 95 and len(cur) >= 45:
            parts.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        if parts and len(cur) < 20:
            parts[-1] += " " + cur
        else:
            parts.append(cur)
    return parts


def parse_script(text):
    """แปลงข้อความเป็นรายการ: ("seg", ข้อความ, (r,p,v) ของแท็ก, ชนิดการเว้นหลังท่อน) หรือ ("pause", ms)"""
    items, unknown = [], []
    style = (0, 0, 0)
    pos = 0
    chunks = []  # (ข้อความ, style)
    for m in TAG_RE.finditer(text):
        name = m.group(1).strip()
        pm = PAUSE_RE.match(name)
        if name in TAGS or pm or LOOKS_TAG.match(name):
            chunks.append((text[pos:m.start()], style))
            pos = m.end()
            if name in TAGS:
                style = TAGS[name]
            elif pm:
                sec = float(pm.group(1)) if pm.group(1) else 0.8
                chunks.append((("__PAUSE__", int(min(10, max(0.1, sec)) * 1000)), style))
            else:
                unknown.append(name)
    chunks.append((text[pos:], style))

    for body, st in chunks:
        if isinstance(body, tuple):
            items.append(("pause", body[1]))
            continue
        paras = re.split(r"\n\s*\n", body)
        for pi, para in enumerate(paras):
            lines = [ln for ln in para.split("\n")]
            for li, line in enumerate(lines):
                sents = [s.strip() for s in SENT_END.split(line.strip()) if s.strip()]
                for si, s in enumerate(sents):
                    parts = _split_long(s)
                    for ci, c in enumerate(parts):
                        last_c, last_s = ci == len(parts) - 1, si == len(sents) - 1
                        if not last_c:
                            brk = "clause"
                        elif not last_s:
                            brk = "sent"
                        elif li < len(lines) - 1:
                            brk = "line"
                        elif pi < len(paras) - 1:
                            brk = "para"
                        else:
                            brk = "sent"
                        if WORDY.search(c):
                            items.append(("seg", c, st, brk))
                        elif "…" in c or "..." in c:
                            items.append(("pause", 500))  # "…" อยู่โดดๆ = หยุดหายใจ
    return items, unknown


# จังหวะพื้นฐานหลังท่อน (มิลลิวินาที)
BASE_PAUSE = {"clause": 90, "sent": 260, "line": 420, "para": 750}
PLAIN_PAUSE = {"clause": 40, "sent": 140, "line": 260, "para": 450}
MICRO_R = [0, 3, -2, 2, -3, 1, -1, 2]
MICRO_P = [2, -2, 3, -1, 0, -3, 1, -2]


def plan_prosody(items, base, natural, level):
    """คำนวณ ความเร็ว/ระดับเสียง/ความดัง และช่วงหยุด ของแต่ละท่อน"""
    k = max(0.0, min(1.0, level / 100))
    scale = 1.6 * k
    segs = [i for i, x in enumerate(items) if x[0] == "seg"]
    plan = []
    seed = sum(ord(c) for c in "".join(x[1] for x in items if x[0] == "seg")) % 8
    for n, idx in enumerate(segs):
        _, txt, st, brk = items[idx]
        r, p, v = (base[0] + st[0], base[1] + st[1], base[2] + st[2])
        pause = (BASE_PAUSE if natural else PLAIN_PAUSE)[brk]
        if natural:
            t = txt.rstrip()
            dr = dp = dv = 0
            if t.endswith(("?", "？")) or re.search(r"(ไหม|มั้ย|หรือเปล่า|อะไร|ทำไม|อย่างไร|ยังไง)\s*$", t):
                dr, dp = -2, 6
                pause = max(pause, 420)
            elif t.endswith(("!", "！")):
                dr, dp, dv = 8, 4, 6
                pause = max(pause, 380)
            elif t.endswith(("…", "...")):
                dr, dp, dv = -10, -4, -3
                pause = max(pause, 650)
            if n == 0 and len(segs) > 1:          # ประโยคเปิด ดึงความสนใจ
                dr, dp, dv = dr - 5, dp + 3, dv + 3
            elif n == len(segs) - 1 and len(segs) > 2:  # ประโยคปิด ช้าลงนิดให้ฟังจบสวย
                dr, dp = dr - 6, dp + 1
            if re.search(r"\d", t):               # ตัวเลข อ่านช้าลงให้ฟังชัด
                dr -= 5
            if len(t) <= 14:                       # ประโยคสั้น ห้วนๆ เน้นหนัก
                dr, dv = dr - 8, dv + 3
            elif len(t) > 80:
                dr += 4
            j = (n + seed) % 8
            dr += MICRO_R[j] * 1.5
            dp += MICRO_P[j] * 1.5
            r, p, v = r + dr * scale, p + dp * scale, v + dv * scale
            pause = int(pause * (0.6 + 0.7 * k))
        # ประโยคก่อนช่วงหยุดที่ผู้ใช้ใส่เอง ไม่ต้องเว้นซ้ำ
        if idx + 1 < len(items) and items[idx + 1][0] == "pause":
            pause = min(pause, 120)
        plan.append({"i": idx, "text": txt, "rate": fmt_params(r, p, v), "pause": pause})
    return plan


async def synthesize_script(text, voice, base, natural, level, mp3_path: Path, srt_path: Path | None):
    items, unknown = parse_script(text)
    plan = plan_prosody(items, base, natural, level)
    if not plan:
        raise ValueError("ไม่มีข้อความให้อ่าน (มีแต่แท็ก)")
    if len(plan) > MAX_SEGMENTS:
        raise ValueError(f"ข้อความแบ่งได้ {len(plan)} ท่อน เกิน {MAX_SEGMENTS} ท่อน ลองแบ่งสร้างทีละส่วน")
    sem = asyncio.Semaphore(3)

    async def one(seg):
        async with sem:
            for attempt in range(3):
                try:
                    return await synth_bytes(seg["text"], voice, *seg["rate"])
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1 + attempt)

    results = await asyncio.gather(*(one(s) for s in plan))
    by_idx = {s["i"]: (s, res) for s, res in zip(plan, results)}

    out, cues = bytearray(), []
    for i, it in enumerate(items):
        if it[0] == "pause":
            out += silence(it[1])
            continue
        seg, (audio, scues) = by_idx[i]
        off = timedelta(seconds=len(out) / BYTES_PER_SEC)
        dur = timedelta(seconds=len(audio) / BYTES_PER_SEC)
        if scues:
            for c in scues:
                cues.append(Subtitle(index=len(cues) + 1, start=c.start + off, end=c.end + off, content=c.content))
        else:
            cues.append(Subtitle(index=len(cues) + 1, start=off, end=off + dur, content=seg["text"]))
        out += audio
        out += silence(seg["pause"])
    size = write_out(bytes(out), cues, mp3_path, srt_path)
    return size, len(plan), unknown


def load_hist():
    try:
        return json.loads(HIST_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def save_hist(items):
    tmp = HIST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, HIST_FILE)


def err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


def net_error_msg(e):
    name = e.__class__.__name__
    if "Connect" in name or "DNS" in name or "Timeout" in name or isinstance(e, (OSError, asyncio.TimeoutError)):
        return "เชื่อมต่อบริการเสียงไม่ได้ — ตรวจว่าเครื่องที่รันโปรแกรมต่ออินเทอร์เน็ตอยู่"
    if "NoAudio" in name:
        return "บริการไม่ส่งเสียงกลับมา ลองเปลี่ยนเสียงหรือแก้ข้อความ (เช่น ตัดสัญลักษณ์แปลกๆ ออก)"
    return f"สร้างเสียงไม่สำเร็จ: {e}"


# ---------------- routes ----------------
routes = web.RouteTableDef()


@routes.get("/")
async def index(_):
    # ส่งเป็นข้อมูลธรรมดา (ไม่ใช้ FileResponse/sendfile ซึ่งพังบนเซิร์ฟเวอร์คลาวด์บางเจ้า)
    return web.Response(body=(STATIC / "index.html").read_bytes(), content_type="text/html",
                        charset="utf-8", headers={"Cache-Control": "no-cache"})


@routes.get("/api/config")
async def config(_):
    v = await get_voices()
    return web.json_response({"voices": v, "max_chars": MAX_CHARS, "tags": list(TAGS)})


@routes.post("/api/tts")
async def tts(req):
    try:
        data = await req.json()
    except Exception:  # noqa: BLE001
        return err("ข้อมูลไม่ถูกต้อง")
    text = str(data.get("text", "")).strip()
    voice = str(data.get("voice", "")).strip()
    if not text:
        return err("กรุณาพิมพ์ข้อความ")
    if len(text) > MAX_CHARS:
        return err(f"ข้อความยาวเกิน {MAX_CHARS:,} ตัวอักษร")
    if not re.fullmatch(r"[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z]+Neural", voice):
        return err("เลือกเสียงไม่ถูกต้อง")
    base = tuple(int(data.get(k, 0) or 0) for k in ("rate", "pitch", "volume"))
    rate, pitch, volume = fmt_params(*base)
    want_srt = bool(data.get("srt", True))
    natural = bool(data.get("natural", False))
    level = max(0, min(100, int(data.get("level", 60) or 0)))

    vid = uuid.uuid4().hex[:8]
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}_{slugify(TAG_RE.sub(' ', text))}_{vid}"
    mp3 = OUT / f"{stem}.mp3"
    srt = OUT / f"{stem}.srt" if want_srt else None
    t0 = time.time()
    nseg, unknown = 1, []
    async with _sem:
        try:
            if natural or has_tags(text):
                size, nseg, unknown = await synthesize_script(text, voice, base, natural, level, mp3, srt)
            else:
                size = await synthesize(text, voice, rate, pitch, volume, mp3, srt)
        except ValueError as e:
            return err(str(e))
        except Exception as e:  # noqa: BLE001
            print(f"[x] {e.__class__.__name__}: {e}")
            return err(net_error_msg(e), 502)
    item = {
        "id": vid, "text": text, "chars": len(text), "voice": voice,
        "voice_name": data.get("voice_name") or voice, "tone": data.get("tone", ""),
        "rate": rate, "pitch": pitch, "volume": volume,
        "mp3": mp3.name, "srt": srt.name if srt else None,
        "duration": round(size * 8 / BITRATE, 2), "created": int(time.time()),
        "natural": natural, "level": level if natural else None,
    }
    async with _hist_lock:
        h = load_hist()
        h.insert(0, item)
        for old in h[MAX_HISTORY:]:  # ลบรายการเก่าเกินจำนวนที่กำหนด กันพื้นที่เต็ม
            for k in ("mp3", "srt"):
                if old.get(k) and (OUT / old[k]).exists():
                    (OUT / old[k]).unlink()
        save_hist(h[:MAX_HISTORY])
    print(f"[✓] {item['duration']:.1f} วินาที · {voice} · {nseg} ท่อน · {time.time() - t0:.1f}s · {mp3.name}")
    return web.json_response({**item, "unknown_tags": unknown})


@routes.post("/api/preview")
async def preview(req):
    data = await req.json()
    voice = str(data.get("voice", ""))
    if not re.fullmatch(r"[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z]+Neural", voice):
        return err("เสียงไม่ถูกต้อง")
    base = tuple(int(data.get(k, 0) or 0) for k in ("rate", "pitch", "volume"))
    rate, pitch, volume = fmt_params(*base)
    natural = bool(data.get("natural", False))
    level = max(0, min(100, int(data.get("level", 60) or 0)))
    nat = f"_n{level}" if natural else ""
    key = re.sub(r"[^A-Za-z0-9+-]", "", f"{voice}_{rate}_{pitch}_{volume}{nat}")
    p = PREV / f"{key}.mp3"
    if not p.exists():
        async with _sem:
            try:
                if natural:
                    await synthesize_script(PREVIEW_TEXT_NATURAL, voice, base, True, level, p, None)
                else:
                    await synthesize(PREVIEW_TEXT, voice, rate, pitch, volume, p, None)
            except Exception as e:  # noqa: BLE001
                return err(net_error_msg(e), 502)
    return web.json_response({"url": f"/files/.previews/{p.name}"})


@routes.get("/api/history")
async def history(_):
    h = [x for x in load_hist() if (OUT / x["mp3"]).exists()]
    return web.json_response(h)


@routes.delete("/api/history/{id}")
async def delete_item(req):
    vid = req.match_info["id"]
    async with _hist_lock:
        h = load_hist()
        keep = []
        for x in h:
            if x["id"] == vid:
                for k in ("mp3", "srt"):
                    if x.get(k):
                        f = OUT / x[k]
                        if f.exists():
                            f.unlink()
            else:
                keep.append(x)
        save_hist(keep)
    return web.json_response({"ok": True})


@routes.get("/files/{name:.+}")
async def files(req):
    name = req.match_info["name"]
    p = (OUT / name).resolve()
    if OUT.resolve() not in p.parents or not p.is_file():
        raise web.HTTPNotFound()
    headers = {}
    if req.query.get("dl"):
        nice = req.query.get("name") or p.name
        nice = re.sub(r'[\\/:*?"<>|\r\n]+', "_", nice)
        headers["Content-Disposition"] = f"attachment; filename=\"voice{p.suffix}\"; filename*=UTF-8''{quote(nice)}"
    if p.suffix == ".mp3":
        return web.Response(body=p.read_bytes(), content_type="audio/mpeg", headers=headers)
    return web.Response(body=p.read_bytes(), content_type="text/plain", charset="utf-8", headers=headers)


LOGIN_HTML = """<!DOCTYPE html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>เข้าสู่ระบบ · Thai Voice Studio</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f7f8;font:15px/1.5 "Prompt","Leelawadee UI",Tahoma,sans-serif;color:#18181b}
form{background:#fff;border:1px solid #e7e7ea;border-radius:16px;padding:28px;width:320px;max-width:90vw}h1{font-size:19px;margin:0 0 4px}p{color:#71717a;margin:0 0 18px;font-size:13.5px}
input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #e7e7ea;border-radius:10px;font:inherit;margin-bottom:12px}button{width:100%;padding:11px;border:0;border-radius:10px;background:#18181b;color:#fff;font:inherit;cursor:pointer}
.e{color:#b91c1c;font-size:13px;margin:-4px 0 10px}</style></head><body><form method="post" action="/login"><h1>Thai Voice Studio</h1><p>ใส่รหัสผ่านเพื่อใช้งาน</p>
<input type="password" name="password" placeholder="รหัสผ่าน" autofocus required>__ERR__<button>เข้าสู่ระบบ</button></form></body></html>"""


def _is_https(req):
    return req.secure or req.headers.get("X-Forwarded-Proto", "").lower() == "https"


@web.middleware
async def auth_mw(req, handler):
    if not APP_PASSWORD or req.path == "/login":
        return await handler(req)
    if hmac.compare_digest(req.cookies.get("tvs_auth", ""), AUTH_TOKEN):
        return await handler(req)
    if req.path.startswith(("/api", "/files")):
        return web.json_response({"error": "กรุณาเข้าสู่ระบบ"}, status=401)
    raise web.HTTPFound("/login")


@routes.get("/login")
async def login_page(_):
    return web.Response(text=LOGIN_HTML.replace("__ERR__", ""), content_type="text/html")


@routes.post("/login")
async def login_post(req):
    form = await req.post()
    if APP_PASSWORD and hmac.compare_digest(str(form.get("password", "")), APP_PASSWORD):
        resp = web.HTTPFound("/")
        secure = _is_https(req)
        resp.set_cookie("tvs_auth", AUTH_TOKEN, max_age=60 * 60 * 24 * 30, httponly=True,
                        secure=secure, samesite="None" if secure else "Lax", path="/")
        raise resp
    await asyncio.sleep(1)
    return web.Response(text=LOGIN_HTML.replace("__ERR__", '<div class="e">รหัสผ่านไม่ถูกต้อง</div>'),
                        content_type="text/html", status=401)


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:  # noqa: BLE001
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7860)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    logging.basicConfig(level=logging.INFO, format="%(message)s")  # แสดงทุกครั้งที่มีคนเปิดเว็บใน Logs
    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[auth_mw])
    app.add_routes(routes)

    print("=" * 56)
    print("  Thai Voice Studio — เครื่องมือสร้างเสียงพากย์ AI")
    print("=" * 56)
    print(f"  เครื่องนี้:        http://localhost:{a.port}")
    for ip in lan_ips():
        print(f"  เครื่องอื่นในวงแลน: http://{ip}:{a.port}")
    print(f"  ไฟล์เสียงเก็บที่:   {OUT}")
    print(f"  รหัสผ่าน:          {'เปิดใช้' if APP_PASSWORD else 'ไม่มี (ใครในวงแลนก็ใช้ได้)'}")
    print("  ปิดโปรแกรม: กด Ctrl+C หรือปิดหน้าต่างนี้")
    print("=" * 56)
    if not a.no_browser:
        webbrowser.open(f"http://localhost:{a.port}")
    web.run_app(app, host=a.host, port=a.port, print=None,
                access_log_format='%a "%r" %s %b')


if __name__ == "__main__":
    main()
