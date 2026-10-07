#!/bin/bash
# Install or upgrade linemon. Run on the Pi: sudo ./install.sh
#
# Can be run wherever the Pi is plugged in (e.g. behind the UDM). Afterwards,
# move the Pi's cable to the Livebox: the monitor notices the new router within
# 15 seconds, re-detects the ISP path, and the analyzer ignores the data from before.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo"; exit 1; }
cd "$(dirname "$0")"

# Check the settings with the new code before touching anything, so a mistake in
# /etc/linemon/linemon.conf or LINEMON_ARGS can't stop a monitor that is working.
if ! check=$(/usr/bin/python3 ./linemon.py --check-config 2>&1); then
    echo "$check" >&2
    echo "Nothing was changed. Fix the settings above and run ./install.sh again." >&2
    exit 1
fi

# Restarting the monitor starts a new run in events.csv, so only do it when the
# monitor itself changed (or isn't running) - upgrading the web page or the
# analyzer then doesn't interrupt the measurements.
restart_monitor=yes
if cmp -s linemon.py /opt/linemon/linemon.py \
    && cmp -s linemon.service /etc/systemd/system/linemon.service \
    && systemctl is-active -q linemon; then
    restart_monitor=no
fi

install -d /opt/linemon /opt/linemon/routers /var/lib/linemon
[ -d /etc/linemon ] || install -d -m 700 /etc/linemon
install -m 644 linemon.conf.example /etc/linemon/linemon.conf.example
install -m 755 linemon.py analyze.py web.py trim.py /opt/linemon/
install -m 755 routers/*.py /opt/linemon/routers/
install -m 644 linemon.service linemon-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q linemon linemon-web
[ "$restart_monitor" = yes ] && systemctl restart linemon
systemctl restart linemon-web

sleep 2
address=$(ip -4 -o addr show dev eth0 | awk '{split($4, a, "/"); print a[1]; exit}')
echo
echo "Monitor:  $(systemctl is-active linemon)$([ "$restart_monitor" = no ] && echo ' (unchanged, kept running)')"
echo "Web page: $(systemctl is-active linemon-web) at http://${address:-<pi-address>}:8080"
echo "NTP synchronised: $(timedatectl show -p NTPSynchronized --value)"
echo "Router on eth0 right now: $(ip -4 route show default dev eth0 | awk '/default/ {print $3; exit}')"
echo "Settings: $(sed -n 's/^  settings: //p' <<< "$check")"
hook=$(sed -n 's/^  hook: //p' <<< "$check")
if [ "$hook" != off ]; then
    echo "Router capture: on ($hook)"
else
    echo "Router capture: off (see 'Capturing the router's own status' in the README)"
fi
# Password files must stay root-only. Only warn: changing someone's permissions is theirs to do.
# (linemon.conf and the example hold no secrets, so they may be readable.)
open_files=$(find /etc/linemon -maxdepth 1 -type f ! -name 'linemon.conf*' -perm /077 2>/dev/null || true)
if [ -n "$open_files" ]; then
    echo "Warning: other users can read these files, which may hold passwords. Run chmod 600 on them:"
    echo "$open_files" | sed 's/^/     /'
fi
if [ ! -e /etc/linemon/linemon.conf ] && ! /usr/bin/python3 ./linemon.py --migrate | grep -q 'Nothing to migrate'; then
    echo "Tip: your settings are still in /etc/default/linemon. That keeps working;"
    echo "     /opt/linemon/linemon.py --migrate prints the equivalent /etc/linemon/linemon.conf."
fi
