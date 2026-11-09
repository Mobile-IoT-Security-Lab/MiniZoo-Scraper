# syntax=docker/dockerfile:1
FROM eclipse-temurin:21-jdk-jammy AS java
FROM node:26-bookworm-slim

# Google's Linux SDK tools use x86-64; Compose selects linux/amd64 for this image.
ENV JAVA_HOME=/opt/java/openjdk \
    ANDROID_HOME=/opt/android-sdk \
    APPIUM_HOME=/opt/appium \
    APPIUM_SKIP_CHROMEDRIVER_INSTALL=1 \
    ADB_SERVER_SOCKET=tcp:127.0.0.1:5037
ENV PATH="${JAVA_HOME}/bin:${ANDROID_HOME}/platform-tools:${PATH}"
COPY --from=java /opt/java/openjdk /opt/java/openjdk
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Archive and checksum published at https://developer.android.com/studio.
ARG SDK_TOOLS=15859902
ARG SDK_SHA256=4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583
RUN curl --fail --location --retry 3 \
      "https://dl.google.com/android/repository/commandlinetools-linux-${SDK_TOOLS}_latest.zip" -o /tmp/sdk.zip \
    && echo "${SDK_SHA256}  /tmp/sdk.zip" | sha256sum --check - \
    && mkdir -p "${ANDROID_HOME}/cmdline-tools" \
    && unzip -q /tmp/sdk.zip -d "${ANDROID_HOME}/cmdline-tools" \
    && mv "${ANDROID_HOME}/cmdline-tools/cmdline-tools" "${ANDROID_HOME}/cmdline-tools/latest" \
    && rm /tmp/sdk.zip \
    && (yes | "${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null) \
    && "${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager" \
       "platform-tools" "platforms;android-36" "build-tools;36.0.0" \
    && rm -rf /root/.android/cache

WORKDIR /opt/appium
COPY version_scraper/package.json version_scraper/package-lock.json ./
RUN npm ci --no-audit --no-fund \
    && npm cache clean --force \
    && chown -R node:node /opt/appium
COPY --chmod=755 docker/appium.sh /usr/local/bin/minizoo-appium
USER node
ENTRYPOINT ["minizoo-appium"]
