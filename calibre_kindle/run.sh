#!/bin/sh
# Entwicklungs-Override: Liegt unter /share/calibre/app-dev eine main.py,
# wird diese statt der eingebauten App verwendet (Updates ohne Neu-Build).
if [ -f /share/calibre/app-dev/main.py ]; then
  echo "Verwende App aus /share/calibre/app-dev"
  rm -rf /app && cp -r /share/calibre/app-dev /app
fi
exec python3 /app/main.py
