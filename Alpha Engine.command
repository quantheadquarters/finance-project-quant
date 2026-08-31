#!/bin/bash
# Alpha Engine - double-click this file to start.
#
# .command is the macOS extension Finder opens in Terminal. The real logic is in
# launch.py; this only locates a Python 3 and hands over.

cd "$(dirname "$0")" || exit 1

for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
        exec "$py" launch.py "$@"
    fi
done

echo ""
echo "  Python 3 was not found on this Mac."
echo ""
echo "  Install it either way:"
echo "    - from https://python.org/downloads, or"
echo "    - with Homebrew:  brew install python"
echo ""
echo "  Then double-click this file again."
echo ""
read -r -p "Press Enter to close..."
exit 1
