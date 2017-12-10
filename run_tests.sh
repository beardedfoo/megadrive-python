#!/bin/bash
export PYTHON=python3.6
export GCC="gcc -Wall -pedantic-errors"
export PYC="$(pwd)/pyc.py"

FAILED=0
PASSED=0
for test_script in $(find $(pwd)/tests -name run.sh); do
    (cd $(dirname $test_script); bash -e $test_script)
    if [ $? -ne 42 ]; then
        echo "FAILED: $test_script"
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
