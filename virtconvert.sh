#!/usr/bin/env bash
# Run VirtConvert straight from this folder, without installing it.
# Needs Python 3.10+, qemu-img, and PySide6 for the app (see the README).
#   ./virtconvert.sh                     # the app
#   ./virtconvert.sh disk.vhdx           # the app, with this image in the list
#   ./virtconvert.sh convert disk.vhdx   # the command line (./virtconvert.sh --help)
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m virtconvert "$@"
