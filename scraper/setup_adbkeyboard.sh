#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
APK="${ADBKEYBOARD_APK:-${MINIZOO_DATA_DIR:-$PWD/../collection_results}/tools/ADBKeyboard.apk}"
SHA256="e698adea5633135a067b038f9a0cf41baa4de09888713a81593fb2b9682cdc59"
: "${ANDROID_SERIAL:?Imposta ANDROID_SERIAL sul device da configurare}"
URL="https://raw.githubusercontent.com/senzhk/ADBKeyBoard/master/ADBKeyboard.apk"

if ! command -v adb >/dev/null 2>&1; then
  echo "adb not found"
  exit 1
fi

if ! adb devices | grep -q $'\tdevice'; then
  echo "No authorized ADB device found"
  adb devices
  exit 1
fi

mkdir -p "$(dirname "$APK")"
if [ ! -f "$APK" ]; then
  echo "Download ADBKeyboard da $URL"
  curl -L --fail -o "$APK" "$URL"
fi

python3 - "$APK" "$SHA256" <<'VERIFY'
import hashlib
from pathlib import Path
import sys
if hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() != sys.argv[2]:
    raise SystemExit("SHA-256 ADBKeyboard inatteso: installazione interrotta")
VERIFY

echo "Installing ADBKeyboard on the device..."
adb install -r "$APK"
adb shell ime enable com.android.adbkeyboard/.AdbIME || true
adb shell ime set com.android.adbkeyboard/.AdbIME || true

echo "Fatto. Puoi lanciare wechat_csv_scraper.py"
