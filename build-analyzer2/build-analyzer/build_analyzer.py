#!/usr/bin/env python3
"""
build_analyzer.py

Static, read-only analysis of a shipped Unity WebGL build or a generic
WebGL/HTML5 program, producing coding-ready evidence for interaction analysis.

Design constraints:
  1. Every finding carries provenance (source file, byte offset or line number,
     and the detector that produced it) so a claim in a paper can be traced back
     to the artifact.
  2. Nothing is inferred that is not observed. Detectors report presence of
     signatures, not conclusions about design intent.
  3. Category labels are deliberately theory-neutral (input.*, feedback.*,
     guidance.*). Map them to a coding scheme with --scheme (see README).
  4. Degrades gracefully. Without UnityPy or brotli it still inventories the
     build, unpacks UnityWebData, and extracts strings.

Usage:
  python3 build_analyzer.py <path-to-build-dir-or-file> -o <outdir> [--scheme s.json]

Outputs in <outdir>:
  report.md          human-readable report
  findings.json      every finding with provenance
  text_corpus.csv    on-screen / embedded text, one row per string, ready to code
  components.csv     MonoScript class names and counts (Unity mode)
  manifest.json      file inventory with sizes and SHA-256 hashes

Tested against synthetic fixtures. Verify results against the build before
citing them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import struct
import sys
import zlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

TOOL_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Optional dependencies
# --------------------------------------------------------------------------

try:
    import UnityPy  # type: ignore
    HAVE_UNITYPY = True
except Exception:
    UnityPy = None  # type: ignore
    HAVE_UNITYPY = False

try:
    import brotli  # type: ignore
    HAVE_BROTLI = True
except Exception:
    try:
        import brotlicffi as brotli  # type: ignore
        HAVE_BROTLI = True
    except Exception:
        brotli = None  # type: ignore
        HAVE_BROTLI = False


# --------------------------------------------------------------------------
# Finding model
# --------------------------------------------------------------------------

@dataclass
class Finding:
    """One observation with full provenance."""
    category: str            # e.g. "input.xr_controller"
    detector: str            # which rule fired
    evidence: str            # the literal matched text, truncated
    source: str              # file path relative to the build root
    locator: str             # "offset:12345", "line:42", "pathid:-88421"
    context: str = ""        # optional surrounding context
    confidence: str = "observed"   # observed | heuristic

    def key(self) -> tuple:
        return (self.category, self.detector, self.evidence, self.source, self.locator)


@dataclass
class FileRecord:
    path: str
    size: int
    sha256: str
    kind: str = ""


# --------------------------------------------------------------------------
# Signature tables
# --------------------------------------------------------------------------
# Each entry: (detector_name, category, regex). Regexes run over extracted
# strings (Unity) or source text (web). Keep them specific enough that a hit is
# meaningful and general enough to survive minor version differences.

UNITY_SIGNATURES: list[tuple[str, str, str]] = [
    # XR / VR input
    ("xr_interaction_toolkit", "input.xr_controller", r"XR(Base|Direct|Ray|Socket|Grab|Simple)Interact(or|able)"),
    ("xr_toolkit_namespace", "input.xr_controller", r"UnityEngine\.XR\.Interaction\.Toolkit"),
    ("xr_controller_component", "input.xr_controller", r"\bXR(BaseC|C)ontroller\b|ActionBasedController|XRController"),
    ("xr_node_enum", "input.xr_controller", r"\bXRNode\b|InputDevices?\.GetDeviceAtXRNode"),
    ("openxr", "input.xr_controller", r"\bOpenXR\b|UnityEngine\.XR\.OpenXR"),
    ("oculus_sdk", "input.xr_controller", r"\bOVR(Input|Manager|CameraRig|Grabbable|Player)"),
    ("steamvr", "input.xr_controller", r"\bSteamVR(_\w+)?\b|Valve\.VR"),
    ("hand_tracking", "input.hand_tracking", r"\bHandTracking\b|OVRHand\b|XRHands?\b|UnityEngine\.XR\.Hands"),
    ("gaze_input", "input.gaze", r"\bGazeInteractor\b|\bEyeTracking\b|GazeProvider|\bXRGazeAssistance\b"),
    ("teleportation", "locomotion.teleport", r"Teleportation(Area|Anchor|Provider)|\bTeleportRequest\b"),
    ("continuous_move", "locomotion.continuous", r"ContinuousMoveProvider|ContinuousTurnProvider|SnapTurnProvider"),
    ("haptics", "feedback.haptic", r"SendHapticImpulse|\bHapticImpulsePlayer\b|OVRInput\.SetControllerVibration|\bImpulse(Duration|Amplitude)\b"),

    # Desktop / 2D input
    ("legacy_input_axis", "input.keyboard", r"Input\.GetAxis(Raw)?"),
    ("legacy_key", "input.keyboard", r"Input\.GetKey(Down|Up)?|\bKeyCode\b"),
    ("legacy_mouse", "input.pointer", r"Input\.GetMouseButton(Down|Up)?|Input\.mousePosition|\bmouseScrollDelta\b"),
    ("input_system", "input.generic", r"UnityEngine\.InputSystem|\bInputActionAsset\b|\bPlayerInput\b|InputActionReference"),
    ("input_system_keyboard", "input.keyboard", r"\bKeyboard\.current\b|<Keyboard>/"),
    ("input_system_mouse", "input.pointer", r"\bMouse\.current\b|<Mouse>/"),
    ("input_system_gamepad", "input.gamepad", r"\bGamepad\.current\b|<Gamepad>/|\bXInputController\b"),
    ("input_system_xr", "input.xr_controller", r"<XRController>/|\bXRController\{"),
    ("touch_input", "input.touch", r"Input\.touch(es|Count)|\bTouchscreen\.current\b|EnhancedTouch"),
    ("accelerometer", "input.accelerometer", r"Input\.acceleration|\bAccelerometer\.current\b|\bGyroscope\b"),
    ("microphone", "input.voice", r"\bMicrophone\.Start\b|UnityEngine\.Windows\.Speech|KeywordRecognizer"),

    # UI event plumbing
    ("event_system", "structure.ui_events", r"\bEventSystem\b|StandaloneInputModule|InputSystemUIInputModule|XRUIInputModule"),
    ("ui_selectable", "feedback.visual_ui", r"UnityEngine\.UI\.(Button|Toggle|Slider|Dropdown|Scrollbar|InputField)"),
    ("tmp_ui", "feedback.text", r"\bTMPro\b|TextMeshProUGUI|\bTMP_(Text|InputField|Dropdown)\b"),
    ("ui_text", "feedback.text", r"UnityEngine\.UI\.Text\b"),
    ("canvas", "structure.canvas", r"\bCanvasScaler\b|\bGraphicRaycaster\b|TrackedDeviceGraphicRaycaster"),
    ("pointer_handlers", "input.pointer", r"IPointer(Click|Enter|Exit|Down|Up)Handler|\bOnPointerClick\b"),
    ("drag_handlers", "input.pointer", r"IDragHandler|IBeginDragHandler|IEndDragHandler|IDropHandler|\bOnDrag\b"),

    # Feedback channels
    ("audio_source", "feedback.audio", r"\bAudioSource\b|PlayOneShot|\bAudioMixer\b"),
    ("animation", "feedback.animation", r"\bAnimator\b|SetTrigger|CrossFade|\bAnimationClip\b"),
    ("particles", "feedback.particle", r"ParticleSystem\b|\bVisualEffect\b"),
    ("tts_or_captions", "feedback.text", r"\bSubtitle\b|\bCaption\b|\bTextToSpeech\b"),

    # Networking and data
    ("web_request", "data.network", r"UnityWebRequest|\bWWWForm\b"),
    ("player_prefs", "data.local", r"\bPlayerPrefs\b"),
    ("json_util", "data.serialization", r"JsonUtility|\bNewtonsoft\.Json\b"),
    ("jslib_interop", "data.jsinterop", r"\bDllImport\b|\[?__Internal\]?|SendMessage\("),
]

WEB_SIGNATURES: list[tuple[str, str, str]] = [
    ("webxr_session", "input.xr_controller", r"navigator\.xr\b|requestSession\(|immersive-(vr|ar)"),
    ("webxr_input", "input.xr_controller", r"inputSources|XRInputSource|targetRaySpace|gripSpace"),
    ("webxr_hand", "input.hand_tracking", r"\bhand-tracking\b|XRHand\b"),
    ("gamepad_api", "input.gamepad", r"getGamepads\(|\bgamepadconnected\b"),
    ("vibration_api", "feedback.haptic", r"navigator\.vibrate\(|hapticActuators|pulse\("),
    ("pointer_events", "input.pointer", r"addEventListener\(\s*['\"](pointer(down|up|move|enter|leave)|click|dblclick|contextmenu|wheel|mouse(down|up|move))['\"]"),
    ("touch_events", "input.touch", r"addEventListener\(\s*['\"]touch(start|end|move|cancel)['\"]"),
    ("keyboard_events", "input.keyboard", r"addEventListener\(\s*['\"]key(down|up|press)['\"]"),
    ("pointer_lock", "input.pointer", r"requestPointerLock\(|pointerlockchange"),
    ("device_motion", "input.accelerometer", r"devicemotion|deviceorientation|DeviceMotionEvent"),
    ("speech_input", "input.voice", r"SpeechRecognition|webkitSpeechRecognition|getUserMedia\(\s*\{\s*audio"),
    ("webgl_context", "structure.renderer", r"getContext\(\s*['\"]webgl2?['\"]"),
    ("canvas2d_context", "structure.renderer", r"getContext\(\s*['\"]2d['\"]"),
    ("web_audio", "feedback.audio", r"\bAudioContext\b|webkitAudioContext|createBufferSource\("),
    ("html_audio", "feedback.audio", r"new Audio\(|<audio\b"),
    ("speech_synthesis", "feedback.audio", r"speechSynthesis|SpeechSynthesisUtterance"),
    ("aria_roles", "accessibility.aria", r"\baria-[a-z]+=|role=['\"](button|dialog|alert|status)"),
    ("focus_management", "accessibility.focus", r"\btabindex=|\.focus\(\)"),
    ("alt_text", "accessibility.alt", r"<img[^>]+alt="),
    ("fullscreen", "structure.presentation", r"requestFullscreen\(|webkitRequestFullscreen"),
    ("unity_loader", "engine.unity", r"createUnityInstance|UnityLoader\.instantiate|\.loader\.js"),
    ("emscripten", "engine.emscripten", r"\b_emscripten_|Module\[['\"]asm['\"]\]|wasmBinaryFile"),
    ("threejs", "engine.threejs", r"\bTHREE\.[A-Z]|three\.module\.js"),
    ("babylon", "engine.babylon", r"\bBABYLON\.[A-Z]|babylon(\.max)?\.js"),
    ("playcanvas", "engine.playcanvas", r"\bpc\.Application\b|playcanvas-stable"),
    ("phaser", "engine.phaser", r"\bPhaser\.(Game|Scene)\b"),
    ("godot", "engine.godot", r"godot\.engine|Godot\.js|\.pck\b"),
]

# --------------------------------------------------------------------------
# Procedural rhetoric lexicons
# --------------------------------------------------------------------------
# A build's rules are compiled to WebAssembly and cannot be read as logic. What
# survives readable is the vocabulary the developers used to name the rules, and
# the language the game uses to tell the player what happened. Both are evidence
# about the possibility space and the incentive structure. Neither is the rule
# itself. Everything produced from these tables is a candidate for the analyst
# to confirm by playing the build, and is labeled `heuristic` in the output.
#
# Categories follow the standard procedural-rhetoric questions: what is made
# possible, what is made impossible, what is bounded, what is rewarded, what is
# punished, what is priced, what is judged, and where agency is taken away.

RULE_LEXICON: list[tuple[str, str, str]] = [
    ("possibility.permission", r"^(Can|Is|Has|May)[A-Z]|^(Allow|Enable|Unlock|Activate|Grant|Permit|Open)[A-Z]|^Try[A-Z]|Available|Eligible|Usable|Interactable|Selectable",
     "names a condition under which the player is allowed to act"),
    ("possibility.prohibition", r"^(Cannot|Cant|Deny|Block|Prevent|Disable|Lock|Restrict|Forbid|Reject|Refuse|Ignore|Cancel|Abort|Veto)[A-Z]?|Invalid|Illegal|Forbidden|NotAllowed|Locked|Disabled|ReadOnly",
     "names a condition under which an action is refused"),
    ("possibility.requirement", r"^(Require|Need|Must|Demand|Prerequisite|Gate)[A-Z]?|Required|Needed|Prereq|Unlocks?With|KeyNeeded|MinimumTo",
     "names something the player must have or do first"),
    ("constraint.limit", r"^(Max|Min|Limit|Cap|Clamp|Quota|Budget|Threshold|Cooldown|Timeout|Timer|Countdown|Duration|Deadline)[A-Z]?|MaxAttempts|Remaining|Lives|Capacity|Stamina|Energy|Fuel|Charges|Uses(Left|Remaining)",
     "names a ceiling, floor, or clock bounding what the player can do"),
    ("reward.grant", r"^(Add|Gain|Earn|Award|Reward|Grant|Bonus|Increment|Boost|Praise|Celebrate)[A-Z]?|Score|Points?[A-Z_]|Streak|Combo|Multiplier|Achievement|Trophy|Medal|Star|Coin|Gem|Prize|Win|Victory|Success|Correct|Passed|Perfect",
     "names something given to the player for acting in a particular way"),
    ("punishment.penalty", r"^(Lose|Deduct|Subtract|Penal|Punish|Damage|Hurt|Fail|Miss|Break|Destroy|Kill|Reset|Restart|Revert|Expire|Decay)[A-Z]?|Penalty|GameOver|Wrong|Incorrect|Mistake|Error(Count|Rate)|Strike|Warning|Death|Defeat|Lost|Failed",
     "names something taken away or triggered against the player"),
    ("economy.exchange", r"^(Buy|Purchase|Sell|Spend|Pay|Trade|Exchange|Redeem|Refund)[A-Z]?|Cost|Price|Currency|Wallet|Balance|Shop|Store|Inventory|Transaction|Afford",
     "names a price, a purchase, or a holding the player trades with"),
    ("progression.state", r"^(Advance|Progress|Complete|Finish|Unlock|Next|Skip|Save|Load|Checkpoint)[A-Z]?|Level(Up|Complete|Index)?|Stage|Phase|Round|Turn|Chapter|Milestone|Tier|Rank|Unlocked",
     "names movement through the structure of the game"),
    ("evaluation.judgment", r"^(Validate|Verify|Evaluate|Judge|Assess|Grade|Compare|Match)[A-Z]|^Check[A-Z]|IsCorrect|IsRight|IsValid|Answer(Check|Correct|Is)?|Accuracy|Attempt(s)?Made|Correctness",
     "names the moment the system decides whether the player was right"),
    ("agency.removal", r"^(Freeze|Stun|Interrupt|Halt|ForceQuit|Kick|Eject|Autoplay|AutoAdvance|Cutscene)[A-Z]?|LockInput|DisableInput|BlockInput|IgnoreInput|NonInteractive|Uncontrollable",
     "names a point at which control is taken from the player"),
    ("assistance.scaffold", r"^(Hint|Help|Tip|Assist|Guide|Tutorial|Teach|Explain|Suggest|Reveal|Snap|Magnet|AutoAim|AutoComplete)[A-Z]?|Hints?(Left|Used|Count)?|Easy|Assisted|Forgiving|Tolerance|Leniency|GracePeriod",
     "names help the system extends to make an action easier"),
    ("repetition.retry", r"^(Retry|Redo|Repeat|Replay|Again|Undo|Rewind|Respawn|Restore)[A-Z]?|Attempt(s)?(Left|Remaining|Count)?|TriesLeft|SecondChance",
     "names whether and how a failed action can be attempted again"),
]

# Framework and runtime names that will otherwise flood the categories above.
# An identifier matching this is kept but flagged, not deleted, because a game
# type can legitimately be named `Level` or `Timer`.
FRAMEWORK_LIKELY = re.compile(
    r"^(System|Microsoft|Mono|Unity(Engine|Editor)?|TMPro|Newtonsoft|Internal|Interop|"
    r"Il2Cpp|Cecil|JetBrains|NUnit|Google|Firebase|Facebook|log4net|ICSharpCode)\b"
    r"|^(get|set|add|remove)_|^\.(cctor|ctor)$|^<[A-Za-z_<>]|"
    r"^(ToString|Equals|GetHashCode|Dispose|MoveNext|Invoke|BeginInvoke|EndInvoke|"
    r"CompareTo|GetEnumerator|GetType|Finalize|Clone|Reset|Current|Item|Count|Length|"
    r"Value|Key|Empty|Instance|Main|Awake|Start|Update|LateUpdate|FixedUpdate|OnEnable|"
    r"OnDisable|OnDestroy|OnGUI)$"
)

# Speech acts in player-visible strings. These are what the rules sound like
# from the player's side, which is the part a reader of the article can check.
SPEECH_ACT_LEXICON: list[tuple[str, str, str]] = [
    ("act.praise", r"\b(correct|right|nice|great|excellent|well done|good job|perfect|awesome|yes[!.]|you (win|won)|success|congratulations|amazing|brilliant)\b",
     "tells the player the action was right"),
    ("act.reprimand", r"\b(incorrect|wrong|not quite|nope|oops|sorry|failed|you lose|you lost|game over|missed|too slow|out of time|no[!.])\b",
     "tells the player the action was wrong"),
    ("act.prohibition", r"\b(you (can ?not|cannot|can't)|not allowed|unavailable|locked|blocked|denied|insufficient|not enough|too far|out of (reach|range|bounds)|invalid)\b",
     "tells the player an action is refused"),
    ("act.requirement", r"\b(you need|you must|requires?|first (collect|find|complete|unlock)|unlock(s|ed)? (at|by|with)|reach level|earn \d)\b",
     "tells the player what is needed before an action is possible"),
    ("act.tally", r"(\+ ?\d|\- ?\d|\b(score|points|coins|gems|stars|lives|streak|combo|multiplier|bonus|attempts|tries)\b\s*[:=]|\b(score|points|coins|stars|lives|total)\b\s*[:=]?\s*[\{\d]|\byou (earned|scored|collected|gained|lost)\b)",
     "reports a quantity the system is keeping"),
    ("act.deadline", r"\b(time (left|remaining|is up)|seconds? (left|remaining)|hurry|quick|before the timer|countdown|running out)\b",
     "tells the player a clock is running"),
    ("act.retry", r"\b(try again|once more|another (try|attempt)|retry|play again|restart|next time|keep trying)\b",
     "tells the player the action can be repeated"),
    ("act.instruction", r"\b(press|click|tap|hold|drag|grab|point|aim|look|select|choose|type|enter|move|use|place)\b",
     "tells the player which action to take"),
    ("act.encouragement", r"\b(you can do (it|this)|almost|close|getting there|nearly|keep going|don't give up|nice try)\b",
     "softens a failure without reporting an outcome"),
]


@dataclass
class RuleRow:
    """One identifier from the metadata name table, classified."""
    identifier: str
    category: str
    gloss: str
    ordinal: int
    source: str
    framework_likely: bool


@dataclass
class SpeechActRow:
    """One player-visible string, classified by what it does to the player."""
    text: str
    category: str
    gloss: str
    matched: str
    source: str
    locator: str


_RULE_RULES = [(cat, re.compile(pat), gloss) for cat, pat, gloss in RULE_LEXICON]
_ACT_RULES = [(cat, re.compile(pat, re.IGNORECASE), gloss) for cat, pat, gloss in SPEECH_ACT_LEXICON]


def classify_identifier(ordinal: int, name: str, source: str) -> list[RuleRow]:
    if len(name) < 4 or len(name) > 120:
        return []
    fw = bool(FRAMEWORK_LIKELY.search(name))
    out = []
    for cat, rx, gloss in _RULE_RULES:
        if rx.search(name):
            out.append(RuleRow(identifier=name, category=cat, gloss=gloss,
                               ordinal=ordinal, source=source, framework_likely=fw))
    return out


def classify_speech_act(text: str, source: str, locator: str) -> list[SpeechActRow]:
    out = []
    for cat, rx, gloss in _ACT_RULES:
        m = rx.search(text)
        if m:
            out.append(SpeechActRow(text=text, category=cat, gloss=gloss,
                                    matched=m.group(0), source=source, locator=locator))
    return out


# --------------------------------------------------------------------------
# Guidance-text lexicon
# --------------------------------------------------------------------------
# Words that signal the text is telling the player what to do. Used only to
# flag rows in text_corpus.csv for the human coder. Never used to draw a
# conclusion on its own.

INPUT_VERBS = [
    "press", "hold", "click", "tap", "double-click", "right-click", "drag",
    "drop", "grab", "grasp", "release", "pull", "push", "throw", "swipe",
    "point", "aim", "look", "gaze", "turn", "rotate", "spin", "move", "walk",
    "teleport", "jump", "select", "choose", "pick", "place", "type", "enter",
    "submit", "scroll", "zoom", "swing", "shake", "touch", "reach", "use",
    "open", "close", "drop down", "hover", "wave", "squeeze", "trigger",
]

KEY_TOKENS = [
    "wasd", "spacebar", "space bar", "enter", "esc", "escape", "shift", "ctrl",
    "alt", "tab", "arrow key", "left mouse", "right mouse", "mouse button",
    "trigger", "grip button", "thumbstick", "joystick", "touchpad", "a button",
    "b button", "x button", "y button", "left click", "right click",
    "controller", "headset", "keyboard", "mouse",
]

FEEDBACK_TOKENS = [
    "correct", "incorrect", "try again", "well done", "nice", "wrong", "right",
    "score", "points", "you earned", "you lost", "complete", "finished",
    "failed", "success", "error", "not quite", "good job",
]

IMPERATIVE_START = re.compile(
    r"^\s*(" + "|".join(re.escape(v) for v in sorted(INPUT_VERBS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def truncate(s: str, n: int = 160) -> str:
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def is_probably_text(s: str) -> bool:
    if not s:
        return False
    printable = sum(1 for c in s if c.isprintable() or c in "\t\n")
    return printable / len(s) >= 0.9


# --------------------------------------------------------------------------
# Decompression
# --------------------------------------------------------------------------

def decompress_if_needed(data: bytes) -> tuple[bytes, str]:
    """Return (decompressed_bytes, method). Unity WebGL data files ship raw,
    gzip (.gz / .unityweb), or brotli (.br / .unityweb)."""
    if data[:16].startswith(b"UnityWebData"):
        return data, "none"
    if data[:2] == b"\x1f\x8b":
        try:
            return zlib.decompress(data, 16 + zlib.MAX_WBITS), "gzip"
        except zlib.error:
            pass
    if HAVE_BROTLI:
        try:
            out = brotli.decompress(data)  # type: ignore[union-attr]
            if out:
                return out, "brotli"
        except Exception:
            pass
    try:
        return zlib.decompress(data), "zlib"
    except zlib.error:
        pass
    return data, "none"


# --------------------------------------------------------------------------
# UnityWebData container
# --------------------------------------------------------------------------
# Layout (Unity WebGL .data):
#   char[]  signature, NUL-terminated, "UnityWebData1.0\0"
#   int32   header_size  (absolute offset where file payloads begin)
#   repeat while cursor < header_size:
#       int32 offset, int32 size, int32 path_len, char[path_len] path
#
# Reference implementations agree on this layout; the parser below validates
# every offset/size against the buffer length and refuses malformed entries
# rather than guessing.

@dataclass
class WebDataEntry:
    path: str
    offset: int
    size: int


class UnityWebData:
    SIGNATURE_PREFIX = b"UnityWebData"

    def __init__(self, data: bytes):
        self.data = data
        self.signature = ""
        self.entries: list[WebDataEntry] = []
        self._parse()

    @staticmethod
    def looks_like(data: bytes) -> bool:
        return data[:12] == UnityWebData.SIGNATURE_PREFIX

    def _parse(self) -> None:
        data = self.data
        nul = data.find(b"\x00")
        if nul < 0 or nul > 64:
            raise ValueError("UnityWebData: no NUL-terminated signature found")
        self.signature = data[:nul].decode("ascii", "replace")
        cursor = nul + 1
        (header_size,) = struct.unpack_from("<i", data, cursor)
        cursor += 4
        if not (0 < header_size <= len(data)):
            raise ValueError(f"UnityWebData: implausible header size {header_size}")
        while cursor + 12 <= header_size:
            offset, size, path_len = struct.unpack_from("<iii", data, cursor)
            cursor += 12
            if path_len < 0 or cursor + path_len > header_size:
                break
            path = data[cursor: cursor + path_len].decode("utf-8", "replace")
            cursor += path_len
            if offset < 0 or size < 0 or offset + size > len(data):
                continue
            self.entries.append(WebDataEntry(path=path, offset=offset, size=size))

    def read(self, entry: WebDataEntry) -> bytes:
        return self.data[entry.offset: entry.offset + entry.size]


# --------------------------------------------------------------------------
# String extraction
# --------------------------------------------------------------------------

ASCII_RUN = re.compile(rb"[\x20-\x7e\t]{4,}")


def scan_ascii_strings(data: bytes, min_len: int = 4) -> Iterator[tuple[int, str]]:
    """Plain printable-run scan. Cheap, noisy, good for wasm and metadata."""
    for m in ASCII_RUN.finditer(data):
        s = m.group().decode("ascii", "replace")
        if len(s) >= min_len:
            yield m.start(), s


def scan_unity_strings(data: bytes, min_len: int = 2, max_len: int = 8192) -> Iterator[tuple[int, str]]:
    """Unity serializes managed strings as int32 length + UTF-8 bytes, aligned
    to 4. Scanning for that shape recovers real string fields (including UI
    text) from serialized objects even when no type tree is present."""
    n = len(data)
    i = 0
    while i + 4 <= n:
        (length,) = struct.unpack_from("<i", data, i)
        if min_len <= length <= max_len and i + 4 + length <= n:
            raw = data[i + 4: i + 4 + length]
            try:
                s = raw.decode("utf-8")
            except UnicodeDecodeError:
                i += 4
                continue
            if is_probably_text(s) and not s.isspace():
                yield i, s
                i += 4 + length
                i += (-i) % 4
                continue
        i += 4


# --------------------------------------------------------------------------
# IL2CPP metadata
# --------------------------------------------------------------------------

IL2CPP_SANITY = 0xFAB11BAF


def read_il2cpp_header(data: bytes) -> Optional[dict[str, Any]]:
    """Il2CppGlobalMetadataHeader begins with, in order:
        int32 sanity (0xFAB11BAF), int32 version,
        int32 stringLiteralOffset, int32 stringLiteralSize,
        int32 stringLiteralDataOffset, int32 stringLiteralDataSize,
        int32 stringOffset, int32 stringSize, ...
    Only these eight fields are read. They have held their position across
    metadata versions 16 to 31, unlike the later fields.

    The two string regions are different things and the distinction matters.
    The literal region holds string constants the program can display. The
    string region holds identifiers: type, method, field, and parameter names."""
    if len(data) < 32:
        return None
    (sanity, version, lit_off, lit_size, lit_data_off, lit_data_size,
     str_off, str_size) = struct.unpack_from("<Iiiiiiii", data, 0)
    if sanity != IL2CPP_SANITY:
        return None
    return {
        "sanity": hex(sanity),
        "metadata_version": version,
        "size": len(data),
        "string_literal_offset": lit_off,
        "string_literal_size": lit_size,
        "string_literal_data_offset": lit_data_off,
        "string_literal_data_size": lit_data_size,
        "string_offset": str_off,
        "string_size": str_size,
    }


def parse_il2cpp_identifiers(data: bytes, hdr: dict[str, Any]) -> list[tuple[int, str]]:
    """Return [(ordinal, identifier)] from the metadata string region.

    The region is a packed block of NUL-terminated UTF-8 names covering every
    type, method, field, and parameter in the build. Order is roughly
    declaration order, which is why an ordinal is returned: neighbouring
    ordinals often belong to the same type. That adjacency is a lead to follow
    by hand, not a grouping the tool asserts."""
    off, size = hdr.get("string_offset", 0), hdr.get("string_size", 0)
    n = len(data)
    if not (0 < off < n and 0 < size and off + size <= n):
        return []
    block = data[off: off + size]
    out: list[tuple[int, str]] = []
    ordinal = 0
    for raw in block.split(b"\x00"):
        if not raw:
            continue
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            ordinal += 1
            continue
        if s:
            out.append((ordinal, s))
        ordinal += 1
    return out


def parse_il2cpp_string_literals(data: bytes, hdr: dict[str, Any]) -> list[tuple[int, str]]:
    """Return [(literal_index, text)] from the string literal table.

    Each entry in the table is Il2CppStringLiteral { int32 length; int32
    dataIndex }, and dataIndex is a byte offset into the string literal data
    block. Every offset is bounds-checked; a single bad entry is skipped rather
    than aborting, and a table that fails validation returns an empty list so
    the caller falls back to raw scanning."""
    off, size = hdr["string_literal_offset"], hdr["string_literal_size"]
    doff, dsize = hdr["string_literal_data_offset"], hdr["string_literal_data_size"]
    n = len(data)
    if not (0 < off < n and 0 < size and off + size <= n and size % 8 == 0):
        return []
    if not (0 < doff < n and 0 <= dsize and doff + dsize <= n):
        return []
    out: list[tuple[int, str]] = []
    count = size // 8
    if count > 2_000_000:
        return []
    for i in range(count):
        length, data_index = struct.unpack_from("<ii", data, off + i * 8)
        if length < 0 or data_index < 0 or data_index + length > dsize:
            continue
        raw = data[doff + data_index: doff + data_index + length]
        try:
            out.append((i, raw.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return out


# --------------------------------------------------------------------------
# Detector engine
# --------------------------------------------------------------------------

class Detectors:
    def __init__(self, table: list[tuple[str, str, str]]):
        self.rules = [(name, cat, re.compile(pat)) for name, cat, pat in table]

    def run_on_strings(self, items: Iterable[tuple[int, str]], source: str,
                       locator_kind: str = "offset") -> list[Finding]:
        out: list[Finding] = []
        for pos, s in items:
            for name, cat, rx in self.rules:
                m = rx.search(s)
                if m:
                    out.append(Finding(
                        category=cat,
                        detector=name,
                        evidence=truncate(m.group(0), 120),
                        source=source,
                        locator=f"{locator_kind}:{pos}",
                        context=truncate(s, 200),
                    ))
        return out

    def run_on_text(self, text: str, source: str) -> list[Finding]:
        out: list[Finding] = []
        line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                line_starts.append(i + 1)

        def line_of(idx: int) -> int:
            lo, hi = 0, len(line_starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_starts[mid] <= idx:
                    lo = mid
                else:
                    hi = mid - 1
            return lo + 1

        for name, cat, rx in self.rules:
            for m in rx.finditer(text):
                ln = line_of(m.start())
                start = max(0, m.start() - 60)
                out.append(Finding(
                    category=cat,
                    detector=name,
                    evidence=truncate(m.group(0), 120),
                    source=source,
                    locator=f"line:{ln}",
                    context=truncate(text[start: m.end() + 60], 200),
                ))
        return out


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen = set()
    out = []
    for f in findings:
        k = f.key()
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


# --------------------------------------------------------------------------
# Text corpus rows
# --------------------------------------------------------------------------

@dataclass
class CorpusRow:
    text: str
    source: str
    locator: str
    origin: str            # "unity_string", "monobehaviour", "textasset", "html", "js_literal"
    object_type: str = ""
    object_name: str = ""
    char_len: int = 0
    word_count: int = 0
    input_verbs: str = ""
    key_tokens: str = ""
    feedback_tokens: str = ""
    imperative_opening: bool = False
    code: str = ""         # left blank for the human coder

    @staticmethod
    def build(text: str, source: str, locator: str, origin: str,
              object_type: str = "", object_name: str = "") -> "CorpusRow":
        low = text.lower()
        verbs = [v for v in INPUT_VERBS if re.search(r"\b" + re.escape(v) + r"\b", low)]
        keys = [k for k in KEY_TOKENS if k in low]
        fb = [t for t in FEEDBACK_TOKENS if t in low]
        return CorpusRow(
            text=text,
            source=source,
            locator=locator,
            origin=origin,
            object_type=object_type,
            object_name=object_name,
            char_len=len(text),
            word_count=len(text.split()),
            input_verbs="|".join(verbs),
            key_tokens="|".join(keys),
            feedback_tokens="|".join(fb),
            imperative_opening=bool(IMPERATIVE_START.match(text)),
        )


# Strings that are almost certainly engine internals, not player-facing text.
NOISE_PATTERNS = [
    re.compile(r"^[A-Za-z0-9_]+\.(cs|js|dll|png|wav|ogg|mp3|shader|asset|prefab|unity|mat|anim|ttf|otf)$", re.I),
    re.compile(r"^(m_|k_|_)[A-Za-z]"),
    re.compile(r"^(UnityEngine|System|Mono|TMPro|Microsoft|Newtonsoft|Assembly-CSharp)\b"),
    re.compile(r"^[0-9a-f]{16,}$", re.I),
    re.compile(r"^[^A-Za-z]*$"),
    re.compile(r"^(Assets|Library|Packages|Resources)/"),
    re.compile(r"^#?[A-Za-z]+\s*\{"),
    re.compile(r"^\s*(get|set)_[A-Za-z]"),
    re.compile(r"^(SHADER|PROP|Hidden/|Legacy Shaders/)"),
]


def looks_player_facing(s: str, min_words: int = 1) -> bool:
    s = s.strip()
    if len(s) < 3 or len(s) > 600:
        return False
    if any(p.search(s) for p in NOISE_PATTERNS):
        return False
    letters = sum(1 for c in s if c.isalpha())
    if letters < 3 or letters / len(s) < 0.5:
        return False
    if len(s.split()) < min_words:
        return False
    # Reject identifier-shaped tokens such as PascalCaseNoSpaces.
    if " " not in s and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", s) and len(s) < 25:
        return False
    return True


# --------------------------------------------------------------------------
# Unity analysis
# --------------------------------------------------------------------------

UNITY_VERSION_RX = re.compile(rb"\d{4}\.\d+\.\d+[abfpx]\d+")

WEB_TEXT_EXT = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".txt", ".xml", ".svg", ".jslib"}
UNITY_DATA_EXT = {".data", ".unityweb", ".br", ".gz", ".bundle", ".wasm"}


class UnityAnalysis:
    def __init__(self, root: Path, outdir: Path, args: argparse.Namespace):
        self.root = root
        self.outdir = outdir
        self.args = args
        self.findings: list[Finding] = []
        self.corpus: list[CorpusRow] = []
        self.files: list[FileRecord] = []
        self.components: dict[str, int] = {}
        self.assets_summary: dict[str, int] = {}
        self.scenes: list[str] = []
        self.unity_versions: set[str] = set()
        self.notes: list[str] = []
        self.rules: list[RuleRow] = []
        self.acts: list[SpeechActRow] = []
        self.det = Detectors(UNITY_SIGNATURES)
        self.webdet = Detectors(WEB_SIGNATURES)
        self.extract_dir = outdir / "extracted"

    # -- entry ------------------------------------------------------------
    def run(self) -> None:
        targets = [self.root] if self.root.is_file() else sorted(
            p for p in self.root.rglob("*") if p.is_file()
        )
        for p in targets:
            rel = str(p.relative_to(self.root)) if self.root.is_dir() else p.name
            try:
                rec = FileRecord(path=rel, size=p.stat().st_size, sha256=sha256_file(p))
            except OSError as e:
                self.notes.append(f"Could not hash {rel}: {e}")
                continue
            rec.kind = self._classify(p)
            self.files.append(rec)

        for rec in self.files:
            p = self.root / rec.path if self.root.is_dir() else self.root
            if rec.kind == "web_text":
                self._analyze_web_text(p, rec.path)
            elif rec.kind in ("unity_data", "wasm", "bundle"):
                self._analyze_binary(p, rec.path, rec.kind)

    def _classify(self, p: Path) -> str:
        suf = p.suffix.lower()
        name = p.name.lower()
        if suf in WEB_TEXT_EXT:
            return "web_text"
        if suf == ".wasm" or name.endswith(".wasm.br") or name.endswith(".wasm.gz"):
            return "wasm"
        if suf in UNITY_DATA_EXT or name.endswith(".data.br") or name.endswith(".data.gz"):
            return "unity_data"
        if name in ("data.unity3d", "globalgamemanagers") or suf in (".assets", ".unity3d", ".resource"):
            return "bundle"
        return "other"

    # -- web text ---------------------------------------------------------
    def _analyze_web_text(self, path: Path, rel: str) -> None:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError as e:
            self.notes.append(f"Could not read {rel}: {e}")
            return
        self.findings.extend(self.webdet.run_on_text(text, rel))
        self.findings.extend(self.det.run_on_text(text, rel))
        for m in UNITY_VERSION_RX.finditer(text.encode("utf-8", "replace")):
            self.unity_versions.add(m.group().decode())
        if path.suffix.lower() in (".html", ".htm"):
            self._corpus_from_html(text, rel)

    def _corpus_from_html(self, text: str, rel: str) -> None:
        stripped = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
        for m in re.finditer(r">([^<>]{3,300})<", stripped):
            s = " ".join(m.group(1).split())
            if looks_player_facing(s, min_words=self.args.min_words):
                self.corpus.append(CorpusRow.build(s, rel, f"offset:{m.start()}", "html"))

    # -- binaries ---------------------------------------------------------
    def _analyze_binary(self, path: Path, rel: str, kind: str) -> None:
        try:
            raw = path.read_bytes()
        except OSError as e:
            self.notes.append(f"Could not read {rel}: {e}")
            return
        data, method = decompress_if_needed(raw)
        if method != "none":
            self.notes.append(f"{rel}: decompressed with {method} "
                              f"({len(raw):,} to {len(data):,} bytes)")
        elif raw[:2] == b"\x1f\x8b" or path.name.lower().endswith(".br"):
            self.notes.append(
                f"{rel}: appears compressed but could not be decompressed"
                + ("" if HAVE_BROTLI else "; install the brotli package")
            )

        for m in UNITY_VERSION_RX.finditer(data[: 4 << 20]):
            self.unity_versions.add(m.group().decode())

        if UnityWebData.looks_like(data):
            self._analyze_webdata(data, rel)
        else:
            self._analyze_blob(data, rel, kind)

    def _analyze_webdata(self, data: bytes, rel: str) -> None:
        try:
            wd = UnityWebData(data)
        except ValueError as e:
            self.notes.append(f"{rel}: {e}")
            return
        self.notes.append(f"{rel}: UnityWebData container '{wd.signature}' "
                          f"with {len(wd.entries)} entries")
        if self.args.extract:
            self.extract_dir.mkdir(parents=True, exist_ok=True)
        for entry in wd.entries:
            blob = wd.read(entry)
            inner = f"{rel}!{entry.path}"
            if self.args.extract:
                dest = self.extract_dir / Path(entry.path.replace("\\", "/"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(blob)
            if entry.path.lower().endswith("global-metadata.dat"):
                self._analyze_metadata(blob, inner)
            elif entry.path.lower().endswith((".json", ".txt", ".xml", ".csv")):
                try:
                    self._analyze_web_text_blob(blob.decode("utf-8"), inner)
                except UnicodeDecodeError:
                    self._analyze_blob(blob, inner, "bundle")
            else:
                self._analyze_blob(blob, inner, "bundle")

    def _analyze_web_text_blob(self, text: str, rel: str) -> None:
        self.findings.extend(self.webdet.run_on_text(text, rel))
        self.findings.extend(self.det.run_on_text(text, rel))
        for m in re.finditer(r'"([^"\\]{4,300})"', text):
            s = m.group(1)
            if looks_player_facing(s, min_words=self.args.min_words):
                self.corpus.append(CorpusRow.build(s, rel, f"offset:{m.start()}", "textasset"))

    def _analyze_metadata(self, blob: bytes, rel: str) -> None:
        hdr = read_il2cpp_header(blob)
        literals: list[tuple[int, str]] = []
        if hdr:
            literals = parse_il2cpp_string_literals(blob, hdr)
            self.notes.append(
                f"{rel}: IL2CPP global-metadata, format version "
                f"{hdr['metadata_version']}, {hdr['size']:,} bytes, "
                f"{len(literals):,} string literals parsed from the literal table"
                if literals else
                f"{rel}: IL2CPP global-metadata, format version "
                f"{hdr['metadata_version']}, {hdr['size']:,} bytes; "
                f"literal table did not validate, fell back to raw string scanning"
            )
        else:
            self.notes.append(f"{rel}: no IL2CPP sanity marker; treated as opaque data")

        if hdr:
            idents = parse_il2cpp_identifiers(blob, hdr)
            if idents:
                self.notes.append(
                    f"{rel}: {len(idents):,} identifiers read from the metadata name "
                    f"table (type, method, field, and parameter names)")
                for ordinal, name in idents:
                    self.rules.extend(classify_identifier(ordinal, name, rel))

        if literals:
            self.findings.extend(self.det.run_on_strings(
                [(i, s) for i, s in literals], rel, locator_kind="literal"))
            for idx, s in literals:
                if looks_player_facing(s, min_words=self.args.min_words):
                    self.corpus.append(CorpusRow.build(
                        s, rel, f"literal:{idx}", "il2cpp_string_literal"))

        strings = list(scan_ascii_strings(blob, min_len=4))
        self.findings.extend(self.det.run_on_strings(strings, rel))
        if not literals:
            for pos, s in strings:
                if looks_player_facing(s, min_words=self.args.min_words):
                    self.corpus.append(CorpusRow.build(
                        s, rel, f"offset:{pos}", "il2cpp_metadata_scan"))

    def _analyze_blob(self, data: bytes, rel: str, kind: str) -> None:
        if HAVE_UNITYPY and kind in ("bundle", "unity_data") and self._try_unitypy(data, rel):
            return
        ascii_strings = list(scan_ascii_strings(data, min_len=5))
        self.findings.extend(self.det.run_on_strings(ascii_strings, rel))
        if kind != "wasm":
            unity_strings = list(scan_unity_strings(data))
            self.findings.extend(self.det.run_on_strings(unity_strings, rel))
            for pos, s in unity_strings:
                if looks_player_facing(s, min_words=self.args.min_words):
                    self.corpus.append(CorpusRow.build(s, rel, f"offset:{pos}", "unity_string"))
        else:
            for pos, s in ascii_strings:
                if looks_player_facing(s, min_words=max(3, self.args.min_words)):
                    self.corpus.append(CorpusRow.build(s, rel, f"offset:{pos}", "wasm_string"))

    # -- UnityPy path -----------------------------------------------------
    def _try_unitypy(self, data: bytes, rel: str) -> bool:
        """Parse a serialized file or bundle with UnityPy. Returns True when
        UnityPy produced objects, False to fall back to raw scanning."""
        try:
            env = UnityPy.load(io.BytesIO(data))  # type: ignore[union-attr]
            objects = list(env.objects)
        except Exception as e:
            self.notes.append(f"{rel}: UnityPy could not parse ({type(e).__name__}); "
                              f"fell back to string scanning")
            return False
        if not objects:
            return False
        self.notes.append(f"{rel}: UnityPy parsed {len(objects)} objects")

        for obj in objects:
            tname = getattr(obj.type, "name", str(obj.type))
            self.assets_summary[tname] = self.assets_summary.get(tname, 0) + 1
            locator = f"pathid:{obj.path_id}"

            if tname == "MonoScript":
                try:
                    ms = obj.read()
                    cls = getattr(ms, "m_ClassName", "") or ""
                    ns = getattr(ms, "m_Namespace", "") or ""
                    asm = getattr(ms, "m_AssemblyName", "") or ""
                    full = f"{ns}.{cls}" if ns else cls
                    if full:
                        self.components[full] = self.components.get(full, 0) + 1
                        self.rules.extend(classify_identifier(0, cls, f"{rel}!MonoScript"))
                        self.findings.extend(self.det.run_on_strings(
                            [(0, f"{full} {asm}")], rel, locator_kind="pathid"))
                except Exception:
                    pass
                continue

            if tname in ("TextAsset",):
                try:
                    ta = obj.read()
                    script = getattr(ta, "m_Script", None)
                    if isinstance(script, (bytes, bytearray)):
                        script = script.decode("utf-8", "replace")
                    if isinstance(script, str) and script:
                        name = getattr(ta, "m_Name", "")
                        self._analyze_web_text_blob(script, f"{rel}!{name or 'TextAsset'}")
                except Exception:
                    pass
                continue

            if tname in ("AssetBundle", "PlayerSettings", "BuildSettings"):
                try:
                    raw = obj.get_raw_data()
                    for pos, s in scan_unity_strings(raw):
                        if s.lower().endswith(".unity"):
                            self.scenes.append(s)
                except Exception:
                    pass
                continue

            if tname == "MonoBehaviour":
                name = ""
                try:
                    name = obj.peek_name() or ""
                except Exception:
                    pass
                try:
                    raw = obj.get_raw_data()
                except Exception:
                    continue
                strings = list(scan_unity_strings(raw))
                self.findings.extend(self.det.run_on_strings(strings, rel, locator_kind="offset"))
                for pos, s in strings:
                    if looks_player_facing(s, min_words=self.args.min_words):
                        self.corpus.append(CorpusRow.build(
                            s, rel, locator, "monobehaviour",
                            object_type="MonoBehaviour", object_name=name))
                continue

            if tname in ("GameObject", "Canvas", "RectTransform", "Animator",
                         "AudioSource", "ParticleSystem", "Camera"):
                try:
                    nm = obj.peek_name() or ""
                except Exception:
                    nm = ""
                if nm:
                    self.findings.append(Finding(
                        category=f"structure.{tname.lower()}",
                        detector="unitypy_object",
                        evidence=f"{tname}: {truncate(nm, 80)}",
                        source=rel,
                        locator=locator,
                        confidence="observed",
                    ))
        return True


# --------------------------------------------------------------------------
# Generic web analysis
# --------------------------------------------------------------------------

class WebAnalysis:
    def __init__(self, root: Path, outdir: Path, args: argparse.Namespace):
        self.root = root
        self.outdir = outdir
        self.args = args
        self.findings: list[Finding] = []
        self.corpus: list[CorpusRow] = []
        self.files: list[FileRecord] = []
        self.notes: list[str] = []
        self.rules: list[RuleRow] = []
        self.acts: list[SpeechActRow] = []
        self.components: dict[str, int] = {}
        self.assets_summary: dict[str, int] = {}
        self.scenes: list[str] = []
        self.unity_versions: set[str] = set()
        self.det = Detectors(WEB_SIGNATURES)

    def run(self) -> None:
        paths = [self.root] if self.root.is_file() else sorted(
            p for p in self.root.rglob("*") if p.is_file()
        )
        for p in paths:
            rel = str(p.relative_to(self.root)) if self.root.is_dir() else p.name
            try:
                self.files.append(FileRecord(path=rel, size=p.stat().st_size,
                                             sha256=sha256_file(p), kind=p.suffix.lower()))
            except OSError:
                continue
            if p.suffix.lower() not in WEB_TEXT_EXT:
                continue
            text = p.read_text("utf-8", errors="replace")
            self.findings.extend(self.det.run_on_text(text, rel))
            if p.suffix.lower() in (".html", ".htm"):
                stripped = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
                for m in re.finditer(r">([^<>]{3,300})<", stripped):
                    s = " ".join(m.group(1).split())
                    if looks_player_facing(s, min_words=self.args.min_words):
                        self.corpus.append(CorpusRow.build(s, rel, f"offset:{m.start()}", "html"))
            else:
                for m in re.finditer(r"""(['"])([^'"\\\n]{6,300})\1""", text):
                    s = m.group(2)
                    if looks_player_facing(s, min_words=max(3, self.args.min_words)):
                        self.corpus.append(CorpusRow.build(s, rel, f"offset:{m.start()}", "js_literal"))


# --------------------------------------------------------------------------
# Detection of build type
# --------------------------------------------------------------------------

def detect_build_type(root: Path) -> str:
    if root.is_file():
        n = root.name.lower()
        if n.endswith((".data", ".unityweb", ".data.br", ".data.gz", ".unity3d", ".wasm")):
            return "unity"
        return "web"
    names = {p.name.lower() for p in root.rglob("*") if p.is_file()}
    unity_markers = any(
        n.endswith((".loader.js", ".framework.js", ".data", ".unityweb", ".data.br",
                    ".data.gz", ".wasm.br", "data.unity3d"))
        for n in names
    )
    return "unity" if unity_markers else "web"


# --------------------------------------------------------------------------
# Scheme mapping
# --------------------------------------------------------------------------

def load_scheme(path: Optional[str]) -> dict[str, str]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("scheme file must be a JSON object mapping category -> code")
    return {str(k): str(v) for k, v in data.items()}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def write_outputs(analysis, root: Path, outdir: Path, build_type: str,
                  scheme: dict[str, str], argv: list[str]) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    findings = dedupe(analysis.findings)
    findings.sort(key=lambda f: (f.category, f.detector, f.source, f.locator))

    # findings.json
    payload = {
        "tool": "build_analyzer.py",
        "tool_version": TOOL_VERSION,
        "command": " ".join(argv),
        "target": str(root),
        "build_type": build_type,
        "unitypy_available": HAVE_UNITYPY,
        "brotli_available": HAVE_BROTLI,
        "notes": analysis.notes,
        "unity_versions_seen": sorted(getattr(analysis, "unity_versions", set())),
        "asset_type_counts": dict(sorted(getattr(analysis, "assets_summary", {}).items(),
                                         key=lambda kv: -kv[1])),
        "scenes": sorted(set(getattr(analysis, "scenes", []))),
        "findings": [
            {**asdict(f), "mapped_code": scheme.get(f.category, "")}
            for f in findings
        ],
        "rule_vocabulary": [asdict(r) for r in dedupe_rules(getattr(analysis, "rules", []))],
        "speech_acts": [asdict(a) for a in collect_speech_acts(dedupe_corpus(analysis.corpus))],
    }
    (outdir / "findings.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # manifest.json
    (outdir / "manifest.json").write_text(
        json.dumps([asdict(r) for r in analysis.files], indent=2), encoding="utf-8")

    # components.csv
    comps = getattr(analysis, "components", {})
    if comps:
        with (outdir / "components.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["component_type", "occurrences"])
            for k, v in sorted(comps.items(), key=lambda kv: (-kv[1], kv[0])):
                w.writerow([k, v])

    # text_corpus.csv
    rows = dedupe_corpus(analysis.corpus)
    with (outdir / "text_corpus.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        cols = ["text", "source", "locator", "origin", "object_type", "object_name",
                "char_len", "word_count", "input_verbs", "key_tokens",
                "feedback_tokens", "imperative_opening", "code"]
        w.writerow(cols)
        for r in rows:
            w.writerow([getattr(r, c) for c in cols])

    # procedural rhetoric outputs
    rules = dedupe_rules(getattr(analysis, "rules", []))
    acts = collect_speech_acts(rows)
    write_rhetoric_outputs(outdir, rules, acts)

    # report.md
    (outdir / "report.md").write_text(
        render_report(analysis, root, build_type, findings, rows, scheme, rules, acts),
        encoding="utf-8")


def dedupe_rules(rules: list[RuleRow]) -> list[RuleRow]:
    seen = set()
    out = []
    for r in rules:
        k = (r.identifier, r.category, r.source)
        if k not in seen:
            seen.add(k)
            out.append(r)
    out.sort(key=lambda r: (r.framework_likely, r.category, r.identifier.lower()))
    return out


def collect_speech_acts(rows: list[CorpusRow]) -> list[SpeechActRow]:
    acts: list[SpeechActRow] = []
    for r in rows:
        acts.extend(classify_speech_act(r.text, r.source, r.locator))
    return acts


def write_rhetoric_outputs(outdir: Path, rules: list[RuleRow],
                           acts: list[SpeechActRow]) -> None:
    if rules:
        with (outdir / "rule_vocabulary.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["identifier", "category", "gloss", "metadata_ordinal",
                        "source", "framework_likely", "makes_possible",
                        "makes_impossible", "confirmed_in_play", "notes"])
            for r in rules:
                w.writerow([r.identifier, r.category, r.gloss, r.ordinal, r.source,
                            r.framework_likely, "", "", "", ""])

    if acts:
        with (outdir / "speech_acts.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["text", "category", "gloss", "matched_phrase", "source",
                        "locator", "trigger_condition", "confirmed_in_play", "notes"])
            for a in acts:
                w.writerow([a.text, a.category, a.gloss, a.matched, a.source,
                            a.locator, "", "", ""])

    # Worksheet: one row per category, pre-filled with counts and evidence,
    # with the interpretive columns left empty on purpose.
    buckets: dict[str, dict[str, Any]] = {}
    for r in [x for x in rules if not x.framework_likely]:
        b = buckets.setdefault(r.category, {"gloss": r.gloss, "kind": "rule vocabulary",
                                            "count": 0, "evidence": []})
        b["count"] += 1
        if len(b["evidence"]) < 12:
            b["evidence"].append(r.identifier)
    for a in acts:
        b = buckets.setdefault(a.category, {"gloss": a.gloss, "kind": "player-visible text",
                                            "count": 0, "evidence": []})
        b["count"] += 1
        if len(b["evidence"]) < 12:
            b["evidence"].append(truncate(a.text, 70))

    if not buckets:
        return
    with (outdir / "rhetoric_worksheet.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "evidence_kind", "what_this_category_names",
                    "evidence_count", "example_evidence",
                    "what_the_build_makes_possible",
                    "what_the_build_makes_impossible",
                    "what_the_build_rewards",
                    "what_the_build_punishes",
                    "who_this_positions_the_player_as",
                    "confirmed_in_play", "notes"])
        for cat in sorted(buckets):
            b = buckets[cat]
            w.writerow([cat, b["kind"], b["gloss"], b["count"],
                        " | ".join(b["evidence"]), "", "", "", "", "", "", ""])


