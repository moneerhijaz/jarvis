"""Shared vision helpers: talk to the local multimodal model (LM Studio, e.g. Qwen3-VL)
for screen understanding and click-target location. Used by ui.* and screen.look.
All heavy deps (httpx, mss, Pillow) are lazy.
"""
from __future__ import annotations

import base64
import json
import re
from io import BytesIO

_VISION_HINT = re.compile(r"(vl|vision|llava|multimodal|qwen.*vl|minicpm|moondream)", re.I)


def lm_endpoint(settings):
    """Endpoint for vision (describe) calls — its own LAN host if set, else the brain's."""
    m = settings.models
    prov = settings.provider_for("brain")
    if getattr(m, "vision_base_url", ""):
        return m.vision_base_url.rstrip("/"), (m.vision_api_key or prov.api_key)
    base = getattr(m, "brain_base_url", "") or prov.base_url
    return base.rstrip("/"), (getattr(m, "brain_api_key", "") or prov.api_key)


def grounding_endpoint(settings):
    """Endpoint for GUI-grounding (click-locating) calls — its own host if set, else vision's."""
    m = settings.models
    if getattr(m, "grounding_base_url", ""):
        prov = settings.provider_for("brain")
        return m.grounding_base_url.rstrip("/"), (m.grounding_api_key or prov.api_key)
    return lm_endpoint(settings)


def resolve_vision_model(settings, base, key) -> str | None:
    vm = getattr(settings.models, "vision_model", "") or ""
    if vm:
        return vm
    try:
        import httpx
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
        for m in r.json().get("data", []):
            if _VISION_HINT.search(m.get("id", "")):
                return m["id"]
    except Exception:
        pass
    return None


def capture_primary(max_width: int = 0):
    """Grab the whole virtual desktop; return (PIL image, scale, monitor).

    mss monitor 0 is the full screen space across displays. max_width=0 keeps native
    resolution, so returned coordinates can be mapped back exactly.
    """
    import mss
    from PIL import Image
    with mss.mss() as sct:
        mon = sct.monitors[0]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    scale = 1.0
    if max_width and img.width > max_width:
        scale = img.width / max_width
        img = img.resize((max_width, int(img.height / scale)))
    return img, scale, mon


def _chat_image(base, key, model, prompt: str, img, max_tokens: int = 512, temperature: float = 0.2):
    """POST an image + prompt to a given model/endpoint; return (text, model) or (None, reason)."""
    if not model:
        return None, "no vision model available (load one in LM Studio or set the model in VISION)"
    try:
        import httpx
        buf = BytesIO(); img.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                # Disable Qwen3 "thinking" so the answer lands in content, not reasoning_content
                # (a small hybrid model can otherwise spend the whole budget thinking -> empty answer).
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}
        r = httpx.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json=body, timeout=120)
        r.raise_for_status()
        msg = (r.json().get("choices") or [{}])[0].get("message", {})
        content = (msg.get("content") or "").strip()
        if not content:                                   # answer empty -> reasoning model dumped it here
            content = (msg.get("reasoning_content") or msg.get("reasoning") or "")
        content = re.sub(r"(?is)<think>.*?</think>", "", content).strip()   # drop any think wrapper
        return content, model
    except Exception as e:
        return None, f"vision call failed: {e}"


# ---------------- the request/response CONTRACT ----------------
# The brain asks for a result in an explicit schema; the VL output is parsed + validated
# (one strict retry on malformed JSON); the model can also explicitly decline. Returns a
# uniform {"ok": True, "data": {...}} or {"ok": False, "reason": "..."} so the caller never
# has to trust raw prose and can replan on a clean, explicit failure.
def _ask_json(base, key, model, task, img, required, max_tokens=400):
    if not model:
        return {"ok": False, "reason": "no vision model available (load one or set it in VISION)"}
    schema = "{" + ", ".join(f'"{k}": {t}' for k, t in required.items()) + "}"
    base_prompt = (task + "\nRespond with ONLY JSON matching this schema (no prose, no markdown): "
                   + schema + '\nIf you cannot determine it from the screen, respond exactly '
                   '{"ok": false, "reason": "<short why>"}. /no_think')
    last = "vision returned no usable output"
    for attempt in range(2):
        prompt = base_prompt + (" Output STRICT valid JSON only." if attempt else "")
        txt, info = _chat_image(base, key, model, prompt, img, max_tokens=max_tokens, temperature=0)
        if txt is None:
            return {"ok": False, "reason": info}
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            last = "vision did not return JSON"; continue
        try:
            data = json.loads(m.group(0))
        except Exception:
            last = "vision returned malformed JSON"; continue
        if data.get("ok") is False:                         # model explicitly declined
            return {"ok": False, "reason": str(data.get("reason") or "vision could not determine it")}
        missing = [k for k in required if data.get(k) in (None, "")]
        if missing:
            last = "vision response missing fields: " + ", ".join(missing); continue
        return {"ok": True, "data": data, "model": info}
    return {"ok": False, "reason": last}


