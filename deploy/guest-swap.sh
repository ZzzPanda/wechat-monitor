#!/bin/sh
# Runs as root inside the project's Lima guest. Its root disk lives on ssd1.
set -eu
umask 077
directory=/var/lib/wechatmonitor
swapfile=$directory/swap
unit=var-lib-wechatmonitor-swap.swap
install -d -m 0700 "$directory"
exec 9>"$directory/setup.lock"
flock 9

if [ ! -e "$swapfile" ]; then
    # Reuse the locked staging path after an interrupted first installation.
    pending=$directory/swap.pending
    rm -f "$pending"
    trap 'rm -f "$pending"' EXIT
    : >"$pending"
    fallocate -l 2G "$pending"
    mkswap "$pending" >/dev/null
    mv -T "$pending" "$swapfile"
fi
if [ ! -f "$swapfile" ] || [ -L "$swapfile" ] || [ "$(stat -c '%u:%a:%s' "$swapfile")" != '0:600:2147483648' ]; then
    echo "Unexpected guest swap file: $swapfile" >&2
    exit 1
fi

# systemd also activates the file on subsequent boots without rerunning deploy.
unit_path=/etc/systemd/system/$unit
pending_unit=$(mktemp /etc/systemd/system/.wechat-swap.XXXXXX)
trap 'rm -f "$pending_unit"' EXIT
cat >"$pending_unit" <<'EOF'
[Unit]
Description=WeChat monitor guest swap on the external SSD

[Swap]
What=/var/lib/wechatmonitor/swap

[Install]
WantedBy=swap.target
EOF
if ! cmp -s "$pending_unit" "$unit_path"; then
    chmod 0644 "$pending_unit"
    mv -T "$pending_unit" "$unit_path"
    systemctl daemon-reload
fi
systemctl enable --now "$unit"
