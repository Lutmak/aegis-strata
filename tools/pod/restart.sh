#!/bin/bash
# restart.sh <case> : stop whatever serves that case's port, start serve.sh <case> detached, log to /opt/us2-servers/<case>.log
set -u; S=/opt/us2-servers; mkdir -p $S; C=${1:?case}
case $C in glm) PORT=18096;; *) PORT=18095;; esac
pid=$(ss -ltnp "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
if [ -n "$pid" ]; then kill $pid; for i in $(seq 1 60); do kill -0 $pid 2>/dev/null || break; sleep 1; done; fi
pkill -x strata-poly 2>/dev/null; sleep 2
setsid nohup bash /opt/us2-build/tools/pod/serve.sh $C > $S/$C.log 2>&1 < /dev/null &
echo "started $C on $PORT, log $S/$C.log"