def _clamp01(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


def _marked_image(img, x: int, y: int):
    """Return a copy of img with a visible marker at x/y for vision self-check."""
    from PIL import ImageDraw

    marked = img.copy()
    draw = ImageDraw.Draw(marked)
    radius = max(14, min(img.width, img.height) // 70)
    color = (255, 0, 0)
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color, width=5)
    draw.line((x - radius * 2, y, x + radius * 2, y), fill=color, width=4)
    draw.line((x, y - radius * 2, x, y + radius * 2), fill=color, width=4)
    return marked


def _verify_click_point(base, key, model, img, target: str, x: int, y: int):
    marked = _marked_image(img, x, y)
    task = (
        "The screenshot has a red circle/cross marker. Verify whether the CENTER of that marker "
        f"is on the exact UI target the user requested: {target}.\n"
        "For browser tabs, the marker must be on the tab strip/tab header itself, not the page body, "
        "not the address bar, and not a different tab. If the target mentions an ordinal such as "
        "second/third, count matching visible candidates from left to right within the relevant tab strip. "
        "If the target mentions nearby text or a number, use it as a landmark.\n"
        "Respond only with JSON."
    )
    res = _ask_json(
        base,
        key,
        model,
        task,
        marked,
        {"target_hit": "<bool>", "confidence": "<float 0..1>", "reason": "<short string>"},
        max_tokens=160,
    )
    if not res.get("ok"):
        return False, 0.0, str(res.get("reason") or "point verification failed")
    data = res.get("data") or {}
    hit = bool(data.get("target_hit"))
    conf = _clamp01(data.get("confidence"))
    reason = str(data.get("reason") or "")
    return hit and conf >= 0.65, conf, reason


def describe(settings, question: str | None):
    """Look at the screen and return the model's answer. Description is natural PROSE (VL models
    answer that way), wrapped in the {ok,data}|{ok,reason} envelope with an explicit failure
    channel so the brain can replan. (Strict JSON is only used where it makes sense — coords.)"""
    try:
        img, _, _ = capture_primary(1400)   # moderate res: enough to read, light on tokens
    except Exception as e:
        return {"ok": False, "reason": f"screen capture failed: {e}"}
    base, key = lm_endpoint(settings)
    model = resolve_vision_model(settings, base, key)
    if not model:
        return {"ok": False, "reason": "no vision model available (load one or set it in VISION)"}
    task = (f'Look at the screenshot and answer concisely: "{question}".' if question
            else "Look at the screenshot and describe what is on the screen: the active app/window, "
                 "key text, and notable UI elements.")
    # Force a clean, final answer — this VL model otherwise emits a numbered "Thinking Process".
    task += (" Answer in 2 to 4 plain sentences. Output ONLY the description itself — no headings, "
             "no numbered steps, no 'Thinking Process', no analysis, no markdown. /no_think")
    reason = "vision returned an empty description"
    # Retry a few times; shrink the image each time in case the payload size is the problem.
    for w in (1100, 900, 700):
        try:
            shot, _, _ = capture_primary(w)
        except Exception:
            shot = img
        txt, info = _chat_image(base, key, model, task, shot, max_tokens=700, temperature=0.2)
        if txt is None:
            reason = info; continue
        if txt.strip():
            return {"ok": True, "data": {"answer": txt.strip()}, "model": info}
    return {"ok": False, "reason": reason}


def locate(settings, target: str):
    """Ask the GROUNDING model where to click for `target`, via the validated contract.
    Returns ((sx, sy), info) or (None, reason)."""
    try:
        img, scale, mon = capture_primary()   # native res for precise coordinates
    except Exception as e:
        return None, f"screen capture failed: {e}"
    W, H = img.size
    base, key = grounding_endpoint(settings)
    model = getattr(settings.models, "grounding_model", "") or resolve_vision_model(settings, base, key)
    task = (
        f"This is a {W}x{H} whole-screen screenshot. Find the exact visible UI target to click: {target}.\n"
        "Rules:\n"
        "- Only return a coordinate if the target is visible in this screenshot.\n"
        "- For browser tabs, click the tab header in the top tab strip, preferably the center of the tab title/fav icon area.\n"
        "- Do not click page content, videos, the address bar, search boxes, or a different tab.\n"
        "- If the target says first/second/third/last, count matching visible candidates left-to-right within the relevant area.\n"
        "- If the target mentions nearby text, a number, or a side of the screen, use that as a landmark.\n"
        "- If unsure, return ok=false with a short reason instead of guessing.\n"
    )
    res = _ask_json(
        base,
        key,
        model,
        task,
        img,
        {
            "target_visible": "<bool>",
            "x": "<int>",
            "y": "<int>",
            "confidence": "<float 0..1>",
            "description": "<short string>",
        },
        max_tokens=220,
    )
    if not res["ok"]:
        return None, res["reason"]
    if not bool(res["data"].get("target_visible")):
        return None, str(res["data"].get("description") or "vision did not mark the target visible")
    confidence = _clamp01(res["data"].get("confidence"))
    if confidence < 0.55:
        return None, f"vision confidence too low ({confidence:.2f}) for target: {target}"
    try:
        x, y = int(res["data"]["x"]), int(res["data"]["y"])
    except Exception:
        return None, "vision returned non-numeric coordinates"
    if not (0 <= x < W and 0 <= y < H):
        return None, f"vision returned coordinates outside screenshot bounds: {x},{y}"
    verified, verify_conf, verify_reason = _verify_click_point(base, key, model, img, target, x, y)
    if not verified:
        return None, f"vision point verification failed ({verify_conf:.2f}): {verify_reason}"
    sx, sy = int(x * scale) + int(mon.get("left", 0)), int(y * scale) + int(mon.get("top", 0))
    info = {
        "model": res.get("model"),
        "confidence": confidence,
        "verification_confidence": verify_conf,
        "description": str(res["data"].get("description") or ""),
    }
    return (sx, sy), info
