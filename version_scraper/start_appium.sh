#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Local installation: discover the driver from package.json dependencies.
export APPIUM_HOME="$PWD"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
if [ -z "${JAVA_HOME:-}" ] && [ -d /opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home ]; then
  export JAVA_HOME=/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home
fi
exec ./node_modules/.bin/appium --address 127.0.0.1 --port 4723 "$@"
