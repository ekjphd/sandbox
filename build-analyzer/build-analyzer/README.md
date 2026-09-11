# build-analyzer

Static, read-only analysis of a shipped Unity WebGL build or a generic
WebGL/HTML5 program. It inventories the build, unpacks the WebGL data container,
recovers the text the program carries, and reports which interaction-related
APIs and component types the build references. Every observation is recorded
with the file and the byte offset, literal index, or object path ID it came
from.

Built for artifact analysis of interactive instructional software, where a claim
in a write-up needs to be traceable back to the artifact. See
[METHODS.md](METHODS.md) for what the tool does, why it does it that way, how it
was validated, and what it cannot tell you.

---

## Requirements

- Python 3.10 or newer
- Two optional packages, strongly recommended:
  - `UnityPy`, which enables asset-level parsing of Unity serialized files
  - `brotli`, which is required to open `.data.br` builds

Without the optional packages the tool still runs. It inventories the build,
unpacks the container, and extracts strings, and it prints and records a note
saying what it could not do.

---

## Install

```bash
git clone https://github.com/ekjphd/sandbox.git
cd sandbox/build-analyzer
python3 -m pip install -r requirements.txt
```

To install without cloning the whole repository, download
`build_analyzer.py` and run `python3 -m pip install UnityPy brotli`. The script
has no other dependencies and no configuration file.

---

## Quick start

Point the tool at the folder a Unity WebGL build produced, meaning the folder
that contains `index.html` and a `Build` subfolder:

```bash
python3 build_analyzer.py /path/to/WebGLBuild -o results
```

Then open `results/report.md`.

A more complete run, with the container members written out and a coding scheme
applied:

```bash
python3 build_analyzer.py /path/to/WebGLBuild \
  -o results \
  --extract \
  --scheme scheme.example.json
```

---

## What to point it at

| You have | Pass this |
| --- | --- |
| A Unity WebGL build folder | The folder containing `index.html` and `Build/` |
| A build served from a web server | Download the folder first, then pass the local path |
| Only the data file | The `.data`, `.data.gz`, `.data.br`, or `.unityweb` file directly |
| A non-Unity WebGL or HTML5 program | The folder containing its `index.html` and scripts |
| A Unity project, not a build | Build it to WebGL first. This tool reads builds, not projects |

The tool auto-detects Unity against generic web by looking for `.loader.js`,
`.framework.js`, `.data`, `.wasm`, or `data.unity3d`. Override with
`--type unity` or `--type web` if the detection is wrong.

---

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `-o`, `--outdir` | `analysis_out` | Where to write results |
| `--type` | `auto` | Force `unity` or `web` analysis |
| `--scheme` | none | JSON file mapping detector categories to your own codes |
| `--extract` | off | Write unpacked container members to `<outdir>/extracted` |
| `--min-words` | `2` | Minimum word count for a string to enter the text corpus. Raise it on large builds to cut engine noise |
| `--quiet` | off | Suppress progress output |

---

## What you get

| File | What it is |
| --- | --- |
| `report.md` | Read this first. Inventory, asset counts, component types, interaction signatures with evidence, text corpus summary, and the notes and limits for this run |
| `findings.json` | Every observation with category, detector, evidence, source file, locator, context, and mapped code, plus the full run metadata |
| `text_corpus.csv` | One row per candidate player-facing string, annotated and ready to hand code. The last column, `code`, is left empty for you |
| `components.csv` | Every script and component type name with its occurrence count |
| `manifest.json` | Every input file with size and SHA-256 hash |
| `extracted/` | Unpacked container members, only with `--extract` |

### Reading report.md

Section 1 is the file inventory and the hashes. Section 2 and 3 are the asset
and component type profiles, which appear only when UnityPy could parse the
build. Section 4 is the interaction signature summary, which is the part most
people want: a category table, then a per-category detail table listing the
exact evidence and where it came from. Section 5 summarizes the text corpus and
lists the longest instruction-bearing strings. Section 6 is the notes for this
specific run, including anything the tool could not open, plus the standing
limits.

Section 6 matters. If a build failed to decompress or UnityPy could not parse
it, the absence of findings means nothing, and Section 6 is where that is said.

### Reading text_corpus.csv

