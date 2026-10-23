import base64
import json
import time
import urllib.parse

import requests
import websocket
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")


def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://loot-link.com",
        "Referer": "https://loot-link.com/",
    })
    return s


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def resolve(url: str) -> dict:
    """
    Resolve a lootlabs link to its destination.

    Steps:
      1. follow redirects to get the final loot-link.com/<id> page
      2. hit their /api/v1/links/<id> endpoint for the task payload
      3. connect to the websocket, complete the task, receive dest
      4. fall back to decoding the client-side encrypted blob if the api
         flow is blocked (base64 -> xor with 5-char prefix)

    Returns {"ok": bool, "dest": str, "error": str}
    """
    try:
        s = _session()

        # --- 1. resolve shortlink to the loot-link page ---
        r = s.get(url, allow_redirects=True, timeout=20)
        if r.status_code != 200:
            return {"ok": False, "dest": "", "error": f"http {r.status_code} on shortlink"}

        final_url = r.url
        # loot-link.com/<slug>  (or loot-labs.com)
        parts = urllib.parse.urlparse(final_url)
        slug = parts.path.strip("/").split("/")[-1]
        if not slug:
            return {"ok": False, "dest": "", "error": "could not extract slug"}

        # --- 2. api call for task data ---
        api = f"https://loot-link.com/api/v1/links/{slug}"
        a = s.get(api, timeout=20)
        if a.status_code != 200:
            return {"ok": False, "dest": "", "error": f"http {a.status_code} on api"}

        try:
            data = a.json()
        except Exception:
            return {"ok": False, "dest": "", "error": "api returned non-json"}

        if not data.get("success", True):
            return {"ok": False, "dest": "", "error": data.get("message", "api rejected")}

        link_obj = data.get("data", data)

        # --- 3. websocket task flow ---
        dest = _ws_flow(s, link_obj)
        if dest:
            return {"ok": True, "dest": dest, "error": ""}

        # --- 4. fallback: decode the client blob if present ---
        blob = link_obj.get("encrypted") or link_obj.get("destination")
        if blob:
            dec = _decode_blob(blob)
            if dec:
                return {"ok": True, "dest": dec, "error": ""}

        return {"ok": False, "dest": "", "error": "no destination returned"}
    except Exception as e:
        return {"ok": False, "dest": "", "error": f"{type(e).__name__}: {e}"}


def _ws_flow(s, link_obj) -> str:
    """
    Connect to the lootlabs websocket, send the task-complete handshake,
    and read back the destination. Best-effort; returns "" on any failure.
    """
    ws_url = link_obj.get("websocket") or link_obj.get("ws_url")
    if not ws_url:
        return ""

    task_id = link_obj.get("task_id") or link_obj.get("id", "")
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())

    try:
        ws = websocket.create_connection(
            ws_url,
            timeout=15,
            header={
                "User-Agent": UA,
                "Origin": "https://loot-link.com",
                "Cookie": cookie,
            },
        )
        ws.send(json.dumps({"type": "join", "taskId": task_id}))
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                msg = ws.recv()
            except Exception:
                break
            try:
                j = json.loads(msg)
            except Exception:
                continue
            if j.get("type") in ("destination", "complete", "unlock"):
                d = j.get("destination") or j.get("url") or j.get("data")
                if d:
                    ws.close()
                    return d
        ws.close()
    except Exception:
        pass
    return ""


def _decode_blob(blob: str) -> str:
    """
    Some lootlabs pages embed the destination as:
      base64( xor( url, first_5_chars_of_blob ) )
    Try both orders.
    """
    try:
        raw = base64.b64decode(blob)
    except Exception:
        return ""
    try:
        key = raw[:5]
        plain = _xor(raw[5:], key).decode("utf-8", "ignore")
        if plain.startswith("http"):
            return plain
    except Exception:
        pass
    try:
        key = blob[:5].encode()
        plain = _xor(raw, key).decode("utf-8", "ignore")
        if plain.startswith("http"):
            return plain
    except Exception:
        pass
    return ""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/bypass", methods=["POST"])
def bypass():
    body = request.get_json(silent=True) or {}
    link = (body.get("link") or "").strip()
    if not link:
        return jsonify({"ok": False, "dest": "", "error": "missing link"}), 400
    if not (link.startswith("http://") or link.startswith("https://")):
        return jsonify({"ok": False, "dest": "", "error": "not a url"}), 400
    result = resolve(link)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)