#!/usr/bin/env python3
"""Build synthetic fixtures that mimic the byte layouts a real build uses,
so the parsers can be exercised without shipping a proprietary build."""

import gzip
import os
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "fixtures"


def unity_string(s: str) -> bytes:
    b = s.encode("utf-8")
    out = struct.pack("<i", len(b)) + b
    pad = (-len(out)) % 4
    return out + b"\x00" * pad


def make_webdata(files: dict[str, bytes]) -> bytes:
    sig = b"UnityWebData1.0\x00"
    # header: sig + int32 header_size + per-file (off,size,pathlen,path)
    entries = []
    header_len = len(sig) + 4
    for path in files:
        header_len += 12 + len(path.encode("utf-8"))
    offset = header_len
    for path, blob in files.items():
        entries.append((offset, len(blob), path))
        offset += len(blob)
    out = bytearray()
    out += sig
    out += struct.pack("<i", header_len)
    for off, size, path in entries:
        pb = path.encode("utf-8")
        out += struct.pack("<iii", off, size, len(pb))
        out += pb
    assert len(out) == header_len, (len(out), header_len)
    for path, blob in files.items():
        out += blob
    return bytes(out)


def fake_metadata() -> bytes:
    """Structurally valid Il2CppGlobalMetadataHeader with a real string literal
    table, so the table parser is exercised rather than only the ASCII scan."""
    strings = [
        "UnityEngine.XR.Interaction.Toolkit",
        "XRGrabInteractable",
        "XRRayInteractor",
        "TeleportationArea",
        "SendHapticImpulse",
        "UnityEngine.UI.Button",
        "TMPro",
        "TextMeshProUGUI",
        "Input.GetMouseButtonDown",
        "Input.GetAxis",
        "StandaloneInputModule",
        "AudioSource",
        "PlayOneShot",
        "Animator",
        "UnityWebRequest",
        "Grab the ingredient with the trigger and place it in the pot.",
        "Press the left mouse button to pick up an item.",
        "Use WASD to move and the mouse to look around.",
        "Correct. Well done.",
        "Not quite, try again.",
        "Assets/Scenes/Kitchen.unity",
        "m_CachedPtr",
        "System.Collections.Generic",
    ]
    header_size = 0x110
    encoded = [s.encode("utf-8") for s in strings]
    table = bytearray()
    data_block = bytearray()
    for b in encoded:
        table += struct.pack("<ii", len(b), len(data_block))
        data_block += b

    lit_off = header_size
    lit_size = len(table)
    lit_data_off = header_size + lit_size
    lit_data_size = len(data_block)

    header = bytearray(b"\x00" * header_size)
    struct.pack_into("<Iiiiii", header, 0, 0xFAB11BAF, 29,
                     lit_off, lit_size, lit_data_off, lit_data_size)
    return bytes(header + table + data_block)


def fake_serialized_assets() -> bytes:
    body = bytearray(b"\x00" * 64)
    for s in [
        "MainCanvas",
        "InstructionPanel",
        "Press SPACEBAR to spin the wheel.",
        "Click a card to select it.",
        "Point at the door and pull the trigger to teleport.",
        "Score: {0}",
        "UnityEngine.UI.Text",
        "XRBaseInteractable",
        "Horizontal",
        "Vertical",
    ]:
        body += unity_string(s)
    return bytes(body)


def main() -> None:
    unity = ROOT / "unity_build"
    (unity / "Build").mkdir(parents=True, exist_ok=True)

    (unity / "index.html").write_text("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>HELLE's Kitchen (WebGL)</title></head>
<body>
<div id="unity-container"><canvas id="unity-canvas" tabindex="-1"></canvas></div>
<div id="unity-instructions">Click the canvas to start. Use WASD to move.</div>
<button id="unity-fullscreen-button" aria-label="Enter fullscreen">Fullscreen</button>
<script src="Build/kitchen.loader.js"></script>
<script>
  createUnityInstance(document.querySelector("#unity-canvas"), {
    dataUrl: "Build/kitchen.data.gz",
    frameworkUrl: "Build/kitchen.framework.js.gz",
    codeUrl: "Build/kitchen.wasm.gz",
    productName: "HELLEs Kitchen", productVersion: "1.2"
  });
  document.addEventListener("keydown", function (e) { console.log(e.key); });
  canvas.addEventListener("pointerdown", onDown);
  document.querySelector("#unity-canvas").requestPointerLock();
</script>
</body></html>
""", encoding="utf-8")

    (unity / "Build" / "kitchen.loader.js").write_text(
        'function createUnityInstance(c,cfg){var unityVersion="2021.3.16f1";'
        'return new Promise(function(r){c.addEventListener("mousedown",r);'
        'if(navigator.xr){navigator.xr.isSessionSupported("immersive-vr");}'
        'new AudioContext();});}\n', encoding="utf-8")

    data = make_webdata({
        "Il2CppData/Metadata/global-metadata.dat": fake_metadata(),
        "data.unity3d": fake_serialized_assets(),
        "StreamingAssets/modules.json":
            b'{"terms":[{"term":"la olla","hint":"Grab the pot and place it on the stove."}],'
            b'"ui":{"retry":"Not quite, try again."}}',
    })
    with open(unity / "Build" / "kitchen.data.gz", "wb") as fh:
        fh.write(gzip.compress(data))

    wasm = b"\x00asm\x01\x00\x00\x00" + b"".join(
        s.encode() + b"\x00" for s in [
            "_emscripten_glDrawArrays", "Input.GetKeyDown", "OVRInput",
            "SendMessage", "Hold the grip button to carry the pan.",
        ])
    (unity / "Build" / "kitchen.wasm").write_bytes(wasm)

    web = ROOT / "web_build"
    web.mkdir(parents=True, exist_ok=True)
    (web / "index.html").write_text("""<!DOCTYPE html><html><head><title>Demo</title></head>
<body><canvas id="c"></canvas>
<p role="status">Press the arrow keys to steer. Tap the screen to brake.</p>
<img src="wheel.png" alt="Steering wheel">
<script src="app.js"></script></body></html>
""", encoding="utf-8")
    (web / "app.js").write_text("""
const gl = document.getElementById('c').getContext('webgl2');
window.addEventListener('keydown', e => handle(e));
canvas.addEventListener('pointermove', onMove);
canvas.addEventListener('touchstart', onTouch);
navigator.xr && navigator.xr.requestSession('immersive-vr');
const ac = new AudioContext();
navigator.vibrate(50);
const HINT = "Hold the trigger and drag the block onto the target.";
const DONE = "Correct. Well done.";
THREE.Scene;
""", encoding="utf-8")

    print("fixtures at", ROOT)


if __name__ == "__main__":
    main()
