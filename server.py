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


async def synthesize(text, voice, rate, pitch, volume, mp3_path: Path, srt_path: Path | None):
    com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume, boundary="SentenceBoundary")
    sub = edge_tts.SubMaker()
    tmp = mp3_path.with_suffix(".part")
    got = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                    got += len(chunk["data"])
                elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                    sub.feed(chunk)
        if not got:
            raise RuntimeError("ไม่ได้รับเสียงกลับมา")
        os.replace(tmp, mp3_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    if srt_path is not None:
        srt_path.write_text(sub.get_srt(), encoding="utf-8")
    return got


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
    return web.json_response({"voices": v, "max_chars": MAX_CHARS})


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
    rate, pitch, volume = fmt_params(data.get("rate", 0), data.get("pitch", 0), data.get("volume", 0))
    want_srt = bool(data.get("srt", True))

    vid = uuid.uuid4().hex[:8]
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}_{slugify(text)}_{vid}"
    mp3 = OUT / f"{stem}.mp3"
    srt = OUT / f"{stem}.srt" if want_srt else None
    t0 = time.time()
    async with _sem:
        try:
            size = await synthesize(text, voice, rate, pitch, volume, mp3, srt)
        except Exception as e:  # noqa: BLE001
            print(f"[x] {e.__class__.__name__}: {e}")
            return err(net_error_msg(e), 502)
    item = {
        "id": vid, "text": text, "chars": len(text), "voice": voice,
        "voice_name": data.get("voice_name") or voice, "tone": data.get("tone", ""),
        "rate": rate, "pitch": pitch, "volume": volume,
        "mp3": mp3.name, "srt": srt.name if srt else None,
        "duration": round(size * 8 / BITRATE, 2), "created": int(time.time()),
    }
    async with _hist_lock:
        h = load_hist()
        h.insert(0, item)
        for old in h[MAX_HISTORY:]:  # ลบรายการเก่าเกินจำนวนที่กำหนด กันพื้นที่เต็ม
            for k in ("mp3", "srt"):
                if old.get(k) and (OUT / old[k]).exists():
                    (OUT / old[k]).unlink()
        save_hist(h[:MAX_HISTORY])
    print(f"[✓] {item['duration']:.1f} วินาที · {voice} · {time.time() - t0:.1f}s · {mp3.name}")
    return web.json_response(item)


@routes.post("/api/preview")
async def preview(req):
    data = await req.json()
    voice = str(data.get("voice", ""))
    if not re.fullmatch(r"[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z]+Neural", voice):
        return err("เสียงไม่ถูกต้อง")
    rate, pitch, volume = fmt_params(data.get("rate", 0), data.get("pitch", 0), data.get("volume", 0))
    key = re.sub(r"[^A-Za-z0-9+-]", "", f"{voice}_{rate}_{pitch}_{volume}")
    p = PREV / f"{key}.mp3"
    if not p.exists():
        async with _sem:
            try:
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
