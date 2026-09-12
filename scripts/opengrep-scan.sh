#!/usr/bin/env bash
#
# Run the repo's custom opengrep ruleset (.opengrep/librenms-rules.yaml) over the source tree.
# Used by the pre-push hook and CI. Exits non-zero on any finding.
#
# --taint-intrafile is required, not optional: taint must cross into a module-private helper, which
# is where a per-function analysis loses the serial-match branch.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/scripts/opengrep-bin.sh"

# An argument that names an existing path is a target; anything else is passed to opengrep
# unchanged. This avoids restating opengrep's own flag table here, which would rot on every
# release. The one ambiguity, an option VALUE that happens to name an existing path, does not
# arise for the flags this repo uses.
options=()
targets=()
for arg in "$@"; do
  if [[ -e "$arg" ]]; then
    targets+=("$arg")
  else
    options+=("$arg")
  fi
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
