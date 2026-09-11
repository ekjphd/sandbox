#!/usr/bin/env python3
"""
compare_runs.py

Diff two build_analyzer.py runs. Written for paired corpora, for example an
original build and a later port of the same game.

Usage:
  python3 compare_runs.py <run_a_dir> <run_b_dir> [-o comparison.md] [--csv diff.csv]

Reports, in both directions:
  - categories present in one run and not the other
  - detectors present in one run and not the other
  - component types present in one run and not the other
  - counts of instruction-bearing strings in each run

Every row is a candidate for hand inspection, not a finding. Absence in one run
can mean a real design change or it can mean the tool could not read that build.
Read the notes section of both reports before treating any row as a change.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def load_run(d: Path) -> dict:
    f = d / "findings.json"
    if not f.exists():
        raise SystemExit(f"error: {f} not found. Point at a build_analyzer output directory.")
    data = json.loads(f.read_text(encoding="utf-8"))

    comps: dict[str, int] = {}
    cf = d / "components.csv"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                comps[row["component_type"]] = int(row["occurrences"])

    corpus = []
    tf = d / "text_corpus.csv"
    if tf.exists():
        with tf.open(encoding="utf-8") as fh:
            corpus = list(csv.DictReader(fh))

    cats: dict[str, int] = {}
    dets: dict[str, int] = {}
    for fd in data["findings"]:
        cats[fd["category"]] = cats.get(fd["category"], 0) + 1
        dets[fd["detector"]] = dets.get(fd["detector"], 0) + 1

    return {
        "dir": str(d),
        "target": data.get("target", ""),
        "notes": data.get("notes", []),
        "categories": cats,
        "detectors": dets,
        "components": comps,
        "corpus": corpus,
    }


def instruction_rows(corpus: list[dict]) -> list[dict]:
    return [r for r in corpus if r.get("input_verbs") or r.get("key_tokens")]


def section(title: str, a_label: str, b_label: str,
            a: dict[str, int], b: dict[str, int]) -> list[str]:
    out = [f"## {title}", ""]
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    both = sorted(set(a) & set(b))

    out.append(f"### Only in {a_label}")
    out.append("")
    if only_a:
        out.append("| Item | Hits |")
        out.append("| --- | ---: |")
        out += [f"| `{k}` | {a[k]} |" for k in only_a]
    else:
        out.append("None.")
    out.append("")

    out.append(f"### Only in {b_label}")
    out.append("")
    if only_b:
        out.append("| Item | Hits |")
        out.append("| --- | ---: |")
        out += [f"| `{k}` | {b[k]} |" for k in only_b]
    else:
        out.append("None.")
    out.append("")

    changed = [(k, a[k], b[k]) for k in both if a[k] != b[k]]
    out.append("### In both, different counts")
    out.append("")
    if changed:
        out.append(f"| Item | {a_label} | {b_label} |")
        out.append("| --- | ---: | ---: |")
        out += [f"| `{k}` | {x} | {y} |" for k, x, y in sorted(changed)]
    else:
        out.append("None.")
    out.append("")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Diff two build_analyzer.py runs.")
    ap.add_argument("run_a")
    ap.add_argument("run_b")
    ap.add_argument("-o", "--out", default="comparison.md")
    ap.add_argument("--csv", default=None, help="also write a flat diff CSV")
    ap.add_argument("--label-a", default=None)
    ap.add_argument("--label-b", default=None)
    args = ap.parse_args()

    a = load_run(Path(args.run_a))
    b = load_run(Path(args.run_b))
    la = args.label_a or Path(args.run_a).name
    lb = args.label_b or Path(args.run_b).name

    lines = [f"# Comparison: {la} against {lb}", ""]
    lines += [f"- {la}: `{a['target']}`", f"- {lb}: `{b['target']}`", ""]
    lines += ["Rows here are candidates for inspection, not findings. Check the notes",
              "in both reports before reading an absence as a design change.", ""]

    lines += section("Interaction categories", la, lb, a["categories"], b["categories"])
    lines += section("Detectors", la, lb, a["detectors"], b["detectors"])
    if a["components"] or b["components"]:
        lines += section("Component and script types", la, lb, a["components"], b["components"])

    ia, ib = instruction_rows(a["corpus"]), instruction_rows(b["corpus"])
    lines += ["## Text corpus", "",
              "| Measure | " + la + " | " + lb + " |",
              "| --- | ---: | ---: |",
              f"| Total strings | {len(a['corpus'])} | {len(b['corpus'])} |",
              f"| Instruction-bearing strings | {len(ia)} | {len(ib)} |", ""]

    ta = {r["text"] for r in ia}
    tb = {r["text"] for r in ib}
    for label, only in ((la, sorted(ta - tb)), (lb, sorted(tb - ta))):
        lines.append(f"### Instruction strings only in {label}")
        lines.append("")
        lines += [f"- {t}" for t in only[:60]] or ["None."]
        if len(only) > 60:
            lines.append(f"- ... {len(only) - 60} more")
        lines.append("")

    lines += ["## Notes from each run", ""]
    for label, run in ((la, a), (lb, b)):
        lines.append(f"**{label}**")
        lines.append("")
        lines += [f"- {n}" for n in run["notes"]] or ["- none"]
        lines.append("")

    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["kind", "item", f"{la}_count", f"{lb}_count", "status"])
            for kind, da, db in (("category", a["categories"], b["categories"]),
                                 ("detector", a["detectors"], b["detectors"]),
                                 ("component", a["components"], b["components"])):
                for k in sorted(set(da) | set(db)):
                    x, y = da.get(k, 0), db.get(k, 0)
                    status = ("only_a" if y == 0 else "only_b" if x == 0
                              else "same" if x == y else "changed")
                    w.writerow([kind, k, x, y, status])

    print(f"wrote {args.out}" + (f" and {args.csv}" if args.csv else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
