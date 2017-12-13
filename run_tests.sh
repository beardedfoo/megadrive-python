#!/bin/bash
export PYTHON=python3.6
export GCC="gcc -Wall -pedantic-errors"
export PYC="$(pwd)/pyc.py -p unix"
export PYC_MD="$(pwd)/pyc.py -p md"

FAILED=0
PASSED=0
for test_script in $(find tests -name run.sh); do
    pushd $(dirname $test_script)
    bash -xe ./run.sh
    RETVAL=$?
    popd
    if [ $RETVAL -ne 42 ]; then
        echo "FAILED: $test_script"
        echo "RETVAL: $RETVAL"
        let FAILED++
    else
        echo "PASSED: $test_script"
        let PASSED++
    fi
done

echo ""
echo "Summary:"
echo "  FAILED: $FAILED"
echo "  PASSED: $PASSED"

exit $FAILED
