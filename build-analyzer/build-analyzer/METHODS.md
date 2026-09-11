# Build analyzer: methods notes

Notes on `build_analyzer.py` written for inclusion in a methods section. Every
factual claim about a file format is sourced at the end. Every claim about tool
behavior was verified by running the tool against fixtures on 11 September 2026;
the validation section records what was actually exercised.

---

## 1. What the tool is for

The tool performs static, read-only analysis of a shipped build. It does not run
the game, instrument it, or observe a player. Its purpose in a study of
interaction is narrow and worth stating plainly, because overclaiming here is
the obvious reviewer objection.

It answers three questions about an artifact:

1. Which interaction-related engine APIs and component types does this build
   reference?
2. What text does this build carry that addresses the player, and how much of
   that text is telling the player what to do?
3. What are the file-level facts about the build, so that a later reader can
   confirm the analysis ran against the same bytes?

It does not answer what the player experienced, whether a referenced API was
reached at runtime, or why a design decision was made. Those remain the job of
playthrough notes, design documentation, and interviews. The tool is a way of
grounding recalled redesign decisions in evidence that is still present in the
artifact, and of catching decisions that were made but never written down.

For a paired corpus, such as an original build and a later port, the useful move
is to run the tool on both and diff the outputs. A component type present in one
and absent in the other is a candidate redesign decision with a file offset
attached to it.

---

## 2. What the tool does, stage by stage

### Stage 1: inventory

Every file under the target is recorded with its size and SHA-256 hash into
`manifest.json`. This is the reproducibility anchor. A reader who obtains the
same build can confirm the hashes match.

### Stage 2: container handling

Unity WebGL builds ship the whole virtual file system as one `.data` file, which
may be stored raw, gzip compressed (`.data.gz`), or brotli compressed
(`.data.br`). The tool detects the encoding from the gzip magic bytes, or by
attempting brotli, and reports which path was taken.

The decompressed payload is a `UnityWebData1.0` archive. The layout is:

| Field | Size | Meaning |
| --- | --- | --- |
| Signature | 16 bytes | NUL-terminated `UnityWebData1.0` |
| Header size | 4 bytes | int32, offset where file payloads begin |
| Per file, repeated until header size is reached | 12 + n bytes | int32 offset, int32 size, int32 path length, path |

The parser walks that table, bounds-checks every offset and size against the
buffer, and skips malformed entries rather than guessing. With `--extract` it
writes the members out, typically `data.unity3d`, `StreamingAssets/*`, and
`Il2CppData/Metadata/global-metadata.dat`.

### Stage 3: IL2CPP metadata

Unity compiles C# to C++ and then to WebAssembly through IL2CPP. Method bodies
are therefore not recoverable as source. What survives in readable form is
`global-metadata.dat`, which holds type names, method names, and the string
literal table.

The tool validates the header by checking the sanity value `0xFAB11BAF`, reads
the format version, and then reads the string literal table properly rather than
by scanning for printable runs. The first six int32 fields of
`Il2CppGlobalMetadataHeader` are sanity, version, string literal offset, string
literal size, string literal data offset, and string literal data size. Each
table entry is a length and a byte index into the literal data block. Every
entry is bounds-checked, and a table that fails validation causes a documented
fallback to raw scanning rather than a silent guess.

This matters for provenance. A string recovered from the literal table gets a
stable locator such as `literal:16`, which is a position in the game's own
string table, not a byte offset that shifts if the file is recompressed.

### Stage 4: serialized asset parsing

Where UnityPy can parse a serialized file or bundle, the tool enumerates objects
and records:

- counts by object type, which gives a rough profile of the build,
- `MonoScript` class and namespace names, which is the list of component and
  script types the build contains, written to `components.csv`,
- `TextAsset` contents, which is where module data and configuration usually
  live,
- names of `GameObject`, `Canvas`, `Animator`, `AudioSource`, `ParticleSystem`,
  and `Camera` objects.

`MonoBehaviour` fields cannot be parsed generically in a player build, because
release players do not ship type trees. The tool therefore falls back to a
structural read described in the next stage.

### Stage 5: string recovery without type trees

Unity serializes a managed string as an int32 length followed by that many UTF-8
bytes, aligned to four. The tool scans for that shape, validates the decode, and
rejects anything that is not plausibly text. This recovers real string fields,
including on-screen UI text, from objects whose schema is unknown.

The method is structural rather than semantic. It shows that a string is stored
in an object. It does not show that the string was displayed. This limit belongs
in the methods section verbatim.

### Stage 6: signature detection

Recovered strings and the text of HTML, JavaScript, JSON, and shader files are
matched against two signature tables: one for Unity and one for generic web and
WebXR programs. Roughly forty Unity signatures and twenty-five web signatures
cover XR toolkits and vendor SDKs, hand and gaze input, teleport and continuous
locomotion, haptics, legacy input, the newer Input System, touch, gamepad,
microphone, UI event plumbing, canvas and raycaster types, text components,
audio, animation, particles, networking, and JavaScript interop.

