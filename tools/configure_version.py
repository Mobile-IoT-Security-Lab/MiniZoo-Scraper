#!/usr/bin/env python3
"""Create native or container version configuration without contacting a phone."""
import argparse
import csv
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="Versioning phone's ADB serial")
    parser.add_argument("--csv", type=Path, required=True, help="Private version-input CSV")
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--output", type=Path, default=root / "private/config/version.json")
    parser.add_argument("--container", action="store_true", help="Use the Compose /input and /data mounts")
    parser.add_argument("--data-dir", type=Path, default=root / "collection_results/version",
                        help="Native result directory; ignored with --container")
    parser.add_argument("--ocr", choices=("vision", "tesseract"),
                        help="Native OCR; defaults to Vision on macOS, Tesseract elsewhere")
    args = parser.parse_args()
    config = json.loads((root / "version_scraper/config.example.json").read_text())
    if args.output.exists():
        parser.error(f"Configuration already exists: {args.output}; edit it or choose a new --output")
    try:
        with args.csv.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"appid", "name", "previous_app_version"}.issubset(reader.fieldnames or []):
                parser.error("CSV needs appid, name and previous_app_version columns")
            count = sum(1 for _ in reader)
    except OSError as exc:
        parser.error(f"Cannot read the input CSV: {exc}")
    if not count:
        parser.error("CSV contains no records")
    data = Path("/data/version") if args.container else args.data_dir.expanduser().resolve()
    config.update(source_csv=f"/input/{args.csv.name}" if args.container else str(args.csv.expanduser().resolve()),
                  work_csv=str(data / "apps.csv"), output_dir=str(data / "output"), expected_records=count)
    if args.container or args.ocr == "tesseract" or (not args.ocr and sys.platform != "darwin"):
        config["ocr_command"] = ["python3" if args.container else sys.executable, "{project_dir}/tools/ocr_tesseract.py"]
    if not args.container:
        for key in ("evidence_command", "decompiler_command", "search_metadata_command"):
            config[key][0] = sys.executable
    config["devices"] = [{"name": "versioning", "udid": args.serial,
                           "system_port": 8201, "account": "versioning", "enabled": True}]
    command = config.get("decompiler_command", [])
    if args.container and "--tool-root" in command:
        command[command.index("--tool-root") + 1] = "/opt/wxappUnpacker"
    for key in ("volume_mount", "volume_uuid"):
        config.pop(key, None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Created {args.output}; {count} rows; results: {config['output_dir']}.")
    if args.container:
        print(f"Mount the input as {config['source_csv']}.")


if __name__ == "__main__":
    main()
