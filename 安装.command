#!/bin/sh
# Finder entry point. Command-line and unattended installs use the same deploy.sh.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$#" -eq 0 ]; then
    selection=$(/usr/bin/osascript <<'APPLESCRIPT'
try
    set answer to display dialog "默认会沿用已安装的实例。首次安装可使用用户目录，或选择一个专用的空文件夹。" with title "安装微信监控" buttons {"取消", "选择文件夹…", "使用默认目录"} default button "使用默认目录" cancel button "取消"
    if button returned of answer is "选择文件夹…" then
        set chosen to choose folder with prompt "请选择或新建微信监控专用文件夹"
        return POSIX path of chosen
    end if
    return ""
on error number -128
    return "cancel"
end try
APPLESCRIPT
)
    case "$selection" in
        cancel) exit 0 ;;
        "") ;;
        *) set -- --data-dir "$selection" ;;
    esac
fi
exec "$repo/deploy.sh" "$@"
