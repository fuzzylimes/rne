#!/usr/bin/env bash
# Fake HandBrakeCLI that prints an error to stderr and exits 1.
echo "Encoding: task 1 of 1, 10.00 % (20.0 fps, avg 20.0 fps, ETA 00h08m00s)"
echo "ERROR: something went wrong with the encode" >&2
echo "libav: codec not found" >&2
exit 1
