#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["tiktoken==0.12.0"]
# ///
"""Offline candidate detection; never execute commands from the input history.

Input: a session-history fetch snapshot. Output: a local JSON report containing
commands (potentially private). Compare NCD with token SequenceMatcher using an
explicit, narrow oracle: identical pytest invocations except output paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import shlex
import time
import zlib

import tiktoken


OUTPUT = re.compile(r"(?P<key>--(?:basetemp|junitxml))=[^\s]+")
LIMITS = {"shell": None, "login": True, "memory_max_mib": 8192,
          "timeout_seconds": 21600, "workdir": None}


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_calls(path, limit, max_bytes):
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    if snapshot.get("schema") == "template-similarity/v1":
        if (limit, max_bytes) != (snapshot["config"]["limit"], snapshot["config"]["max_command_bytes"]):
            raise ValueError("replay requires the original selection limits")
        return snapshot["calls"], snapshot["input"]
    calls, env = [], {}
    for turn in snapshot["thread"]["turns"]:
        for item in turn.get("items", []):
            if item.get("server") != "blocking-shell":
                continue
            args = item.get("arguments") or {}
            tool = item.get("tool")
            if tool == "set_env" and not item.get("error"):
                env.update(args["values"])
            elif tool == "unset_env" and not item.get("error"):
                for name in args["names"]:
                    env.pop(name, None)
            elif tool == "run" and isinstance(args.get("cmd"), str):
                context = {k: args.get(k, v) for k, v in LIMITS.items()}
                context["env"] = dict(env)
                calls.append({"id": item["id"], "cmd": args["cmd"], "arguments": args,
                              "context": context})
    selected = calls[-limit:]
    # Do not truncate commands: doing so can create false matches.
    kept = [c for c in selected if len(c["cmd"].encode()) <= max_bytes]
    meta = {"captured_at": snapshot["captured_at"],
            "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "total_run_calls": len(calls), "selected": len(selected),
            "oversized_excluded": len(selected) - len(kept)}
    return kept, meta


def oracle(command):
    """Deliberately strict benchmark labels, not a general semantic classifier."""
    if "<<" in command or "python3 -m pytest " not in command:
        return None
    keys = [m["key"] for m in OUTPUT.finditer(command)]
    if Counter(keys) != Counter(["--basetemp", "--junitxml"]):
        return None
    return OUTPUT.sub(lambda m: m["key"] + "=<output>", command)


def compressed(data):
    compressor = zlib.compressobj(level=6, wbits=-15)
    return len(compressor.compress(data) + compressor.flush())


def distances(calls, window):
    encoded = [c["cmd"].encode() for c in calls]
    sizes = [compressed(c) for c in encoded]
    tokens = []
    for call in calls:
        try:
            tokens.append(shlex.split(call["cmd"]))
        except ValueError:
            tokens.append(call["cmd"].split())
    pairs, elapsed = [], {"ncd": 0.0, "tokens": 0.0}
    for j, right in enumerate(encoded):
        for i in range(max(0, j - window), j):
            left = encoded[i]
            start = time.perf_counter()
            ncd = 0.0 if left == right else (
                min(compressed(left + b"\0" + right),
                    compressed(right + b"\0" + left)) - min(sizes[i], sizes[j])
            ) / max(sizes[i], sizes[j])
            elapsed["ncd"] += time.perf_counter() - start
            start = time.perf_counter()
            similarity = SequenceMatcher(None, tokens[i], tokens[j], autojunk=False)
            td = 1 - similarity.ratio()
            elapsed["tokens"] += time.perf_counter() - start
            a, b = oracle(calls[i]["cmd"]), oracle(calls[j]["cmd"])
            same_context = calls[i]["context"] == calls[j]["context"]
            label = None if a is None or b is None else a == b and same_context
            pairs.append({"i": i, "j": j, "ncd": ncd, "tokens": td,
                          "same_context": same_context, "label": label,
                          "exact": left == right})
    return pairs, elapsed


def metrics(pairs, method, threshold):
    counts = Counter()
    for pair in pairs:
        if pair["label"] is None or pair["exact"]:
            continue
        predicted = pair[method] <= threshold and pair["same_context"]
        counts["tp" if predicted and pair["label"] else
               "fp" if predicted else "fn" if pair["label"] else "tn"] += 1
    tp, fp, fn = (counts[k] for k in ("tp", "fp", "fn"))
    return {k: counts[k] for k in ("tp", "fp", "fn", "tn")} | {
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0}


def proposal(commands):
    """Lossless textual proposal only; shell execution still requires review.

    Keep whitespace and fixed tokens verbatim. Only differing --option=/path
    tokens become slots. Reject all other differences, including control flow.
    """
    pieces = [re.split(r"(\s+)", command) for command in commands]
    if len({len(row) for row in pieces}) != 1:
        return None
    segments, values = [], [{} for _ in commands]
    for column in zip(*pieces):
        if len(set(column)) == 1:
            segments.append(column[0])
            continue
        matches = [re.fullmatch(r"(--[\w-]+)=(/[^\s'\"]+)", part)
                   for part in column]
        if not all(matches) or len({m[1] for m in matches if m}) != 1:
            return None
        slot = "p" + str(len(values[0]) + 1)
        prefix = next(m[1] for m in matches if m) + "="
        segments.extend([prefix, {"slot": slot}])
        for row, match in zip(values, matches):
            assert match is not None
            row[slot] = match[2]
    if not values[0]:
        return None
    template = {"name": "candidate", "segments": segments,
                "review_required": True}
    for command, row in zip(commands, values):
        reconstructed = "".join(s if isinstance(s, str) else row[s["slot"]]
                                for s in segments)
        assert reconstructed == command
    return {"template": template, "values": values, "roundtrip": True}


def candidates(calls, pairs, method, threshold):
    groups = []
    for j in range(len(calls)):
        eligible = [p for p in pairs if p["j"] == j and p["same_context"]
                    and p[method] <= threshold]
        if not eligible:
            groups.append([j])
            continue
        nearest = min(eligible, key=lambda p: p[method])["i"]
        group = next(g for g in groups if nearest in g)
        # Every member must match; avoid single-link chaining between families.
        if all(any(p["i"] == member for p in eligible) for member in group):
            group.append(j)
        else:
            groups.append([j])
    result = []
    for group in sorted(groups, key=len, reverse=True):
        if len(group) < 3:
            continue
        commands = [calls[i]["cmd"] for i in group]
        plan = proposal(commands)
        savings = None
        if plan:
            plan["template"]["execution"] = calls[group[0]]["context"]
            encoding = tiktoken.get_encoding("o200k_base")
            instruction = ("Review this candidate before use. Values are literal paths, "
                           "not shell code. Fixed shell segments and execution settings "
                           "must be preserved. This offline proposal does not execute.")
            definition_tokens = len(encoding.encode(serialize(plan["template"])))
            instruction_tokens = len(encoding.encode(instruction))
            original_tokens = sum(len(encoding.encode(serialize(calls[i]["arguments"])))
                                  for i in group)
            new_calls = []
            for i, row in zip(group, plan["values"]):
                invocation = {k: v for k, v in calls[i]["arguments"].items()
                              if k != "cmd" and k not in LIMITS}
                invocation.update(template="candidate", values=row)
                new_calls.append(invocation)
            call_tokens = sum(len(encoding.encode(serialize(c))) for c in new_calls)
            after = definition_tokens + instruction_tokens + call_tokens
            savings = {"encoding": "o200k_base", "json": "compact, ensure_ascii=False",
                       "before": original_tokens, "definition": definition_tokens,
                       "instructions": instruction_tokens, "calls": call_tokens,
                       "after": after, "saved": original_tokens - after,
                       "saved_fraction": (original_tokens - after) / original_tokens,
                       "scope": "retrospective argument JSON only; definition and instruction charged once; "
                                "excludes tool schema, responses and context replay; not realized savings"}
            # Ensure the measured wire data can actually be round-tripped as JSON.
            assert json.loads(serialize(new_calls)) == new_calls
        result.append({"indices": group, "count": len(group),
                       "distinct_commands": len(set(commands)),
                       "proposal": plan, "token_savings": savings,
                       "recommend": savings is not None and savings["saved"] > 0,
                       "examples": commands[:2]})
    return result


def stress_cases(command):
    return {
        "output_only": OUTPUT.sub(lambda m: m["key"] + "=/new/output/" +
                                  ("native" if m["key"] == "--basetemp" else "report/junit.xml"), command),
        "collect_only": command + " --collect-only",
        "environment": "AMD_DEBUG=nodccclear " + command,
        "different_test": command.replace("test_comparison", "test_handoff_trace"),
        "ignore_failure": command + " || true",
        "dry_run_removed": ("rsync -a --dry-run ./source/ ./destination/",
                            "rsync -a ./source/ ./destination/"),
        "different_workdir": command,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--window", type=int, default=50)
    parser.add_argument("--max-command-bytes", type=int, default=16384)
    args = parser.parse_args()
    if not 4 <= args.limit <= 1000 or not 1 <= args.window <= 100:
        parser.error("limit must be 4..1000 and window 1..100")
    if not 1 <= args.max_command_bytes <= 16384:
        parser.error("max-command-bytes must be 1..16384 (DEFLATE window)")
    calls, metadata = load_calls(args.snapshot, args.limit, args.max_command_bytes)
    if len(calls) < 4:
        parser.error("need at least four eligible calls")
    pairs, elapsed = distances(calls, args.window)
    split = len(calls) // 2
    for label, selected in (("training", [p for p in pairs if p["j"] < split]),
                            ("evaluation", [p for p in pairs if p["j"] >= split])):
        if not any(p["label"] is True and not p["exact"] for p in selected):
            parser.error(f"no nonidentical positive pairs in {label}; benchmark unavailable")
    report = {"schema": "template-similarity/v1", "input": metadata, "config": vars(args) | {
        "snapshot": None, "out": None, "compressor": "raw DEFLATE level 6",
        "zlib_version": zlib.ZLIB_RUNTIME_VERSION, "train_calls": split,
        "tiktoken_version": tiktoken.__version__,
        "oracle": "exact command except basetemp/junitxml; same execution context",
        "exact_duplicates_excluded_from_metrics": True},
        "calls": calls, "pairs": len(pairs), "methods": {}}
    thresholds = [i / 100 for i in range(0, 61, 2)]
    for method in ("ncd", "tokens"):
        train = [p for p in pairs if p["j"] < split]
        test = [p for p in pairs if p["j"] >= split]
        sweep = [{"threshold": t, **metrics(train, method, t)} for t in thresholds]
        best = max(sweep, key=lambda row: (row["f1"], -row["threshold"]))
        threshold = best["threshold"]
        groups = candidates(calls, pairs, method, threshold)
        errors = sorted([p for p in test if p["label"] is False
                         and not p["exact"] and p["same_context"]
                         and p[method] <= threshold], key=lambda p: p[method])[:10]
        report["methods"][method] = {
            "threshold": threshold, "train": best,
            "test": metrics(test, method, threshold),
            "seconds": elapsed[method], "train_sweep": sweep,
            "false_positive_examples": errors, "groups": groups,
            "savings": [{"indices": g["indices"], **g["token_savings"]}
                        for g in groups if g["token_savings"]]}
    source = next((c for c in calls if "test_comparison.py" in c["cmd"]
                   and c["cmd"].startswith("python3 -m pytest ")), None)
    report["stress"] = []
    if source:
        for name, variant in stress_cases(source["cmd"]).items():
            original, changed = variant if isinstance(variant, tuple) else (source["cmd"], variant)
            context = dict(source["context"])
            if name == "different_workdir":
                context["workdir"] = "/different/checkout"
            sample = [{"cmd": original, "context": source["context"]},
                      {"cmd": changed, "context": context}]
            pair = distances(sample, 1)[0][0]
            report["stress"].append({"name": name, "ncd": pair["ncd"],
                "tokens": pair["tokens"], "same_context": pair["same_context"],
                "proposal": proposal([original, changed]) is not None})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(serialize({"input": metadata, "pairs": len(pairs), "methods": {
        k: {f: v[f] for f in ("threshold", "test", "seconds", "savings")} |
        {"groups": len(v["groups"]), "proposals": sum(g["proposal"] is not None
         for g in v["groups"])} for k, v in report["methods"].items()},
        "stress": report["stress"]}))


if __name__ == "__main__":
    main()