Each hit becomes a finding with six fields: category, detector name, the matched
evidence, source file, locator, and surrounding context. Findings are
deduplicated on that tuple. Nothing is aggregated into a judgment.

### Stage 7: text corpus

Candidate player-facing strings are filtered against a noise list that removes
asset paths, engine field names prefixed `m_`, framework namespaces, hex blobs,
and identifier-shaped tokens, then written to `text_corpus.csv` with one row per
string. Each row is annotated with word count, which input verbs it contains,
which input-device tokens it contains, which outcome or feedback tokens it
contains, and whether it opens with an imperative input verb. A final empty
`code` column is left for hand coding.

The annotation is a sorting aid for the human coder, not a classification. A
string that mentions "press" is flagged so the coder looks at it sooner. It is
not thereby counted as an instruction.

---

## 3. Why the tool was built this way

**Provenance on every row.** Each finding carries a file and a locator, so any
sentence in a paper can be traced to a byte offset, a literal index, or a
serialized object path ID. Reviewers can ask where a claim came from, and the
answer is a coordinate rather than a recollection.

**Observation separated from interpretation.** Detectors report presence of a
signature. They do not conclude that a game "uses embodied interaction" or that
an interaction was "substituted." That inference is left to the analyst, in
writing, where it can be argued with.

**Theory-neutral category labels.** Categories are named `input.xr_controller`,
`feedback.haptic`, `guidance`, and so on, rather than after any published
framework. A framework can then be applied explicitly through the `--scheme`
option, which takes a JSON object mapping categories to codes; the mapped code
appears in `findings.json` and in the report. Two benefits follow. The mapping
becomes an artifact that can be published and criticized, instead of being baked
into regex names. And the same output can be re-coded under a second framework
without re-running the extraction.

**Graceful degradation, loudly reported.** Optional dependencies are UnityPy for
asset parsing and brotli for `.br` builds. Without them the tool still
inventories, unpacks `UnityWebData`, and extracts strings, and it prints and
records a note saying what it could not do. Silent partial analysis is the
failure mode most likely to produce a wrong claim.

**Refusal to guess.** Every binary parser bounds-checks before reading and
either skips an entry or falls back to a documented weaker method. When the
literal table does not validate, the note says so and the locators change from
`literal:n` to `offset:n`, which signals the weaker provenance to anyone reading
the output.

**Both engines handled.** The same signature and provenance model is applied to
non-Unity WebGL programs through a second table covering WebXR, pointer, touch,
keyboard, gamepad, the Vibration API, Web Audio, and accessibility attributes
such as ARIA roles, tabindex, and alt text. This keeps a non-Unity comparison
case usable without a second toolchain.

---

## 4. Outputs

| File | Contents |
| --- | --- |
| `report.md` | Human-readable report: inventory, asset counts, component types, signature summary and detail, corpus summary, notes and limits |
| `findings.json` | Every finding with category, detector, evidence, source, locator, context, confidence, and mapped code, plus run metadata |
| `text_corpus.csv` | One row per candidate player-facing string, with annotations and an empty `code` column |
| `components.csv` | Script and component type names with occurrence counts |
| `manifest.json` | File inventory with sizes and SHA-256 hashes |
| `extracted/` | Unpacked `UnityWebData` members, when `--extract` is passed |

`text_corpus.csv` columns: `text`, `source`, `locator`, `origin`, `object_type`,
`object_name`, `char_len`, `word_count`, `input_verbs`, `key_tokens`,
`feedback_tokens`, `imperative_opening`, `code`. The `origin` value records how
the string was recovered, one of `il2cpp_string_literal`,
`il2cpp_metadata_scan`, `unity_string`, `monobehaviour`, `textasset`,
`wasm_string`, `html`, or `js_literal`. Origin is the main provenance quality
signal in that file: a literal-table row is stronger evidence than a raw scan
row.

---

## 5. Validation

There is no public corpus of Unity WebGL builds with ground truth, so validation
used synthetic fixtures constructed to the documented byte layouts, with known
contents, generated by `make_fixtures.py`. The point is that every parser was
run against bytes whose correct output was known in advance.

What the fixtures exercise, and the observed results:

| Path exercised | Result |
| --- | --- |
| gzip `.data.gz` | Decompressed, 780 to 1,532 bytes, reported in notes |
| brotli `.data.br` | Decompressed, 563 to 1,363 bytes, reported in notes |
| Uncompressed `.data` | Parsed directly |
| `UnityWebData1.0` table walk | All 3 members located and extracted to the correct relative paths |
| IL2CPP header validation | Sanity matched, format version 29 read |
| IL2CPP literal table parse | All 23 planted literals recovered with correct indices |
| Managed string scan without type trees | Planted UI strings recovered from the serialized blob, for example `Press SPACEBAR to spin the wheel.` at `offset:100` |
| Unity signature detection | 14 categories fired, including `input.xr_controller`, `locomotion.teleport`, `feedback.haptic`, `input.pointer` |
| Web signature detection | 10 categories fired on the non-Unity fixture, including WebXR, touch, keyboard, Vibration API, Web Audio, ARIA |
| Corpus annotation | Instruction strings flagged with the correct verbs and device tokens, for example `press`, `pick` and `left mouse`, `mouse button` |
| Missing UnityPy and brotli | Ran to completion, printed and recorded both limitations |
| Single-file target | Ran against one `.data.gz` alone |
| `--scheme` mapping | Categories rendered with the supplied codes in the report |
| `--min-words` threshold | Corpus reduced from 15 rows to 11 rows at a threshold of 4 |

