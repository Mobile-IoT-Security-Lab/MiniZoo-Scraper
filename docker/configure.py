#!/usr/bin/env python3
"""Create a private container config from the release example; never overwrites."""
import argparse
import csv
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="Versioning phone's ADB serial")
    parser.add_argument("--csv", type=Path, required=True, help="Private version-input CSV")
    parser.add_argument("--output", type=Path, default=Path("private/config/version.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "version_scraper/config.example.json").read_text())
    with args.csv.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"appid", "name", "previous_app_version"}.issubset(reader.fieldnames or []):
            parser.error("CSV needs appid, name and previous_app_version columns")
        count = sum(1 for _ in reader)
    config.update(
        source_csv=f"/input/{args.csv.name}", work_csv="/data/version/apps.csv",
        output_dir="/data/version/output", expected_records=count,
        appium_url="http://127.0.0.1:4723",
        ocr_command=["python3", "{project_dir}/tools/ocr_tesseract.py"],
    )
    config["devices"] = [{"name": "versioning", "udid": args.serial,
                           "system_port": 8201, "account": "versioning", "enabled": True}]
    for command in ("evidence_command", "search_metadata_command"):
        config[command] = ["frida" if value.endswith("/.venv/bin/frida") else value
                           for value in config.get(command, [])]
    command = config.get("decompiler_command", [])
    if "--tool-root" in command:
        command[command.index("--tool-root") + 1] = "/opt/wxappUnpacker"
    for key in ("volume_mount", "volume_uuid"):
        config.pop(key, None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Created {args.output}; {count} rows; local collection only. Input must be mounted as {config['source_csv']}.")


if __name__ == "__main__":
    main()
