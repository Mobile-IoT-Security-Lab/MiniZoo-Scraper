#!/usr/bin/env python3
"""Local Tesseract OCR with the same pixel-coordinate JSON as the macOS adapter.

Requires Tesseract, eng/chi_sim language data and Pillow (Docker requirements,
Python 3.11+). OCR assists UI navigation only; app identity still requires
runtime package evidence.
"""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def line_text(words):
    """Tesseract separates Chinese words; adjacent Han characters need no space."""
    text = words[0][0]
    for previous, word in zip(words, words[1:]):
        a, b = previous[2], word[2]
        adjacent_han = ("\u3400" <= previous[0][-1] <= "\u9fff"
                        and "\u3400" <= word[0][0] <= "\u9fff"
                        and 0 <= b[0] - a[2] < min(a[3] - a[1], b[3] - b[1]) / 2)
        text += ("" if adjacent_han else " ") + word[0]
    return text


def parse_tsv(tsv):
    """Combine word boxes into lines using Tesseract's original image geometry."""
    width = height = 0
    lines = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        level = int(row["level"])
        left, top, w, h = (int(row[key]) for key in ("left", "top", "width", "height"))
        if level == 1:
            width, height = w, h
        if level != 5 or not (text := row.get("text", "").strip()):
            continue
        confidence = float(row["conf"]) / 100.0
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            continue
        if w <= 0 or h <= 0 or left < 0 or top < 0:
            continue
        key = tuple(row[name] for name in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append((text, confidence, [left, top, left + w, top + h]))
    if width <= 0 or height <= 0:
        raise ValueError("Tesseract did not return positive image dimensions")
    nodes = []
    for words in lines.values():
        bounds = [min(w[2][0] for w in words), min(w[2][1] for w in words),
                  max(w[2][2] for w in words), max(w[2][3] for w in words)]
        if bounds[2] > width or bounds[3] > height:
            continue
        node = {"text": line_text(words),
                "confidence": sum(w[1] * len(w[0]) for w in words) / sum(len(w[0]) for w in words),
                "bounds": bounds}
        if len(words) > 1:
            # Keep original word geometry: a back icon or input caret can be
            # grouped with a navigation label without being part of its text.
            node["words"] = [{"text": text, "confidence": confidence, "bounds": box}
                             for text, confidence, box in words]
        nodes.append(node)
    return {"width": width, "height": height, "nodes": nodes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--language", default="eng+chi_sim")
    parser.add_argument("--psm", type=int, default=11, choices=range(3, 14))
    parser.add_argument("--timeout", type=float, default=30, help="Total OCR time budget in seconds")
    args = parser.parse_args()
    try:
        print(json.dumps(recognize(args.image, args.language, args.psm, args.timeout), ensure_ascii=False))
    except (OSError, ValueError, subprocess.SubprocessError, KeyError, ImportError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


def recognize(image, language="eng+chi_sim", psm=11, timeout=30):
    # Tesseract expects dark text on a light background. Keep an additional
    # inverted contrast pass for dark-mode placeholders; do not move pixels or alter the
    # original screenshot. The UI collector merges overlapping readings.
    from PIL import Image, ImageOps
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="minizoo-ocr-") as temporary:
        inverted = Path(temporary) / "inverted.png"
        with Image.open(image) as source:
            # Lift dim text above dark backgrounds before inversion. The
            # original pass remains available for ordinary/light-mode text.
            contrast = source.convert("L").point(lambda value: max(0, min(255, (value - 20) * 4)))
            ImageOps.invert(contrast).save(inverted)
        combined = None
        for path in (image, inverted):
            result = subprocess.run(["tesseract", str(path), "stdout", "-l", language,
                                     "--psm", str(psm), "tsv"],
                                    capture_output=True, text=True, check=True,
                                    timeout=max(0.01, deadline - time.monotonic()))
            screen = parse_tsv(result.stdout)
            if combined is None:
                combined = screen
            else:
                if (combined["width"], combined["height"]) != (screen["width"], screen["height"]):
                    raise ValueError("OCR preprocessing changed image geometry")
                combined["nodes"].extend(screen["nodes"])
        return combined


if __name__ == "__main__":
    sys.exit(main())
