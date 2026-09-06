"""Choosing which microphone to listen on and which speaker to answer through.

There is no single right answer, because the desk changes. A laptop lid holds
its microphone a hand's width from the mouth; a display holds one an arm's
length away and correspondingly quieter; earbuds hold one against the cheek.
The same sentence, recorded on this machine, transcribed as twenty-three words
through earbuds and as *"the weather."* through the display — nothing else
about the system differed.

So the choice is made per session, from the devices the system is actually
using, and the echo canceller is switched in or out with it:

  speakers      sound goes into the room, comes back into the microphone, and
                the assistant answers itself unless it is cancelled. Route both
                ends through the canceller.

  headphones    nothing reaches the room, so there is no echo to cancel — and
                the canceller's noise suppression, which costs signal, is pure
                loss here. Use the plain default devices.

Whether headphones are on is read from the sink the system chose: its active
port, form factor and icon are independent pieces of evidence. Bluetooth alone
is not evidence — speakers, soundbars and car audio use the same BlueZ sink
names as headphones. Anything unrecognised is assumed to put sound into the
room, because an unnecessary canceller costs a little signal, while a missing
one makes the assistant talk to itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import re
import signal
import stat
from dataclasses import dataclass

log = logging.getLogger(__name__)

# The nodes created by pipewire/99-omavoice-echo-cancel.conf.
AEC_SOURCE = "echo-cancel-source"
AEC_SINK = "omavoice_playback"

_WORN_HINTS = ("headphone", "headset", "earbud")
_WORN_FORM_FACTORS = frozenset(("headphone", "headset"))
_OPEN_PORT_HINTS = ("speaker", "handsfree", "hands-free", "car", "tv")


@dataclass(frozen=True)
class Devices:
    """What this session will record from and play into, and why."""

    input_target: str
    output_target: str
    headphones: bool
    reason: str
    # A short line meant for the settings window rather than the log.
    summary: str = ""
    # Where the microphone should go if the chosen one never speaks. A headset
    # microphone node exists only while its card is in headset profile, and
    # opening it is what asks for the switch — so the first attempt can lose
    # that race, and a device can also disappear mid-session.
    fallback_input: str = ""
    # Full duplex on speakers is safe only when both live AEC nodes form the
    # actual route. A node name or a warning alone is not echo protection.
    echo_cancelled: bool = False

    def describe(self) -> str:
        return f"in={self.input_target} out={self.output_target} ({self.reason})"


@dataclass(frozen=True)
class _SinkFacts:
    """The small, semantic part of a verbose `pactl list sinks` entry."""

    port: str = ""
    form_factor: str = ""
    icon_name: str = ""


def short_label(name: str, description: str) -> str:
    """A name that fits in a settings window and still says which microphone.

    PulseAudio descriptions are written for a device tree, not for a person
    choosing between them: "Alder Lake PCH-P High Definition Audio Controller
    Digital Microphone" is sixty-eight characters of which four matter. The
    chipset is not the choice; where the microphone sits is.
    """
    if name.startswith("bluez_input."):
        return description or "Bluetooth headset"
    if name == AEC_SOURCE:
        return "Echo canceller"
    label = re.sub(r"^.*High Definition Audio Controller\s*", "Laptop ", description)
    label = re.sub(r"\s*(Microphone|Mono)$", "", label).strip()
    return label or name


# --- running the system's audio tools -------------------------------------
#
# pactl, pw-record, pw-play and setpriv are distribution binaries and live where
# the distribution puts them. They are deliberately not looked up on PATH. The
# systemd unit captures the user's shell PATH at install time — it has to,
# because codex and claude live in version-manager directories that a bare
# systemd PATH does not have — and those directories stay writable by the user
# for the life of the machine. Anything that lands in one of them earlier on
# that PATH would be what the daemon runs with the microphone open. That
# reasoning covers the agent CLIs and nothing else; the four tools below are
# taken from the system directories or not run at all.
#
# On this machine (Arch, usr-merged) all four are in /usr/bin, shipped by
# libpulse, pipewire-audio and util-linux. /bin, /sbin and /usr/sbin are
# symlinks to it here and are listed for distributions where they are not.
_TRUSTED_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# How long a helper gets to notice SIGTERM before it is killed outright. These
# are stateless readers of the PipeWire graph with nothing to flush; the two
# seconds pw-record and pw-play are given exist so a Bluetooth transport can be
# released cleanly, and pactl has no such obligation.
_HELPER_GRACE = 0.5
# After SIGKILL the only wait is the kernel's. The second is there so a wedged
# uninterruptible sleep cannot hang the daemon along with it.
_KILL_GRACE = 1.0

# `pactl list sources` prints 23 KB for the eight devices on this machine, a
# shade under 3 KB each; the short forms are under a kilobyte. A quarter of a
# megabyte holds ninety devices, which is well past any real desk, and stops a
# pactl that has started talking and does not intend to finish.
_PACTL_MAX_BYTES = 256 * 1024
# The slowest of these calls measured 7.2 ms. Three seconds is not a
# performance budget; it is the point at which pactl is presumed wedged.
_PACTL_TIMEOUT = 3.0


class TrustedBinaryMissing(OSError):
    """A tool is not present as an executable in any trusted directory.

    An OSError because every caller here already treats "the helper will not
    start" that way, and a tool that is missing and a tool that refuses to exec
    deserve the same handling: say so, and do without.
    """


@functools.lru_cache(maxsize=None)
def trusted_binary(name: str) -> str:
    """The absolute path of a system tool, or refuse to run it at all.

    Cached because the answer only changes when packages are installed, and a
    daemon that is running through that wants restarting anyway.
    """
    for directory in _TRUSTED_BIN_DIRS:
        candidate = os.path.join(directory, name)
        try:
            # Follows symlinks on purpose: pw-record and pw-play are both links
            # to pw-cat, and it is the target that has to be a real program.
            info = os.stat(candidate)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.X_OK):
            continue
        # And the link may not lead out of these directories, which would hand
        # the choice back to whoever can write wherever it points.
        if os.path.dirname(os.path.realpath(candidate)) not in _TRUSTED_BIN_DIRS:
            continue
        return candidate
    raise TrustedBinaryMissing(
        f"{name} is not an executable in any of {', '.join(_TRUSTED_BIN_DIRS)} — "
        "install it from the distribution; PATH is deliberately not consulted"
    )


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal a helper and anything it started.

    Every helper here is spawned with `start_new_session=True`, so it leads a
    process group of its own and the group can be signalled whole — a helper
    that forks does not get to leave the child behind. If for any reason it is
    not a group leader, only the process itself is signalled: its group would
    then be the daemon's own, and killing that is worse than a stray child.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)


async def terminate_and_reap(
    proc: asyncio.subprocess.Process, grace: float = _HELPER_GRACE
) -> int | None:
    """End a helper and collect its exit status. Never leaves it running.

    Returning while a `pw-record` still holds the microphone is the failure
    that matters here — the light stays on and the next session opens a second
    capture — but an abandoned `pactl` is the same bug in slower motion.
    """
    if proc.returncode is not None:
        return proc.returncode
    _signal_group(proc, signal.SIGTERM)
    try:
        return await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        pass
    _signal_group(proc, signal.SIGKILL)
    try:
        return await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE)
    except asyncio.TimeoutError:
        log.error("helper pid %d survived SIGKILL", proc.pid)
        return proc.returncode


async def read_capped(
    stream: asyncio.StreamReader, limit: int, drain_rest: bool = False
) -> tuple[bytes, int]:
    """Read at most `limit` bytes. Returns what was kept and what was dropped.

    `drain_rest` is the difference between the two kinds of producer here. A
    one-shot query is finished with once the cap is hit and gets killed. A
    long-lived one is not: stop reading its pipe and it blocks on the next
    write, and for pw-record that stalls the microphone rather than the log, so
    the overflow is read and thrown away instead of left to back up.
    """
    kept = bytearray()
    dropped = 0
    while True:
        block = await stream.read(65536)
        if not block:
            break
        room = limit - len(kept)
        if room > 0:
            kept += block[:room]
        dropped += max(0, len(block) - max(0, room))
        if len(kept) >= limit and not drain_rest:
            break
    return bytes(kept), dropped


async def _pactl(*args: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            trusted_binary("pactl"), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Its own process group, so the cleanup below reaches whatever it
            # may have started and not only pactl itself.
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("pactl %s did not run: %s", " ".join(args), exc)
        return ""
    assert proc.stdout is not None
    try:
        out, _ = await asyncio.wait_for(
            read_capped(proc.stdout, _PACTL_MAX_BYTES), timeout=_PACTL_TIMEOUT
        )
        if len(out) >= _PACTL_MAX_BYTES:
            log.warning(
                "pactl %s went past %d bytes — discarding it", " ".join(args), _PACTL_MAX_BYTES
            )
            out = b""
    except (asyncio.TimeoutError, OSError):
        log.warning("pactl %s did not finish in %.0fs", " ".join(args), _PACTL_TIMEOUT)
        out = b""
    finally:
        # Whatever happened above, pactl does not outlive this call. On timeout
        # the old code simply returned and left it running.
        await terminate_and_reap(proc)
    # An answer that arrived in part is not an answer: half of `list sources`
    # is a device list missing devices, and nothing downstream can tell that
    # from a machine that genuinely has none. Callers already handle "".
    return out.decode(errors="replace").strip()


async def _default_sink() -> str:
    return await _pactl("get-default-sink")


async def _default_source() -> str:
    return await _pactl("get-default-source")


async def _sink_facts(sink: str) -> _SinkFacts:
    """Read the evidence that says whether output stays out of the room.

    PipeWire's native spelling uses hyphens while its PulseAudio compatibility
    layer commonly prints underscores. Accept both; a hardware-dependent
    spelling difference must never disable echo protection.
    """
    if not sink:
        return _SinkFacts()
    text = await _pactl("list", "sinks")
    current = ""
    port = ""
    properties: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name: "):
            current = stripped[6:]
            continue
        if current != sink:
            continue
        if stripped.startswith("Active Port: "):
            port = stripped[13:].strip().lower()
            continue
        match = re.fullmatch(r"([a-zA-Z0-9_.-]+)\s*=\s*(.*)", stripped)
        if not match:
            continue
        key, value = match.groups()
        key = key.replace("_", "-").lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        properties[key] = value.strip().lower()
    return _SinkFacts(
        port=port,
        form_factor=properties.get("device.form-factor", ""),
        icon_name=properties.get("device.icon-name", ""),
    )


def _is_worn(sink: str, facts: _SinkFacts) -> tuple[bool, str]:
    """Does sound from this sink stay out of the room?

    A positive answer needs explicit headphone evidence. `hands-free` is not
    enough: speakerphones and car kits expose that form factor too. A known
    non-headphone form factor wins over an icon, because it describes the
    physical device while icons are presentation hints and can be stale.
    """
    # Some Bluetooth profiles call their port `headset-output-handsfree` even
    # when the hardware is a speakerphone. The open-room word wins; the more
    # generic `headset` substring must not grant an echo-protection exemption.
    for hint in _OPEN_PORT_HINTS:
        if hint in facts.port:
            return False, f"open-output sink port {facts.port!r}"

    for hint in _WORN_HINTS:
        if hint in facts.port:
            return True, f"sink port {facts.port!r}"

    if facts.form_factor:
        if facts.form_factor in _WORN_FORM_FACTORS:
            return True, f"device form factor {facts.form_factor!r}"
        return False, f"device form factor {facts.form_factor!r}"

    for hint in _WORN_HINTS:
        if hint in facts.icon_name:
            return True, f"device icon {facts.icon_name!r}"

    if sink.startswith("bluez_output"):
        return False, "bluetooth output without headphone evidence"
    if facts.port:
        return False, f"sink port {facts.port!r}"
    return False, "no headphone evidence reported"


async def node_exists(name: str) -> bool:
    """Is this capture or playback node actually present right now?

    Worth asking out loud, because `pw-record --target` does not: handed a name
    that does not exist it records from the default source instead, cheerfully
    and without a word. A microphone that vanished therefore shows up as audio
    from the wrong device rather than as silence — which is the harder failure
    to notice and the easier one to misdiagnose.
    """
    if not name:
        return False
    text = await _pactl("list", "short", "sources")
    text += "\n" + await _pactl("list", "short", "sinks")
    return any(name == line.split("\t")[1] for line in text.splitlines() if "\t" in line)


def _bt_address(node: str) -> str:
    """The MAC out of a bluez node name, with separators normalised.

    Sinks and sources spell the same device differently — `bluez_output.C4_77_
    64_49_C0_D9.1` against `bluez_input.C4:77:64:49:C0:D9` — so neither can be
    matched against the other without this.
    """
    for prefix in ("bluez_output.", "bluez_input."):
        if node.startswith(prefix):
            rest = node[len(prefix) :]
            rest = rest.split(".")[0]
            return rest.replace(":", "_").upper()
    return ""


async def _headset_mic_for(sink: str) -> str:
    """The microphone belonging to the same headset as this sink, if it has one.

    Worth going out of the way for. The system's default source is whatever it
    was before the headphones went on — here, a microphone in a display an
    arm's length away, which transcribed the same sentence as *"the weather."*
    against twenty-three words from the earbuds. The right microphone when
    someone is wearing headphones is almost always the one they are wearing.

    The node is listed even while the card is in A2DP and has no microphone;
    opening it is what makes WirePlumber switch the card to its headset
    profile, and releasing it switches back.
    """
    address = _bt_address(sink)
    if not address:
        return ""
    text = await _pactl("list", "short", "sources")
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        if name.startswith("bluez_input.") and _bt_address(name) == address:
            return name
    return ""


async def resolve(
    configured_input: str,
    configured_output: str,
    avoid: set[str] | None = None,
) -> Devices:
    """Pick devices for one session.

    An explicit, present `OMAVOICE_INPUT` / `OMAVOICE_OUTPUT` wins: someone who
    named a device meant it. Routes that bypass AEC still need microphone
    suppression during speaker playback; their metadata makes that explicit.

    `avoid` holds microphones that already failed to produce audio while this
    daemon has been running. A Bluetooth headset whose HFP transport is broken
    fails the same way every time, and rediscovering that at the start of every
    conversation costs the person ten seconds of talking to nothing.
    """
    avoid = avoid or set()

    # A device named by hand can be gone — a dock unplugged, a headset off.
    # Saying so is the whole point: pw-record would take the name, ignore it,
    # and record from something else without telling anyone.
    if configured_input and not await node_exists(configured_input):
        log.warning(
            "the chosen microphone %r is not present — falling back to whatever "
            "the system is using, which is not the same device",
            configured_input,
        )
        configured_input = ""

    if configured_output and not await node_exists(configured_output):
        log.warning(
            "the chosen output %r is not present — falling back to the system "
            "output and checking its echo protection",
            configured_output,
        )
        configured_output = ""

    # Classify what will play, not an unrelated system default. Otherwise an
    # explicit speaker output can inherit the default headphones' exemption.
    sink = configured_output or await _default_sink()
    facts = await _sink_facts(sink)
    worn, why = _is_worn(sink, facts)

    if worn:
        own_mic = await _headset_mic_for(sink)
        if own_mic in avoid:
            log.info("skipping %s — it failed earlier in this session", own_mic)
            own_mic = ""
        source = own_mic or await _default_source()
        detail = "its own microphone" if own_mic else "system default microphone"
        chosen = configured_input or source
        return Devices(
            chosen,
            configured_output or sink,
            True,
            f"headphones — {why}, {detail}, echo canceller not needed",
            f"{await _describe_source(chosen)} · headphones, echo cancellation off",
            await _default_source(),
        )

    # An orphan source does not establish a reference path. pw-play can fall
    # back to the physical default when its target is missing, so verify both.
    source_exists, sink_exists = await asyncio.gather(
        node_exists(AEC_SOURCE), node_exists(AEC_SINK)
    )
    aec_ready = source_exists and sink_exists
    fallback = ""
    if configured_input and configured_output:
        chosen, output = configured_input, configured_output
        detail = "explicit route does not form the echo canceller pair"
    elif aec_ready and (not configured_output or configured_output == AEC_SINK):
        chosen = configured_input or AEC_SOURCE
        if chosen != AEC_SOURCE:
            default = await _default_source()
            if chosen == default:
                log.info(
                    "%s is what the echo canceller is capturing — listening through "
                    "it instead, so the same microphone arrives without the echo",
                    chosen,
                )
                chosen = AEC_SOURCE
            else:
                fallback = AEC_SOURCE
        output = AEC_SINK if chosen == AEC_SOURCE else sink
        detail = "chosen microphone is outside the canceller"
    else:
        chosen = configured_input or await _default_source()
        output = sink
        detail = (
            "echo canceller source/sink pair is not loaded"
            if not aec_ready
            else "chosen output bypasses the echo canceller"
        )

    echo_cancelled = aec_ready and chosen == AEC_SOURCE and output == AEC_SINK
    if echo_cancelled:
        return Devices(
            chosen,
            output,
            False,
            f"speakers — {why}, routed through the echo canceller",
            "Speakers · echo cancellation on",
            # Switching just the input would break the verified pair.
            fallback_input="",
            echo_cancelled=True,
        )

    log.warning(
        "%s — using half duplex: microphone suppressed during playback; "
        "voice interruption unavailable (in=%s out=%s)",
        detail,
        chosen,
        output,
    )
    return Devices(
        chosen,
        output,
        False,
        f"speakers — {why}, {detail}; half duplex",
        f"{await _describe_source(chosen)} · half duplex (voice interruption unavailable)",
        fallback_input=fallback,
    )

async def _describe_source(name: str) -> str:
    for source in await list_sources():
        if source["name"] == name:
            return source["label"]
    return short_label(name, "")


async def list_sources() -> list[dict]:
    """Every capture device, named the way a person would pick it.

    Monitors are left out: they record what is being played, which is useful to
    a debugging tool and never what anyone means by "microphone".
    """
    text = await _pactl("list", "sources")
    out: list[dict] = []
    current: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name: "):
            current = {"name": stripped[6:]}
        elif stripped.startswith("Description: ") and current.get("name"):
            name = current["name"]
            description = stripped[13:]
            if not name.endswith(".monitor"):
                out.append(
                    {
                        "name": name,
                        "description": description,
                        "label": short_label(name, description),
                    }
                )
            current = {}
    return out
