#!/usr/bin/env bash
# Run VertConvert&Create straight from this folder, without installing it.
# Needs Python 3.10+, qemu-img, and PySide6 for the app (see the README).
#   ./vertconvert.sh                     # the app
#   ./vertconvert.sh disk.vhdx           # the app, with this image in the list
#   ./vertconvert.sh convert disk.vhdx   # the command line (./vertconvert.sh --help)
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m vertconvert "$@"
