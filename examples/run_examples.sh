#!/bin/sh

export PYOPENCL_CTX=':'

rm -f examples/*.vtu
printf "Running examples...\n"
python examples/eulerflow.py
printf "done!\n" 