def dedupe_corpus(rows: list[CorpusRow]) -> list[CorpusRow]:
    seen = set()
    out = []
    for r in rows:
        k = (r.text, r.source)
        if k not in seen:
            seen.add(k)
            out.append(r)
    out.sort(key=lambda r: (r.source, -r.word_count, r.text))
    return out


def render_report(analysis, root: Path, build_type: str, findings: list[Finding],
                  rows: list[CorpusRow], scheme: dict[str, str],
                  rules: Optional[list[RuleRow]] = None,
                  acts: Optional[list[SpeechActRow]] = None) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# Build analysis report")
    a("")
    a(f"Target: `{root}`  ")
    a(f"Detected build type: **{build_type}**  ")
    a(f"Tool version: {TOOL_VERSION}  ")
    a(f"UnityPy available: {HAVE_UNITYPY}. brotli available: {HAVE_BROTLI}.")
    a("")
    a("Every row below is an observation with a file and locator. Nothing here is")
    a("an interpretation of design intent.")
    a("")

    a("## 1. File inventory")
    a("")
    a("| File | Bytes | Kind | SHA-256 (first 16) |")
    a("| --- | ---: | --- | --- |")
    for r in sorted(analysis.files, key=lambda r: -r.size)[:40]:
        a(f"| `{r.path}` | {r.size:,} | {r.kind} | `{r.sha256[:16]}` |")
    if len(analysis.files) > 40:
        a(f"| ... {len(analysis.files) - 40} more | | | |")
    a("")

    vers = sorted(getattr(analysis, "unity_versions", set()))
    if vers:
        a(f"Unity version strings observed: {', '.join('`' + v + '`' for v in vers)}")
        a("")

    assets = getattr(analysis, "assets_summary", {})
    if assets:
        a("## 2. Asset object counts")
        a("")
        a("| Object type | Count |")
        a("| --- | ---: |")
        for k, v in sorted(assets.items(), key=lambda kv: -kv[1])[:30]:
            a(f"| {k} | {v} |")
        a("")

    comps = getattr(analysis, "components", {})
    if comps:
        a("## 3. Script and component types")
        a("")
        a(f"{len(comps)} distinct types. Full list in `components.csv`. Top 30:")
        a("")
        a("| Type | Count |")
        a("| --- | ---: |")
        for k, v in sorted(comps.items(), key=lambda kv: (-kv[1], kv[0]))[:30]:
            a(f"| `{k}` | {v} |")
        a("")

    a("## 4. Interaction signature summary")
    a("")
    by_cat: dict[str, list[Finding]] = {}
    for f in findings:
        by_cat.setdefault(f.category, []).append(f)
    if not by_cat:
        a("No interaction signatures matched. This is itself a result: either the")
        a("build is compressed in a form this tool could not open, or the code is")
        a("stripped. Check the notes section before treating it as an absence.")
        a("")
    else:
        a("| Category | Mapped code | Hits | Distinct detectors | Example evidence |")
        a("| --- | --- | ---: | ---: | --- |")
        for cat in sorted(by_cat):
            fs = by_cat[cat]
            dets = sorted({f.detector for f in fs})
            a(f"| `{cat}` | {scheme.get(cat, '')} | {len(fs)} | {len(dets)} | "
              f"`{truncate(fs[0].evidence, 50)}` |")
        a("")

        a("### Detail by category")
        a("")
        for cat in sorted(by_cat):
            a(f"#### `{cat}`")
            a("")
            a("| Detector | Evidence | Source | Locator |")
            a("| --- | --- | --- | --- |")
            for f in by_cat[cat][:25]:
                a(f"| {f.detector} | `{truncate(f.evidence, 60)}` | `{f.source}` | {f.locator} |")
            if len(by_cat[cat]) > 25:
                a(f"| ... {len(by_cat[cat]) - 25} more in findings.json | | | |")
            a("")

    a("## 5. Text corpus")
    a("")
    a(f"{len(rows)} candidate player-facing strings extracted to `text_corpus.csv`.")
    guided = [r for r in rows if r.input_verbs or r.key_tokens]
    imper = [r for r in rows if r.imperative_opening]
    fb = [r for r in rows if r.feedback_tokens]
    a("")
    a("| Slice | Count |")
    a("| --- | ---: |")
    a(f"| Total strings | {len(rows)} |")
    a(f"| Contains an input verb or input-device token | {len(guided)} |")
    a(f"| Opens with an imperative input verb | {len(imper)} |")
    a(f"| Contains an outcome or feedback token | {len(fb)} |")
    a("")
    if guided:
        a("Longest instruction-bearing strings:")
        a("")
        a("| Text | Verbs | Device tokens | Source |")
        a("| --- | --- | --- | --- |")
        for r in sorted(guided, key=lambda r: -r.word_count)[:20]:
            a(f"| {truncate(r.text, 90)} | {r.input_verbs} | {r.key_tokens} | `{r.source}` |")
        a("")

    rules = rules or []
    acts = acts or []
    if rules or acts:
        a("## 6. Procedural rhetoric evidence")
        a("")
        a("The rules themselves are compiled to WebAssembly and cannot be read. What")
        a("follows is the vocabulary the developers used to name the rules, and the")
        a("language the build uses to address the player. Both are evidence about what")
        a("the build treats as possible, impossible, rewarded, and punished. Neither is")
        a("the rule. Every row here is a candidate to confirm by playing the build.")
        a("")

        game_rules = [r for r in rules if not r.framework_likely]
        fw_rules = [r for r in rules if r.framework_likely]

        if game_rules:
            by: dict[str, list[RuleRow]] = {}
            for r in game_rules:
                by.setdefault(r.category, []).append(r)
            a("### 6.1 Rule vocabulary")
            a("")
            a(f"{len(game_rules)} identifiers classified, plus {len(fw_rules)} more that")
            a("look like framework or runtime names and are separated out in")
            a("`rule_vocabulary.csv` under `framework_likely`.")
            a("")
            a("| Category | What it names | Identifiers | Examples |")
            a("| --- | --- | ---: | --- |")
            for cat in sorted(by):
                rs = by[cat]
                ex = ", ".join(f"`{r.identifier}`" for r in rs[:6])
                a(f"| `{cat}` | {rs[0].gloss} | {len(rs)} | {ex} |")
            a("")

            for cat in sorted(by):
                rs = by[cat]
                if len(rs) <= 6:
                    continue
                a(f"#### `{cat}`")
                a("")
                a("| Identifier | Metadata ordinal | Source |")
                a("| --- | ---: | --- |")
                for r in rs[:30]:
                    a(f"| `{r.identifier}` | {r.ordinal} | `{r.source}` |")
                if len(rs) > 30:
                    a(f"| ... {len(rs) - 30} more in rule_vocabulary.csv | | |")
                a("")
            a("Neighbouring metadata ordinals usually belong to the same type, so a")
            a("reward identifier sitting a few ordinals from a punishment identifier is")
            a("worth opening the surrounding range in `rule_vocabulary.csv`. Adjacency is")
            a("a lead, not a grouping this tool asserts.")
            a("")

        if acts:
            byact: dict[str, list[SpeechActRow]] = {}
            for x in acts:
                byact.setdefault(x.category, []).append(x)
            a("### 6.2 What the build says to the player")
            a("")
            a("| Act | What it does | Strings | Examples |")
            a("| --- | --- | ---: | --- |")
            for cat in sorted(byact):
                xs = byact[cat]
                ex = "; ".join(truncate(x.text, 45) for x in xs[:3])
                a(f"| `{cat}` | {xs[0].gloss} | {len(xs)} | {ex} |")
            a("")

            praise = len(byact.get("act.praise", []))
            reprimand = len(byact.get("act.reprimand", []))
            prohibit = len(byact.get("act.prohibition", []))
            require = len(byact.get("act.requirement", []))
            retry = len(byact.get("act.retry", []))
            a("Ratios worth reporting, with the caveat that these count distinct strings")
            a("in the build and not how often a player hears them:")
            a("")
            a("| Measure | Value |")
            a("| --- | ---: |")
            a(f"| Praise strings | {praise} |")
            a(f"| Reprimand strings | {reprimand} |")
            a(f"| Praise to reprimand | {round(praise / reprimand, 2) if reprimand else 'no reprimand strings'} |")
            a(f"| Refusals and requirements | {prohibit + require} |")
            a(f"| Retry offers | {retry} |")
            a("")

        a("### 6.3 Worksheet")
        a("")
        a("`rhetoric_worksheet.csv` has one row per category above, pre-filled with the")
        a("evidence and with empty columns for what the build makes possible, makes")
        a("impossible, rewards, punishes, and who it positions the player as. The tool")
        a("does not fill those in. Filling them is the analysis, and it should be done")
        a("with the build running.")
        a("")

    a("## 7. Notes and limits")
    a("")
    for n in analysis.notes:
        a(f"- {n}")
    a("")
    a("Known limits, which belong in a methods section if this tool is cited:")
    a("")
    a("- The rule vocabulary is names, not logic. `MaxAttempts` shows that a ceiling")
    a("  on attempts was named somewhere in the build. It does not show the value, the")
    a("  condition, or whether the ceiling is ever reached.")
    a("- Identifier classification is lexical. A game type named `Level` and a")
    a("  framework type named `Level` look identical here, which is what the")
    a("  `framework_likely` flag is for, and that flag is itself a heuristic.")
    a("- String counts are counts of distinct strings in the build, not of anything a")
    a("  player experiences. A single reprimand string shown forty times a session")
    a("  counts once.")
    a("- If the build strips or obfuscates metadata, the rule vocabulary disappears")
    a("  entirely and the notes above will say so.")
    a("")
    a("- IL2CPP compiles C# to WebAssembly. Method bodies are not recovered, so this")
    a("  tool reports which types and API calls are referenced, not what they do.")
    a("- Absence of a signature is weak evidence. Engine code stripping, a managed")
    a("  stripping level above Low, or custom wrappers can hide a real dependency.")
    a("- String extraction from serialized objects without type trees is structural,")
    a("  not semantic. A string in a MonoBehaviour is not necessarily displayed.")
    a("- Counts are occurrences of a signature, not counts of runtime events.")
    a("- Anything marked `heuristic` in findings.json should be verified by hand")
    a("  before it appears in a claim.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        description="Static analysis of a Unity WebGL build or a generic WebGL program.")
    ap.add_argument("target", help="build directory, or a single .data/.wasm/.html file")
    ap.add_argument("-o", "--outdir", default="analysis_out", help="output directory")
    ap.add_argument("--type", choices=["auto", "unity", "web"], default="auto",
                    help="force analyzer mode (default: auto-detect)")
    ap.add_argument("--scheme", default=None,
                    help="JSON file mapping detector categories to your coding scheme")
    ap.add_argument("--min-words", type=int, default=2,
                    help="minimum word count for a string to enter text_corpus.csv "
                         "(raise it on large builds to cut engine noise)")
    ap.add_argument("--extract", action="store_true",
                    help="write unpacked UnityWebData members to <outdir>/extracted")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.target).expanduser().resolve()
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    outdir = Path(args.outdir).expanduser().resolve()
    scheme = load_scheme(args.scheme)

    build_type = args.type if args.type != "auto" else detect_build_type(root)
    analysis = UnityAnalysis(root, outdir, args) if build_type == "unity" \
        else WebAnalysis(root, outdir, args)

    if not args.quiet:
        print(f"Analyzing {root} as {build_type} build")
        if build_type == "unity" and not HAVE_UNITYPY:
            print("note: UnityPy not installed; asset-level parsing disabled "
                  "(pip install UnityPy)")
        if not HAVE_BROTLI:
            print("note: brotli not installed; .br builds cannot be decompressed "
                  "(pip install brotli)")

    analysis.run()
    write_outputs(analysis, root, outdir, build_type, scheme,
                  ["build_analyzer.py"] + argv)

    if not args.quiet:
        print(f"Files inventoried : {len(analysis.files)}")
        print(f"Findings          : {len(dedupe(analysis.findings))}")
        print(f"Corpus strings    : {len(dedupe_corpus(analysis.corpus))}")
        print(f"Output            : {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
