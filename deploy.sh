#!/bin/sh
# Bootstrap requires only the utilities shipped with macOS. Never install system-wide tools.
set -eu
umask 077
repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$(/usr/bin/uname -s)" != Darwin ] || [ "$(/usr/bin/uname -m)" != arm64 ]; then
    echo '请在 Apple Silicon Mac 的原生终端运行安装脚本。' >&2
    exit 1
fi
macos=$(/usr/bin/sw_vers -productVersion)
if [ "${macos%%.*}" -lt 15 ]; then
    echo "本版本的宿主工具需要 macOS 15 或更高版本（当前 $macos）。" >&2
    exit 1
fi
cache=${WECHAT_HOST_CACHE:-"$HOME/Library/Caches/wechat-monitor"}
bootstrap=${WECHAT_BOOTSTRAP_HOME:-"$HOME/Library/Application Support/wechat-monitor/bootstrap"}
manifest="$repo/deploy/host-tools.lock.json"
field() { /usr/bin/plutil -extract "python.$1" raw -o - "$manifest"; }
sha=$(field sha256)
release="$bootstrap/cpython-3.12.16-$(echo "$sha" | /usr/bin/cut -c1-12)"
python="$release/bin/python3.12"
if [ ! -x "$python" ]; then
    /bin/mkdir -p "$cache" "$bootstrap"
    archive="$cache/$(field name)"
    work=$(/usr/bin/mktemp -d "$bootstrap/.python.XXXXXXXX")
    transfer=$(/usr/bin/mktemp -d "$cache/.download.XXXXXXXX")
    trap '/bin/rm -rf "$work" "$transfer"' EXIT
    trap 'exit 130' HUP INT TERM
    valid() { [ -f "$1" ] && [ "$(/usr/bin/shasum -a 256 "$1" | /usr/bin/cut -d " " -f1)" = "$sha" ]; }
    download_python() {
        url=$1
        pending="$transfer/archive"
        attempt=0
        while [ "$attempt" -lt 3 ]; do
            attempt=$((attempt + 1))
            set -- /usr/bin/curl --disable -fLsS --proto '=https' --proto-redir '=https' --connect-timeout 20 \
                --retry 2 --speed-limit 512 --speed-time 60 --max-filesize "$(field size)" -H 'Accept: application/octet-stream'
            resumed=no
            if [ -s "$pending" ]; then
                set -- "$@" --continue-at -
                resumed=yes
            fi
            if "$@" -o "$pending" "$url"; then
                if valid "$pending"; then return 0; fi
                /bin/rm -f "$pending"
                return 1
            else
                failure=$?
            fi
            case $failure in
                18|28|56) continue ;;
                6|7|35|52|55|92) [ "$attempt" -ge 3 ] || /bin/sleep "$attempt" ;;
                22|33|36) [ "$resumed" = yes ] || return 1; /bin/rm -f "$pending" ;;
                *) return 1 ;;
            esac
        done
        return 1
    }
    if ! valid "$archive"; then
        echo '正在下载私有 Python 3.12（约 25 MB）…' >&2
        for url in "$(field url)" "$(field fallback)"; do
            if download_python "$url"; then
                /bin/mv -f "$transfer/archive" "$archive"
                break
            fi
        done
        valid "$archive" || { echo 'Python 下载或 SHA-256 校验失败，请重新运行安装。' >&2; exit 1; }
    fi
    /usr/bin/tar -xzf "$archive" -C "$work"
    # rename publishes a complete runtime atomically, including with two simultaneous first launches.
    "$work/python/bin/python3.12" -I -c '
import errno, os, sys
try:
    os.rename(sys.argv[1], sys.argv[2])
except OSError as exc:
    if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not os.access(sys.argv[2]+"/bin/python3.12", os.X_OK):
        raise
' "$work/python" "$release"
    /bin/rm -rf "$work" "$transfer"
    trap - EXIT HUP INT TERM
fi
exec "$python" -EsB "$repo/deploy/install.py" "$@"
