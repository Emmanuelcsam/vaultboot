#!/bin/sh
# Run from a trusted checkout/release ZIP; never pipe this into a root shell.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || {
    echo 'Python 3 is required. Install Python 3, then run ./autoflash.sh again.' >&2
    exit 1
}
python=$(command -v python3)
case " ${*} " in
    *' --list '*|*' --help '*|*' -h '*) exec "$python" "$root/scripts/autoflash.py" "$@" ;;
esac
if [ "$(id -u)" -ne 0 ]; then
    echo 'Administrator access is needed to inspect, unmount, and write your chosen removable drive.'
    exec sudo -- "$python" "$root/scripts/autoflash.py" "$@"
fi
exec "$python" "$root/scripts/autoflash.py" "$@"
