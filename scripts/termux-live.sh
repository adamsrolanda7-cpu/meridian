#!/data/data/com.termux/files/usr/bin/bash
# Meridian — dashboard terminal, lancé hors OpenCode.
# Usage depuis Termux : bash ~/meridian-live.sh
set -e

exec proot-distro login ubuntu -- bash -lc \
  'cd /root/meridian && exec python3 -m meridian live'