| Column | Meaning |
| --- | --- |
| `text` | The recovered string |
| `source`, `locator` | Where it came from. `literal:16` is an index in the game's own string table; `offset:1284` is a byte offset; `pathid:-8842` is a serialized object |
| `origin` | How it was recovered. `il2cpp_string_literal` is the strongest provenance; a raw scan origin is weaker |
| `input_verbs` | Which input verbs the string contains, for example `press`, `grab`, `drag` |
| `key_tokens` | Which input-device terms it contains, for example `wasd`, `trigger`, `left mouse` |
| `feedback_tokens` | Which outcome terms it contains, for example `correct`, `try again` |
| `imperative_opening` | Whether the string opens with an imperative input verb |
| `code` | Empty. Yours to fill in |

The annotations sort strings for a human. They are not a classification. A
string that mentions "press" is flagged so you look at it sooner, not counted as
an instruction.

---

## Applying your own coding scheme

Detector categories are deliberately named after what was observed
(`input.xr_controller`, `feedback.haptic`, `locomotion.teleport`) rather than
after any published framework. To attach your codes, write a JSON file mapping
categories to codes:

```json
{
  "input.xr_controller": "D1-embodied",
  "input.pointer": "D1-indirect",
  "feedback.haptic": "D3-haptic",
  "feedback.text": "D3-textual"
}
```

Pass it with `--scheme`. The mapped code appears in `findings.json` and in the
report's category table. `scheme.example.json` in this folder is a starting
point; replace the codes with your own.

Keeping the mapping in a separate file means it becomes a publishable artifact
that a reader can criticize, and the same extraction can be re-coded under a
second framework without re-running anything.

---

## Comparing two builds

For paired corpora, such as an original and a later port of the same game, run
the analyzer on each and diff the results:

```bash
python3 build_analyzer.py /path/to/vr_build  -o results/vr
python3 build_analyzer.py /path/to/web_build -o results/web
python3 compare_runs.py results/vr results/web \
  -o comparison.md --csv comparison.csv \
  --label-a "VR original" --label-b "WebGL port"
```

`comparison.md` reports, in both directions, which categories, detectors, and
component types appear in one run and not the other, which appear in both with
different counts, and which instruction strings are unique to each. It ends with
the notes from both runs.

Every row is a candidate for inspection, not a finding. A component type present
in one build and absent in the other is a place to look, and the absence can
also mean the tool could not read that build. Check the notes before reading an
absence as a design change.

---

## Verifying the tool

`tests/make_fixtures.py` builds synthetic Unity WebGL and generic web programs
with known contents, constructed to the documented byte layouts, so that every
parser can be run against bytes whose correct output is known in advance:

```bash
cd tests
python3 make_fixtures.py
cd ..
python3 build_analyzer.py tests/fixtures/unity_build -o /tmp/check --extract
python3 build_analyzer.py tests/fixtures/web_build   -o /tmp/check_web
```

The Unity fixture plants 23 IL2CPP string literals, several UI strings in a
serialized blob, XR and desktop input signatures, and a gzip-compressed
`UnityWebData` container. All 23 literals should be recovered with their correct
indices, and the report should show categories including `input.xr_controller`,
`locomotion.teleport`, `feedback.haptic`, and `input.pointer`. The validation
table in [METHODS.md](METHODS.md) lists every path these fixtures exercise and
the results observed.

---

## What this tool does not do

It reports references, not executions. A detected API means the build contains
it, not that any player reached it. Stripped or wrapped code can hide a real
dependency, so a category with no hits should be reported as not detected rather
than absent. A string stored in an object is not necessarily a string that was
displayed. IL2CPP compiles C# to WebAssembly, so method bodies are not
recoverable and the tool reports the vocabulary of a build rather than its
logic.

The full limitations discussion, written to be reusable in a limitations
section, is in [METHODS.md](METHODS.md).

---

## Troubleshooting

**"brotli not installed" and a build that produces no findings.** The build is
`.data.br`. Run `python3 -m pip install brotli` and try again.

**"UnityPy could not parse" in the notes.** Asset-level parsing is unavailable
for that file, so no component list is produced. String extraction still runs.
Check that UnityPy is installed, and note that UnityPy support varies by Unity
version.

**Thousands of corpus rows, most of them noise.** Raise `--min-words`. A value
of 4 or 5 keeps sentences and drops most engine strings.

**Detected as the wrong build type.** Pass `--type unity` or `--type web`.

**Nothing at all in the report.** Read the notes section. It records every file
that could not be opened and why.

---

## License

MIT, matching the repository license.
