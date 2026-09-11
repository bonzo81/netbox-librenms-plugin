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

targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
  targets=("$repo_root/netbox_librenms_plugin")
fi

exec "$opengrep_bin" scan \
  --config "$repo_root/.opengrep/librenms-rules.yaml" \
  --taint-intrafile \
  --error \
  "${targets[@]}"
