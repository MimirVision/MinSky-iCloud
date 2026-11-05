#!/bin/bash
# Installerer Min Sky → iCloud på denne Mac-en. Dobbeltklikk for å kjøre.
# Trygt å kjøre flere ganger (oppdaterer til nyeste versjon).
set -e
cd "$(dirname "$0")/.."
REPO="$(pwd)"
echo ""
echo "== Min Sky → iCloud: installasjon =="
echo ""

if ! xcode-select -p >/dev/null 2>&1; then
  echo "Installerer Apples utviklerverktøy (det dukker opp et vindu – trykk «Installer»)…"
  xcode-select --install || true
  echo "Kjør denne installasjonen på nytt når utviklerverktøyene er ferdig installert."
  read -r -p "Trykk Enter for å lukke."
  exit 0
fi

if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Installerer Homebrew (du blir bedt om Mac-passordet ditt)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi

command -v pipx >/dev/null 2>&1 || brew install pipx
pipx ensurepath >/dev/null 2>&1 || true
export PATH="$HOME/.local/bin:$PATH"

echo "Installerer osxphotos (kobling til Bilder-appen)…"
pipx install osxphotos >/dev/null 2>&1 || pipx upgrade osxphotos

echo "Installerer Min Sky → iCloud…"
pipx install --force "$REPO"

minsky-icloud snarvei
echo ""
echo "Ferdig! Det ligger nå en snarvei «Min Sky til iCloud» på skrivebordet."
echo ""
echo "VIKTIG første gang: Systeminnstillinger → Personvern og sikkerhet → Full disktilgang → slå på «Terminal»."
echo ""
echo "Starter programmet…"
exec minsky-icloud app
