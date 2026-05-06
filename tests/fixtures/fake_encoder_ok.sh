#!/usr/bin/env bash
# Fake HandBrakeCLI that prints progress lines and exits 0.
# Writes the output file (last -o argument) so the rename in run_job works.

OUTPUT=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "-o" ]]; then
        OUTPUT="$2"
        shift 2
    else
        shift
    fi
done

echo "Encoding: task 1 of 1, 0.00 % (0.0 fps, avg 0.0 fps, ETA 00h10m00s)"
echo "Encoding: task 1 of 1, 50.00 % (45.6 fps, avg 50.0 fps, ETA 00h05m12s)"
echo "Encoding: task 1 of 1, 100.00 % (52.3 fps, avg 51.1 fps, ETA 00h00m00s)"

if [[ -n "$OUTPUT" ]]; then
    touch "$OUTPUT"
fi

exit 0
