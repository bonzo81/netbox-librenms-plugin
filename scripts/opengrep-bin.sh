# Locate the opengrep binary. Sourced by opengrep-scan.sh and opengrep-test.sh.
#
# Order: $OPENGREP_BIN, then PATH, then the default user install dir, so the scripts work even when
# opengrep is not on PATH. Install it from https://github.com/opengrep/opengrep.
#
# Exits 127 (the shell's "command not found") when the binary is missing, so a caller can tell
# "the gate could not run" from opengrep's own exit 1, "the gate found something".
opengrep_bin="${OPENGREP_BIN:-}"
if [[ -z "$opengrep_bin" ]]; then
  if command -v opengrep >/dev/null 2>&1; then
    opengrep_bin="$(command -v opengrep)"
  elif [[ -x "$HOME/.local/opt/opengrep/bin/opengrep" ]]; then
    opengrep_bin="$HOME/.local/opt/opengrep/bin/opengrep"
  else
    echo "error: opengrep not found. Install it from https://github.com/opengrep/opengrep" >&2
    echo "       (or set OPENGREP_BIN=/path/to/opengrep)." >&2
    exit 127
  fi
fi
