"""Evaluate labeled StreamFrame predictions without third-party dependencies.

Usage: python -m streamframe.evaluate INPUT.json [--iou-thresholds 0.5,0.75]

Input is a JSON object with a nonempty "samples" array. Each sample has a
unique string "id" and at least one of:

  "transcript": {"reference": [{"word": "hello", "start": 0, "end": 0.4}],
                 "prediction": [{"word": "hello", "start": 0, "end": 0.5}]}
  "localization": {"reference": [{"start": 0, "end": 2}],
                   "prediction": [{"start": 0.1, "end": 2.1}]}
  "face_crops": [{"reference": [0, 0, 10, 10],
                  "prediction": [1, 1, 9, 9]}]

Transcript timestamps are optional, but start and end must appear together.
Words are compared exactly, including case; tokenization is the caller's
responsibility. Per-sample alignments give zero-based reference and prediction
indices (null for insertions or deletions). Localization predictions are ranked
in array order. Face boxes
are [x1, y1, x2, y2]; each entry is an explicitly paired ground truth and
prediction, or a null prediction for a missed crop. Times and boundary errors
are in seconds. Face box coordinates must use the same coordinate system.

Temporal recall uses maximum-cardinality one-to-one matching at each threshold.
Precision@K uses the first K predictions and the same matching rule, divided
by the number returned (up to K), or null if none were returned. Duplicates
are unmatched predictions that overlap a reference at the threshold. Boundary
errors are measured on the resulting matched pairs; ties are deterministic.
Face containment is intersection area divided by ground-truth area. Empty
denominators are reported as null. Aggregate metrics pool counts and errors
across samples rather than averaging sample-level rates.
"""

import argparse
import json
import math
import sys
import time


class InputError(ValueError):
    """An input or configuration error with a location."""


