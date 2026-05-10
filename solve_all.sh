#!/usr/bin/env bash
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
ANSWERS_DIR="$HOME/Downloads"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
# ─────────────────────────────────────────────────────────────────────────────

usage() {
  echo "Usage: $(basename "$0") [--submit] [--no-human] [--min-delay MS] [--max-delay MS]"
  echo ""
  echo "  --submit        Auto-submit each form after filling (default: pause for review)"
  echo "  --no-human      Disable human-like timing delays (human mode is ON by default)"
  echo "  --min-delay MS  Minimum delay between actions in ms (default: 500)"
  echo "  --max-delay MS  Maximum delay between actions in ms (default: 4000)"
  echo "  -v, --verbose   Debug logging"
  echo ""
  echo "  Answers files are loaded from: $ANSWERS_DIR/answers*.json"
  exit 1
}

# ── Parse flags ───────────────────────────────────────────────────────────────
PASSTHROUGH=(--human) # human mode ON by default
NO_HUMAN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
  --submit | -v | --verbose)
    PASSTHROUGH+=("$1")
    shift
    ;;
  --no-human)
    NO_HUMAN=1
    shift
    ;;
  --min-delay | --max-delay)
    PASSTHROUGH+=("$1" "$2")
    shift 2
    ;;
  -h | --help)
    usage
    ;;
  *)
    echo "Unknown option: $1"
    usage
    ;;
  esac
done

# Remove --human from passthrough if --no-human was given
if [[ $NO_HUMAN -eq 1 ]]; then
  PASSTHROUGH=("${PASSTHROUGH[@]/--human/}")
fi

# ── Activate virtual environment ──────────────────────────────────────────────
if [[ ! -f "$VENV/bin/activate" ]]; then
  echo "❌  Virtual environment not found at $VENV"
  echo "    Run: uv venv && uv pip install -r requirements.txt"
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ── Collect answer files ──────────────────────────────────────────────────────
mapfile -t FILES < <(find "$ANSWERS_DIR" -maxdepth 1 -name "answers*.json" | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "❌  No answers*.json files found in $ANSWERS_DIR"
  exit 1
fi

echo "📋  Found ${#FILES[@]} answer file(s) in $ANSWERS_DIR:"
for f in "${FILES[@]}"; do
  echo "     • $(basename "$f")"
done
echo ""

# ── Process each file ─────────────────────────────────────────────────────────
PASS=0
FAIL=0
FAILED_FILES=()

for answers_file in "${FILES[@]}"; do
  name="$(basename "$answers_file")"
  echo "══════════════════════════════════════════════"
  echo "▶  Processing: $name"
  echo "══════════════════════════════════════════════"

  if python "$SCRIPT_DIR/fill_form.py" --answers "$answers_file" "${PASSTHROUGH[@]}"; then
    echo "✅  Done: $name"
    ((PASS++)) || true
  else
    echo "❌  Failed: $name"
    ((FAIL++)) || true
    FAILED_FILES+=("$name")
  fi
  echo ""
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo "══════════════════════════════════════════════"
echo "📊  Summary: $PASS succeeded, $FAIL failed"
if [[ ${#FAILED_FILES[@]} -gt 0 ]]; then
  echo "    Failed files:"
  for f in "${FAILED_FILES[@]}"; do
    echo "     • $f"
  done
fi
echo "══════════════════════════════════════════════"

[[ $FAIL -eq 0 ]]
