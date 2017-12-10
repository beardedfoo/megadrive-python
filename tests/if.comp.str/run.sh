#!/bin/sh
${PYC} test.py > test.c
if [ $? -ne 0 ]; then
    exit 1
fi

${GCC} test.c -otest.bin
if [ $? -ne 0 ]; then
    exit 1
fi

./test.bin
exit $?
