#!/usr/bin/env bash
#
# Run opengrep rule-tests for the ruleset (.opengrep/librenms-rules.yaml) against the annotated
# fixtures in .opengrep/tests/. Each fixture carries `# ruleid:` / `# ok:` markers asserting which
# lines must (and must not) match.
#
# `opengrep test` pairs a <stem>.yaml rule file with a same-stem <stem>.py fixture inside one
# directory. To keep a single source of truth (.opengrep/librenms-rules.yaml) we stage a temp
# directory pairing a copy of the ruleset with each fixture, then run the test there.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/scripts/opengrep-bin.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for fixture in "$repo_root"/.opengrep/tests/*.py; do
  stem="$(basename "$fixture" .py)"
  cp "$repo_root/.opengrep/librenms-rules.yaml" "$tmp/$stem.yaml"
  cp "$fixture" "$tmp/$stem.py"
done

exec "$opengrep_bin" test --taint-intrafile "$tmp"
