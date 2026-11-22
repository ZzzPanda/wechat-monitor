ARG BASE_IMAGE=lscr.io/linuxserver/weixin@sha256:30f07c689b723ddd5bf807c239ed9000933ae7a5a9658599278e203dc753f133
FROM ${BASE_IMAGE}

# The native client and Selkies desktop come from the ARM64 LinuxServer base.
# Use only Debian's signed binary repositories for the capture toolchain.
RUN set -eu; \
    packages=/tmp/wechat-debian.list; \
    printf '%s\n' \
      'deb https://deb.debian.org/debian trixie main' \
      'deb https://deb.debian.org/debian-security trixie-security main' > "$packages"; \
    apt_debian() { apt-get -o "Dir::Etc::sourcelist=$packages" -o Dir::Etc::sourceparts=- -o Acquire::Retries=2 "$@"; }; \
    apt_debian update; \
    DEBIAN_FRONTEND=noninteractive apt_debian install -y --no-install-recommends \
      ffmpeg libx11-6 pulseaudio-utils x11-utils xdotool xsel; \
    rm -rf /var/lib/apt/lists/* "$packages"; \
    for executable in ffmpeg ffprobe xdotool xprop xwininfo xsel pactl; do command -v "$executable" >/dev/null; done; \
    python3 -c 'from PIL import Image; from ctypes import CDLL; CDLL("libX11.so.6")'; \
    ffmpeg -hide_banner -devices 2>/dev/null | grep -q x11grab; \
    ffmpeg -hide_banner -devices 2>/dev/null | grep -q pulse; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264
