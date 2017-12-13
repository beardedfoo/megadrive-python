#!/bin/sh
${PYC_MD} test.py > test.c
if [ $? -ne 0 ]; then
    exit 1
fi

export GCC_MD="docker run -v $(pwd)/build:/src --rm -it beardedfoo/gendev:0.3.0"
mkdir -p build
cp *.c build/
pushd build
${GCC_MD}
if [ $? -eq 0 ]; then
    exit 42
fi
exit $?
