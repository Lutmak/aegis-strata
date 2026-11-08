#!/bin/bash
# judge.sh <task> <work-dir> -> prints judge JSON ({"pass": bool, ...}) and exits 0 on pass. Python tasks are judged
# inside the sandbox image (no host dependencies); the web task through lookingglass on this Mac.
set -u
TASK=$1; WORK=$(cd "$2" && pwd); HERE=$(cd "$(dirname "$0")" && pwd); IMG=us2-acceptance:0.85.1
pyjudge() { docker run --rm --entrypoint python3 -e WORK=/work -v "$WORK":/work -v "$HERE/tasks/$TASK":/task:ro -w /work $IMG "$@"; }
case $TASK in
  md-toc)
    [ -f "$WORK/mdtoc.py" ] || { echo '{"pass": false, "why": "no mdtoc.py"}'; exit 1; }
    out=$(pyjudge -m pytest -q -p no:cacheprovider /task/hidden/test_hidden.py 2>&1); rc=$?
    own=$(pyjudge -m pytest -q -p no:cacheprovider test_mdtoc.py 2>&1 | tail -1)
    python3 -c "import json,sys; print(json.dumps({'pass': $rc==0, 'hidden': sys.argv[1].splitlines()[-1], 'own_tests': sys.argv[2]}))" "$out" "$own"; exit $rc ;;
  kvstore)
    [ -f "$WORK/kvstore.py" ] || { echo '{"pass": false, "why": "no kvstore.py"}'; exit 1; }
    out=$(pyjudge /task/blackbox.py /work 2>&1); rc=$?; echo "$out" | tail -1; exit $rc ;;
  bugfix)
    changed=$(cd "$WORK" && sha256sum tests/test_pkg.py | cut -c1-64); orig=$(sha256sum "$HERE/tasks/bugfix/seed/tests/test_pkg.py" | cut -c1-64)
    out=$(pyjudge -m pytest -q -p no:cacheprovider tests /task/hidden/test_hidden.py 2>&1); rc=$?
    [ "$changed" = "$orig" ] || rc=2
    python3 -c "import json,sys; print(json.dumps({'pass': $rc==0, 'tests_untouched': sys.argv[1]==sys.argv[2], 'pytest': sys.argv[3].splitlines()[-1]}))" "$changed" "$orig" "$out"; exit $rc ;;
  minesweeper)
    [ -f "$WORK/index.html" ] || { echo '{"pass": false, "why": "no index.html"}'; exit 1; }
    export PATH="$HOME/.bun/bin:$PATH"; lg() { bun run "$HOME/Code/lookingglass/src/cli.ts" "$@"; }
    # one browser: campaigns on different servers may judge at the same moment
    LOCK=/tmp/us2-judge-browser.lock; n=0; until mkdir "$LOCK" 2>/dev/null; do sleep 2; n=$((n+1)); [ $n -gt 300 ] && { rmdir "$LOCK"; }; done
    # the browser refuses file: URLs, so the work dir is served over HTTP for the duration
    PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
    (cd "$WORK" && python3 -m http.server --bind 127.0.0.1 "$PORT" > /dev/null 2>&1) & HTTP=$!
    trap 'kill $HTTP 2>/dev/null; rmdir "$LOCK" 2>/dev/null' EXIT; sleep 1
    lg console --clear > /dev/null 2>&1; lg goto "http://127.0.0.1:$PORT/index.html" > /dev/null 2>&1; sleep 1
    cells=$(lg js "document.querySelectorAll('.cell').length" 2>/dev/null | tr -dc 0-9)
    revealed=$(lg js "var c=document.querySelector('.cell[data-row=\"4\"][data-col=\"4\"]'); if(c){c.click();} document.querySelectorAll('.cell.revealed').length" 2>/dev/null | tr -dc 0-9)
    errlines=$(lg console --errors 2>/dev/null | grep -v -i "no console errors" | grep -c .)
    lg screenshot "$WORK/../judge-screenshot.png" > /dev/null 2>&1
    pass=false; [ "${cells:-0}" -ge 81 ] && [ "${revealed:-0}" -ge 1 ] && [ "${errlines:-1}" -eq 0 ] && pass=true
    echo "{\"pass\": $pass, \"cells\": ${cells:-0}, \"revealed_after_click\": ${revealed:-0}, \"console_errors\": ${errlines:-1}}"; [ $pass = true ] ;;
  *) echo '{"pass": false, "why": "unknown task"}'; exit 1 ;;
esac
