#!/bin/sh
set -eu
exec /opt/appium/node_modules/.bin/appium \
  --address 127.0.0.1 --port "${APPIUM_PORT:-4723}" \
  --default-capabilities '{"appium:remoteAdbHost":"127.0.0.1","appium:adbPort":5037,"appium:suppressKillServer":true}' \
  "$@"
