#!/usr/bin/env bash
#
# Run the repo's custom opengrep ruleset (.opengrep/librenms-rules.yaml) over the source tree.
# Used by the pre-push hook and for manual scans. Exits non-zero on any finding.
# Pass opengrep options before the first -- and targets after it.
# Without targets after --, scan the default package and test tree.
#
# --taint-intrafile is required, not optional: taint must cross into a module-private helper, which
# is where a per-function analysis loses the serial-match branch.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/scripts/opengrep-bin.sh"

options=()
targets=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == -- ]]; then
    shift
    targets=("$@")
    break
  fi
  options+=("$1")
  shift
done

if [[ ${#targets[@]} -eq 0 ]]; then
  # opengrep's default ignores skip test directories, so name the test files explicitly.
  shopt -s globstar nullglob
  targets=("$repo_root/netbox_librenms_plugin" "$repo_root"/netbox_librenms_plugin/tests/**/*.py)
fi

exec "$opengrep_bin" scan \
  --config "$repo_root/.opengrep/librenms-rules.yaml" \
  --taint-intrafile \
  --error \
  "${options[@]}" \
  -- "${targets[@]}"
