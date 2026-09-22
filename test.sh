#!/bin/sh
# Run the carbon unit-test suite with twisted.trial. Intended for manual use.
#
# Usage:
#   ./test.sh                     # run the whole carbon test package
#   ./test.sh test_writer         # run a single test module (short name)
#   ./test.sh carbon.tests.test_writer.SchemaMatchCacheTest.test_first_match_wins
#   PYTHON=/path/to/python ./test.sh
set -e

cd "$(dirname "$0")"

if [ -z "$PYTHON" ]; then
  for candidate in python3 /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c "import twisted" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [ -z "$PYTHON" ]; then
  echo "No interpreter with Twisted found. Set PYTHON to an interpreter"
  echo "that has the requirements installed (twisted, six, mock, whisper)."
  exit 1
fi

export GRAPHITE_NO_PREFIX=true
export PYTHONPATH="lib${PYTHONPATH:+:$PYTHONPATH}"

if [ "$#" -eq 0 ]; then
  set -- carbon
else
  targets=""
  for arg do
    case "$arg" in
      carbon.*) targets="$targets $arg" ;;
      *) targets="$targets carbon.tests.$arg" ;;
    esac
  done
  # shellcheck disable=SC2086
  set -- $targets
fi

exec "$PYTHON" -m twisted.trial "$@"
