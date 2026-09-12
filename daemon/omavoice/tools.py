"""Safe local tools for the voice assistant's brain (groq backend).

The brain is a cloud LLM; its "hands" live here. Two safety classes:

  * INSTANT tools — read-only status or trivially reversible actions that
    never need a confirmation: clock, battery/memory status, volume up/down/
    mute, media keys, reminders. A mis-heard phrase can at worst set the
    volume wrong; the person fixes it by hand in a second.
  * CONFIRM tools — anything that opens a window, spends focus or otherwise
    changes the desktop in a visible-but-not-trivially-reversible way:
    launching apps and opening URLs. These surface a confirmation dialog
    in the panel (Enter = run, Esc = decline) before anything executes.

There is deliberately NO tool that writes files, runs shell, or touches the
network beyond launching the browser at an https/http URL. The STT error
rate turns "anything executable" into a slot machine, so the ceiling is
what a wrong confirmation would cost: one window opening by mistake.

Tool results return short Russian strings fit for speech — the caller
speaks them out loud.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = __import__("logging").getLogger("omavoice.tools")

_GROQ_TOOLS: list[dict] = []
_INSTANT: dict[str, "Tool"] = {}
_CONFIRM: dict[str, "Tool"] = {}


@dataclass
class Tool:
    name: str
    desc: str
    args: dict            # JSON-schema-ish, minimal
    instant: bool         # True = run without asking
    run: "Callable[[dict], Awaitable[str]]"


def _tool(name, desc, args, instant):
    def deco(fn):
        t = Tool(name=name, desc=desc, args=args, instant=instant, run=fn)
        spec = {"type": "function", "function": {
            "name": name, "description": desc, "parameters": args}}
        _GROQ_TOOLS.append(spec)
        (_INSTANT if instant else _CONFIRM)[name] = t
        return fn
    return deco


async def _run(cmd: list[str], timeout: float = 5.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "(timeout)"
    return (out or b"").decode(errors="replace").strip()


# --- instant: clock & status -------------------------------------------------

@_tool("clock", "Current date and time on the user's machine.",
       {"type": "object", "properties": {}}, instant=True)
async def _clock(payload: dict) -> str:
    out = await _run(["date", "+%A, %d %B, %H:%M"])
    # LC_TIME is English on this box; map the weekday for speech anyway.
    en2ru = {"Monday": "понедельник", "Tuesday": "вторник", "Wednesday": "среда",
             "Thursday": "четверг", "Friday": "пятница", "Saturday": "суббота",
             "Sunday": "воскресенье", "January": "января", "February": "февраля",
             "March": "марта", "April": "апреля", "May": "мая", "June": "июня",
             "July": "июля", "August": "августа", "September": "сентября",
             "October": "октября", "November": "ноября", "December": "декабря"}
    for en, ru in en2ru.items():
        out = out.replace(en, ru)
    return out or "не удалось узнать время"


@_tool("status", "Brief system status: memory, battery, uptime.",
       {"type": "object", "properties": {}}, instant=True)
async def _status(payload: dict) -> str:
    mem = await _run(["free", "-h", "--si"])
    line = ""
    for l in mem.splitlines():
        if l.startswith("Mem:"):
            line = " ".join(l.split())
    up = await _run(["uptime", "-p"])
    bat = await _run(
        ["upower", "-i", "/org/freedesktop/UPower/devices/battery_BAT0"])
    pct = re.search(r"percentage:\s*(\d+)%", bat or "")
    state = re.search(r"state:\s*(\w+)", bat or "")
    parts = []
    if up:
        parts.append(f"аптайм {up.replace('up ', '')}")
    if pct:
        s = f"батарея {pct.group(1)} процентов"
        if state and state.group(1) != "unknown":
            s += f" ({state.group(1)})"
        parts.append(s)
    if line:
        parts.append(line)
    return "; ".join(parts) or "статус недоступен"


# --- instant: volume ----------------------------------------------------------

def _volume(delta_pct: int | None, mute: bool | None) -> list[str]:
    if mute is True:
        return ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"]
    if mute is False:
        return ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"]
    return ["pactl", "set-sink-volume", "@DEFAULT_SINK@",
            f"{delta_pct:+d}%"]


@_tool("volume", "Change volume. step: percent, positive=louder negative=quieter (-30..30, default 10). mute: true/false.",
       {"type": "object",
        "properties": {"step": {"type": "integer"},
                       "mute": {"type": "boolean"}}, }, instant=True)
async def _volume_tool(payload: dict) -> str:
    if "mute" in payload:
        await _run(_volume(None, bool(payload["mute"])))
        return "звук выключен" if payload["mute"] else "звук включён"
    step = int(payload.get("step") or 10)
    step = max(-30, min(30, step))
    if step == 0:
        return "громкость не изменилась"
    await _run(_volume(step, None))
    return f"громкость {'+' if step > 0 else ''}{step} процентов"


@_tool("media", "Media keys: play, pause, next, prev.",
       {"type": "object",
        "properties": {"action": {"type": "string",
                                  "enum": ["play", "pause", "next", "prev"]}},
        "required": ["action"]}, instant=True)
async def _media(payload: dict) -> str:
    action = str(payload.get("action") or "")
    mapping = {"play": "play", "pause": "pause", "next": "next", "prev": "previous"}
    if action not in mapping:
        return "неизвестное действие"
    await _run(["playerctl", "status"], timeout=3)
    await _run(["playerctl", mapping[action]], timeout=3)
    return f"медиа: {action}"


# --- instant: reminders -------------------------------------------------------

@_tool("reminder", "Set a desktop-notification reminder. minutes: in how many minutes (1..600). text: about what.",
       {"type": "object",
        "properties": {"minutes": {"type": "integer"},
                       "text": {"type": "string"}},
        "required": ["minutes", "text"]}, instant=True)
async def _reminder(payload: dict) -> str:
    minutes = int(payload.get("minutes") or 0)
    text = str(payload.get("text") or "").strip()
    if not (1 <= minutes <= 600) or not text:
        return "нужны минуты и текст напоминания"
    safe_text = " ".join(text.split())[:120]
    out = await _run(["omarchy", "reminder", str(minutes), safe_text], timeout=8)
    return f"напомню через {minutes} минут: {safe_text}"


# --- confirm: launching -------------------------------------------------------

_ALLOWED_SCHEMES = ("http://", "https://")
_SITE_ALIASES = {
    "ютуб": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "гитхаб": "https://github.com",
    "github": "https://github.com",
    "почта": "https://mail.google.com",
    "вкидке": "https://vk.com",
    "вк": "https://vk.com",
    "телеграм": "https://web.telegram.org",
}


def _resolve_url(raw: str) -> str | None:
    """Map a loose spoken target to a URL we are willing to open."""
    t = (raw or "").strip().lower()
    if not t:
        return None
    for alias, url in _SITE_ALIASES.items():
        if t == alias or t.startswith(alias + " "):
            rest = t[len(alias):].strip()
            return url if not rest else url + "/" + rest.lstrip("/")
    if t.startswith(_ALLOWED_SCHEMES):
        return t
    if "." in t and " " not in t and re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/.*)?", t):
        return "https://" + t
    return None


@_tool("open_app", "Open an app or website. target: app name (browser, files) or URL/site name (youtube.com, ютуб, гитхаб).",
       {"type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"]}, instant=False)
async def _open_app(payload: dict) -> str:
    target = str(payload.get("target") or "").strip()
    if not target:
        return "что открыть?"
    t = target.lower()
    # apps first — a bare word the model already resolved
    if t in ("browser", "браузер"):
        await _run(["omarchy", "launch", "browser"], timeout=8)
        return "открываю браузер"
    if t in ("files", "nautilus", "файлы"):
        await _run(["omarchy", "launch", "nautilus"], timeout=8)
        return "открываю файлы"
    url = _resolve_url(target)
    if url:
        await _run(["omarchy", "launch", "browser", url], timeout=8)
        return f"открываю {url}"
    return f"не знаю, как открыть {target!r}"


def groq_tools() -> list[dict]:
    return _GROQ_TOOLS


def lookup(name: str) -> Tool | None:
    return _INSTANT.get(name) or _CONFIRM.get(name)


def classify(name: str) -> str | None:
    t = lookup(name)
    if t is None:
        return None
    return "instant" if t.instant else "confirm"
