#!/usr/bin/env bash
# Scan for content that must never reach this public repo.
#   scripts/privacy-check.sh          staged files (use as .git/hooks/pre-commit)
#   scripts/privacy-check.sh --all    every tracked + untracked (non-ignored) file
# Private patterns (company names, codenames, hostnames, people) live OUTSIDE the
# repo in ~/.config/wd/private-patterns.txt, one extended regex per line.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PRIVATE="${WD_PRIVATE_PATTERNS:-$HOME/.config/wd/private-patterns.txt}"
if [[ "${1:-}" == "--all" ]]; then
  files=$(git ls-files --cached --others --exclude-standard)
else
  files=$(git diff --cached --name-only --diff-filter=ACM)
fi
[[ -z "$files" ]] && exit 0

builtin_patterns=(
  '\b(10|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.[0-9]{1,3}\.[0-9]{1,3}\b'  # private IPv4
  '\b([1-9]|[1-9][0-9]|1[01][0-9]|12[0-689]|1[3-9][0-9]|2[0-5][0-9])(\.[0-9]{1,3}){3}:[0-9]{2,5}\b'  # ip:port, not 127.x
  '(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})'   # tokens
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)

fail=0
check() {
  local label="$1" pat="$2"
  # shellcheck disable=SC2086
  if hits=$(grep -nIE -- "$pat" $files 2>/dev/null | grep -v '^scripts/privacy-check.sh:' | grep -v '^tests/'); then
    echo "✗ $label: $pat"
    echo "$hits" | head -20 | sed 's/^/    /'
    fail=1
  fi
}
for p in "${builtin_patterns[@]}"; do check builtin "$p"; done
if [[ -f "$PRIVATE" ]]; then
  while IFS= read -r p; do
    [[ -z "$p" || "$p" == \#* ]] && continue
    check private "$p"
  done < "$PRIVATE"
else
  echo "note: no private pattern file at $PRIVATE (only built-in checks ran)" >&2
fi
if [[ $fail -ne 0 ]]; then
  echo "privacy-check failed: remove the matches above (or fix the pattern file)." >&2
  exit 1
fi
echo "privacy-check: clean ($(echo "$files" | wc -l | tr -d ' ') files)"
