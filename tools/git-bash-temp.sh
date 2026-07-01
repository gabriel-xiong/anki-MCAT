# Git Bash: native MSVC rustc/cargo need a Windows TEMP dir.
# MSYS TMP=/tmp or /c/... paths make rustc fall back to C:\windows (access denied).
if [[ "${OSTYPE:-}" == "msys" || "${OSTYPE:-}" == "cygwin" ]]; then
  export MSYS2_ENV_CONV_EXCL="${MSYS2_ENV_CONV_EXCL:+$MSYS2_ENV_CONV_EXCL; }TMP;TEMP;TMPDIR"
  if command -v cygpath >/dev/null 2>&1; then
    export TMP="$(cygpath -w "$LOCALAPPDATA/Temp")"
  else
    export TMP="${LOCALAPPDATA}/Temp"
  fi
  export TEMP="$TMP"
  export TMPDIR="$TMP"
fi
