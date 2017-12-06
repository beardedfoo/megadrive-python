#!/bin/sh
${PYC} test > test.c
${GCC} test.c -otest.bin
./test.bin