def _object(value, path, required, optional=()):
    if not isinstance(value, dict):
        raise InputError(f"{path}: expected an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing:
        raise InputError(f"{path}: missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise InputError(f"{path}: unknown fields: {', '.join(sorted(extra))}")
    return value


def _array(value, path):
    if not isinstance(value, list):
        raise InputError(f"{path}: expected an array")
    return value


def _number(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{path}: expected a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise InputError(f"{path}: expected a finite number")
    return value


def _interval(value, path, word=False):
    required = ("word",) if word else ("start", "end")
    optional = ("start", "end") if word else ()
    obj = _object(value, path, required, optional)
    if word:
        token = obj["word"]
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise InputError(f"{path}.word: expected a nonempty single word")
        if ("start" in obj) != ("end" in obj):
            raise InputError(f"{path}: start and end must appear together")
        if "start" not in obj:
            return
    start = _number(obj["start"], f"{path}.start")
    end = _number(obj["end"], f"{path}.end")
    if start < 0 or end < start or (not word and end == start):
        raise InputError(f"{path}: expected 0 <= start {'<=' if word else '<'} end")


def _box(value, path):
    if not isinstance(value, list) or len(value) != 4:
        raise InputError(f"{path}: expected [x1, y1, x2, y2]")
    coords = [_number(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if coords[0] >= coords[2] or coords[1] >= coords[3]:
        raise InputError(f"{path}: expected x1 < x2 and y1 < y2")
    try:
        width, height = coords[2] - coords[0], coords[3] - coords[1]
        area = width * height
        if not math.isfinite(width) or not math.isfinite(height) or not math.isfinite(area) or area == 0:
            raise InputError(f"{path}: box dimensions and area must be representable finite numbers")
    except OverflowError as exc:
        raise InputError(f"{path}: box dimensions and area must be representable finite numbers") from exc


def validate(data):
    """Validate the documented JSON input, raising InputError on errors."""
    _object(data, "$", ("samples",))
    samples = _array(data["samples"], "$.samples")
    if not samples:
        raise InputError("$.samples: expected at least one sample")
    ids = set()
    for i, sample in enumerate(samples):
        path = f"$.samples[{i}]"
        _object(sample, path, ("id",), ("transcript", "localization", "face_crops"))
        name = sample["id"]
        if not isinstance(name, str) or not name.strip():
            raise InputError(f"{path}.id: expected a nonempty string")
        if name in ids:
            raise InputError(f"{path}.id: duplicate sample id {name!r}")
        ids.add(name)
        if not any(key in sample for key in ("transcript", "localization", "face_crops")):
            raise InputError(f"{path}: expected at least one evaluation component")
        for component in ("transcript", "localization"):
            if component not in sample:
                continue
            section = sample[component]
            loc = f"{path}.{component}"
            _object(section, loc, ("reference", "prediction"))
            for side in ("reference", "prediction"):
                arr = _array(section[side], f"{loc}.{side}")
                for j, item in enumerate(arr):
                    _interval(item, f"{loc}.{side}[{j}]", word=component == "transcript")
        if "face_crops" in sample:
            for j, pair in enumerate(_array(sample["face_crops"], f"{path}.face_crops")):
                loc = f"{path}.face_crops[{j}]"
                _object(pair, loc, ("reference", "prediction"))
                _box(pair["reference"], f"{loc}.reference")
                if pair["prediction"] is not None:
                    _box(pair["prediction"], f"{loc}.prediction")
    return data


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _transcript(reference, prediction):
    # Backtracking prefers exact matches, then substitutions, deletions, insertions.
    n, m = len(reference), len(prediction)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = reference[i - 1]["word"] != prediction[j - 1]["word"]
            dp[i][j] = min(dp[i - 1][j - 1] + cost, dp[i - 1][j] + 1, dp[i][j - 1] + 1)
    result = {"reference_words": n, "prediction_words": m, "substitutions": 0,
              "deletions": 0, "insertions": 0, "matched_words": 0,
              "timed_matches": 0, "start_error_sum": 0.0, "end_error_sum": 0.0}
    i, j, alignment = n, m, []
    while i or j:
        if i and j and reference[i - 1]["word"] == prediction[j - 1]["word"] and dp[i][j] == dp[i - 1][j - 1]:
            alignment.append({"operation": "match", "reference_index": i - 1, "prediction_index": j - 1})
            r, p = reference[i - 1], prediction[j - 1]
            result["matched_words"] += 1
            if "start" in r and "start" in p:
                result["timed_matches"] += 1
                result["start_error_sum"] += abs(r["start"] - p["start"])
                result["end_error_sum"] += abs(r["end"] - p["end"])
            i, j = i - 1, j - 1
        elif i and j and dp[i][j] == dp[i - 1][j - 1] + 1:
            alignment.append({"operation": "substitution", "reference_index": i - 1, "prediction_index": j - 1})
            result["substitutions"] += 1
            i, j = i - 1, j - 1
        elif i and dp[i][j] == dp[i - 1][j] + 1:
            alignment.append({"operation": "deletion", "reference_index": i - 1, "prediction_index": None})
            result["deletions"] += 1
            i -= 1
        else:
            alignment.append({"operation": "insertion", "reference_index": None, "prediction_index": j - 1})
            result["insertions"] += 1
            j -= 1
    return result, list(reversed(alignment))


def _finish_transcript(result):
    result = result.copy()
    result["wer"] = _ratio(result["substitutions"] + result["deletions"] + result["insertions"], result["reference_words"])
    result["mean_start_absolute_error"] = _ratio(result["start_error_sum"], result["timed_matches"])
    result["mean_end_absolute_error"] = _ratio(result["end_error_sum"], result["timed_matches"])
    return result


def _temporal_iou(a, b):
    intersection = max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    if not intersection:
        return 0.0
    return intersection / (max(a["end"], b["end"]) - min(a["start"], b["start"]))


def _matching(reference, prediction, threshold):
    # An augmenting-path matching maximizes cardinality; higher IoU edges first
    # make the chosen alignment reproducible without claiming optimal total IoU.
    edges = []
    for p in prediction:
        candidates = [(i, _temporal_iou(r, p)) for i, r in enumerate(reference)]
        edges.append([i for i, score in sorted(candidates, key=lambda x: (-x[1], x[0])) if score >= threshold])
    owners, assigned = {}, {}
    for p in range(len(prediction)):
        queue, seen_predictions, parents = [p], {p}, {}
        free = None
        for candidate in queue:
            for r in edges[candidate]:
                if r in parents:
                    continue
                parents[r] = candidate
                if r not in owners:
                    free = r
                    break
                previous = owners[r]
                if previous not in seen_predictions:
                    seen_predictions.add(previous)
                    queue.append(previous)
            if free is not None:
                break
        while free is not None:
            candidate = parents[free]
            old_ref = assigned.get(candidate)
            owners[free] = candidate
            assigned[candidate] = free
            free = old_ref
    return owners, edges


def _localization(reference, prediction, threshold, top_k):
    owners, edges = _matching(reference, prediction, threshold)
    top_owners, _ = _matching(reference, prediction[:top_k], threshold)
    matched_predictions = set(owners.values())
    return {
        "reference_events": len(reference), "predictions": len(prediction),
        "matched_events": len(owners),
        "duplicate_predictions": sum(bool(edge) and i not in matched_predictions for i, edge in enumerate(edges)),
        "top_k_predictions": min(top_k, len(prediction)),
        "top_k_matches": len(top_owners),
        "start_error_sum": sum(abs(reference[r]["start"] - prediction[p]["start"]) for r, p in owners.items()),
        "end_error_sum": sum(abs(reference[r]["end"] - prediction[p]["end"]) for r, p in owners.items()),
    }


def _finish_localization(result):
    result = result.copy()
    result["recall"] = _ratio(result["matched_events"], result["reference_events"])
    result["precision_at_k"] = _ratio(result["top_k_matches"], result["top_k_predictions"])
    result["mean_start_absolute_error"] = _ratio(result["start_error_sum"], result["matched_events"])
    result["mean_end_absolute_error"] = _ratio(result["end_error_sum"], result["matched_events"])
    return result


def _faces(pairs):
    result = {"ground_truth_faces": len(pairs), "predicted_crops": 0,
              "iou_sum": 0.0, "containment_sum": 0.0}
    for pair in pairs:
        a, b = pair["reference"], pair["prediction"]
        if b is None:
            continue
        result["predicted_crops"] += 1
        intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        area_b = (b[2] - b[0]) * (b[3] - b[1])
        scale = max(area_a, area_b)
        scaled_intersection = intersection / scale
        result["iou_sum"] += scaled_intersection / (area_a / scale + area_b / scale - scaled_intersection)
        result["containment_sum"] += intersection / area_a
    return result


def _finish_faces(result):
    result = result.copy()
    result["mean_iou"] = _ratio(result["iou_sum"], result["ground_truth_faces"])
    result["mean_containment"] = _ratio(result["containment_sum"], result["ground_truth_faces"])
    return result


def _add(total, values):
    for key, value in values.items():
        total[key] = total.get(key, 0) + value
        try:
            finite = math.isfinite(total[key])
        except OverflowError:
            finite = False
        if not finite:
            raise InputError(f"metric {key}: aggregate exceeds finite numeric range")


def evaluate(data, thresholds=(0.5, 0.75), top_k=5):
    """Return per-sample and pooled metrics for validated JSON-compatible data."""
    validate(data)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise InputError("top_k: expected a positive integer")
    try:
        thresholds = list(thresholds)
    except TypeError as exc:
        raise InputError("thresholds: expected a nonempty list of numbers") from exc
    if not thresholds:
        raise InputError("thresholds: expected at least one threshold")
    for value in thresholds:
        _number(value, "threshold")
        if not 0 < value <= 1:
            raise InputError("threshold: expected a number in (0, 1]")
    if len(set(thresholds)) != len(thresholds):
        raise InputError("thresholds: duplicate threshold")
    keys = [str(float(t)) for t in thresholds]
    samples, totals = [], {}
    for sample in data["samples"]:
        output = {"id": sample["id"]}
        if "transcript" in sample:
            section = sample["transcript"]
            values, alignment = _transcript(section["reference"], section["prediction"])
            _add(totals.setdefault("transcript", {}), values)
            output["transcript"] = _finish_transcript(values)
            output["transcript"]["alignment"] = alignment
        if "localization" in sample:
            section = sample["localization"]
            output["localization"] = {}
            for threshold, key in zip(thresholds, keys):
                values = _localization(section["reference"], section["prediction"], threshold, top_k)
                _add(totals.setdefault("localization", {}).setdefault(key, {}), values)
                output["localization"][key] = _finish_localization(values)
        if "face_crops" in sample:
            values = _faces(sample["face_crops"])
            _add(totals.setdefault("face_crops", {}), values)
            output["face_crops"] = _finish_faces(values)
        samples.append(output)
    aggregate = {}
    if "transcript" in totals:
        aggregate["transcript"] = _finish_transcript(totals["transcript"])
    if "localization" in totals:
        aggregate["localization"] = {key: _finish_localization(value) for key, value in totals["localization"].items()}
    if "face_crops" in totals:
        aggregate["face_crops"] = _finish_faces(totals["face_crops"])
    return {"configuration": {"iou_thresholds": thresholds, "top_k": top_k},
            "samples": samples, "aggregate": aggregate}


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise InputError(f"invalid JSON number: {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="path to labeled predictions JSON, or - for stdin")
    parser.add_argument("--iou-thresholds", default="0.5,0.75",
                        help="comma-separated temporal IoU thresholds in (0, 1] (default: 0.5,0.75)")
    parser.add_argument("--top-k", type=int, default=5,
                        help="number of ranked candidates for precision@K (default: 5)")
    parser.add_argument("--measure-resources", action="store_true",
                        help="include nondeterministic evaluation wall time and process peak RSS when available")
    args = parser.parse_args(argv)
    start = time.perf_counter()
    try:
        thresholds = [float(t) for t in args.iou_thresholds.split(",")]
        if args.input == "-":
            data = json.load(sys.stdin, object_pairs_hook=_pairs_no_duplicates, parse_constant=_invalid_constant)
        else:
            with open(args.input, encoding="utf-8") as file:
                data = json.load(file, object_pairs_hook=_pairs_no_duplicates, parse_constant=_invalid_constant)
        result = evaluate(data, thresholds, args.top_k)
        if args.measure_resources:
            resources = {"wall_seconds": time.perf_counter() - start}
            try:
                import resource
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # macOS reports bytes, Linux reports KiB; leave other platforms explicit.
                resources["peak_rss"] = rss
                resources["peak_rss_unit"] = "bytes" if sys.platform == "darwin" else "KiB" if sys.platform.startswith("linux") else "platform-dependent"
            except ImportError:
                resources["peak_rss"] = None
                resources["peak_rss_unit"] = None
            result["resources"] = resources
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, UnicodeError, json.JSONDecodeError, InputError, ValueError, OverflowError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
