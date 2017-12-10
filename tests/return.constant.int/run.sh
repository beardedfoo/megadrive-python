#!/bin/sh
${PYC} test.py > test.c
${GCC} test.c -otest.bin
./test.bin
exit $?
