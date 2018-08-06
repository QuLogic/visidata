#!/usr/bin/env bash

# Usage: test.sh [testname]

set -e

trap "echo aborted; exit;" SIGINT SIGTERM

if [ -z "$1" ] ; then
    TESTS=tests/*.vd
else
    TESTS=tests/$1.vd
fi

for i in $TESTS ; do
    echo "--- $i"
    outbase=${i##tests/}
    if [ "${i%-nosave.vd}-nosave" == "${i%.vd}" ];
    then
        PYTHONPATH=. bin/vd --play $i --batch
    elif [ "${i%-nofail.vd}-nofail" == "${i%.vd}" ];
    then
        PYTHONPATH=. bin/vd --confirm-overwrite=False --play $i --batch --output tests/golden/${outbase%.vd}.tsv
        echo '=== git diffs; will not trigger build failure ==='
        git --no-pager diff tests/
        git --no-pager diff --numstat tests/
        git checkout tests/golden/
        echo '==================================================='
    else
        echo '=== git diffs; will trigger build failure ==='
        git --no-pager diff --numstat tests/
        git --no-pager diff --exit-code tests/
        echo '=============================================='
    fi
done
