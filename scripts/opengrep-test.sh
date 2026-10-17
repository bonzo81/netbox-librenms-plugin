#!/usr/bin/env bash
#
# Run opengrep rule-tests for the ruleset (.opengrep/librenms-rules.yaml) against the annotated
# fixtures in .opengrep/tests/. Each fixture carries `ruleid:` / `ok:` markers asserting which
# lines must (and must not) match. A .py fixture writes them as `#` comments, a .html fixture as
# `<!-- -->` comments.
#
# `opengrep test` pairs a <stem>.yaml rule file with each same-stem fixture inside one directory. To
# keep a single source of truth (.opengrep/librenms-rules.yaml) we stage a temp directory pairing a
# copy of the ruleset with each fixture, then run the test there.
#
# A fixture with another extension, or with no `ruleid:` marker, fails the run: opengrep would check
# nothing in it and still pass. A marker that opengrep cannot read, such as `{# ruleid: ... #}`,
# also fails the run: opengrep only logs it and ignores the marker.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/scripts/opengrep-bin.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for fixture in "$repo_root"/.opengrep/tests/*; do
  name="$(basename "$fixture")"
  case "$name" in
    *.py | *.html) ;;
    *)
      echo "error: unsupported rule-test fixture $name (use .py or .html)" >&2
      exit 1
      ;;
  esac
  if ! grep -Eq '(#|<!--) *ruleid: *[a-z0-9]' "$fixture"; then
    echo "error: rule-test fixture $name has no ruleid: marker, so it tests nothing" >&2
    exit 1
  fi
  cp "$repo_root/.opengrep/librenms-rules.yaml" "$tmp/${name%.*}.yaml"
  cp "$fixture" "$tmp/$name"
done

# Not exec: that would replace the shell before the EXIT trap removes the staging directory.
status=0
output="$("$opengrep_bin" test --taint-intrafile "$tmp" 2>&1)" || status=$?
printf '%s\n' "$output"
if grep -q 'annotation without leading comment' <<<"$output"; then
  echo "error: a rule-test marker is not in a comment that opengrep reads, so it checks nothing" >&2
  exit 1
fi
if grep -q 'No unit tests found' <<<"$output"; then
  echo "error: opengrep collected no rule-tests, so nothing was checked" >&2
  exit 1
fi
exit "$status"
