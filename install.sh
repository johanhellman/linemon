#!/bin/bash
# Install or upgrade linemon. Run on the Pi: sudo ./install.sh
#
# Can be run wherever the Pi is plugged in (e.g. behind the UDM). Afterwards,
# move the Pi's cable to the Livebox: the monitor notices the new router within
# 15 seconds, re-detects the ISP path, and the analyzer ignores the data from before.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo"; exit 1; }
cd "$(dirname "$0")"

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
install -m 755 linemon.py analyze.py web.py /opt/linemon/
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
if grep -qs -- '--hook' /etc/default/linemon; then
    echo "Router capture: on ($(grep -o -- '--hook [^ "]*' /etc/default/linemon))"
else
    echo "Router capture: off (see 'Capturing the router's own status' in the README)"
fi