Performance was measured on 20 MB of random data: the managed-string scan ran in
0.82 seconds and the printable-run scan in 0.49 seconds, so a 100 MB build costs
a few seconds of scanning.

What validation does not establish: that the tool behaves correctly against a
real build from any particular Unity version. Before the outputs support a
published claim, spot-check a sample of findings by hand against the actual
build, and say in the methods section that this was done.

---

## 6. Limits and threats to validity

These are written to be reusable in a limitations paragraph.

**Reference is not execution.** A detected API or component type shows that the
build references it. It does not show that the code path ran, or ran for any
particular player. All counts are counts of static signatures, not of runtime
events.

**Absence is weak evidence.** Managed code stripping above the low setting,
engine code stripping, custom wrappers around an API, or an obfuscated metadata
file can each hide a real dependency. A category with zero hits should be
reported as not detected rather than as absent.

**Storage is not display.** A string recovered from a serialized object or a
literal table is present in the build. Whether it was ever shown to a player,
and in what context, requires playing the build.

**IL2CPP removes the source.** Method bodies are compiled to WebAssembly. The
tool reports the vocabulary of the build, meaning which types and APIs are
referenced, not its logic. Any claim about how an interaction was implemented
needs the project source or the design record, not this output.

**Heuristics are labeled.** The noise filter, the verb and device lexicons, and
the imperative check are heuristics that sort strings for a human. They will
admit engine strings and will miss instructions phrased without a listed verb.
Findings carry a `confidence` field; anything not marked `observed` should be
verified by hand before it appears in a claim.

**Regexes are a moving target.** Signature tables were written against current
Unity API naming. A build from a much older or newer version may use names the
tables do not contain. The tables are plain data at the top of the file and
should be reported as part of the method, with the tool version recorded.

**Third-party format documentation.** The `UnityWebData` and IL2CPP layouts are
not published by Unity. They are documented by independent reimplementations
that agree with one another. That agreement is the basis for trusting them, and
it is the reason the parsers validate rather than assume.

---

## 7. Reproducibility checklist

- `findings.json` records the tool version, the exact command line, the target
  path, whether UnityPy and brotli were available, and every note emitted.
- `manifest.json` records SHA-256 for every input file.
- Environment used for validation: Python 3.12.3, UnityPy 1.25.3, brotli via
  PyPI.
- Pin the tool version and the signature table in the methods section, since
  changing a regex changes the counts.

---

## 8. A draft methods paragraph

Adapt rather than paste.

> Each build was analyzed with a purpose-written static analyzer
> (`build_analyzer.py`, version 1.0.0). The tool inventories and hashes every
> file in a build, decompresses the WebGL data file, unpacks the
> `UnityWebData1.0` container, validates and reads the IL2CPP metadata string
> literal table, recovers serialized string fields by their length-prefixed
> layout, and matches recovered strings against a table of signatures for input,
> feedback, locomotion, and interface APIs. Every match is recorded with its
> source file and a locator, either a literal index, a byte offset, or a
> serialized object path ID, so that each observation can be traced back to the
> artifact. Detector categories are deliberately independent of any interaction
> framework; the mapping from categories to the coding scheme used here is
> supplied as a separate JSON file and is reported in full. The tool reports
> references, not executions: a detected API indicates that the build contains
> it, not that a player encountered it, and stripped or wrapped code may hide a
> real dependency, so absence of a signature is reported as non-detection. All
> tool output was used as a starting point for hand coding rather than as
> coding, and a sample of findings was verified against the builds directly.

---

## 9. Sources for the format claims

- `UnityWebData1.0` layout, signature, header size field, and per-file offset,
  size, and path-length triples: https://github.com/akio7624/uwdtool
- Independent implementation agreeing with the same layout:
  https://github.com/ptr-yudai/UnityUnpacker/blob/master/ExtractUnityWeb.py
- Third implementation confirming the 16-byte NUL-terminated magic and the
  start-offset field: https://pkg.go.dev/github.com/jozsefsallai/unityweb
- IL2CPP metadata sanity value `0xFAB11BAF` and its role as an
  obfuscation check: https://www.hebunilhanli.com/wonderland/mobile-security/analysis-of-il2cpp/
- `Il2CppGlobalMetadataHeader` field order, including the string literal offset,
  size, data offset, and data size fields:
  https://acquykhud.github.io/2021/12/27/ISITDTU-Final-2021.html
- IL2CPP string literals stored as a length-and-index table plus a packed data
  block, and the caveat that no reliable signal separates player-facing strings
  from framework strings:
  https://github.com/jozsefsallai/il2cpp-stringliteral-patcher
- UnityPy, the asset parsing dependency: https://github.com/K0lb3/UnityPy
