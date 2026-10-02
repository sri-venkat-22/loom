#!/bin/bash

# exit when any command fails
set -e

# Add verbosity flag to see more details about dependency resolution
VERBOSITY="-v"  # Use -v for less detail, -vvv for even more detail

# Resolve for every supported Python at once, so pins that differ by Python version
# (e.g. numpy, scikit-learn dropped 3.10) get environment markers instead of breaking
# installs on older interpreters.
UNIVERSAL="--universal --python-version 3.10"

# First compile the common constraints of the full requirement suite
# to make sure that all versions are mutually consistent across files
uv pip compile \
    $VERBOSITY \
    $UNIVERSAL \
    --no-strip-extras \
    --output-file=requirements/common-constraints.txt \
    requirements/requirements.in \
    requirements/requirements-*.in \
    $1

# Compile the base requirements
uv pip compile \
    $VERBOSITY \
    $UNIVERSAL \
    --no-strip-extras \
    --constraint=requirements/common-constraints.txt \
    --output-file=tmp.requirements.txt \
    requirements/requirements.in \
    $1

grep -Ev '^(tree-sitter|numpy|scipy)=' tmp.requirements.txt \
    | cat - requirements/tree-sitter.in requirements/python-compat.in requirements/pydub.in \
    > requirements.txt

# Compile additional requirements files
SUFFIXES=(dev help browser playwright memory)

for SUFFIX in "${SUFFIXES[@]}"; do
    uv pip compile \
        $VERBOSITY \
    $UNIVERSAL \
        --no-strip-extras \
        --constraint=requirements/common-constraints.txt \
        --output-file=requirements/requirements-${SUFFIX}.txt \
        requirements/requirements-${SUFFIX}.in \
        $1
done

# /help installs torch from the CPU-only wheel index (see loom/help.py), so drop the
# CUDA packages that the universal resolve pulls in for PyPI's linux x86_64 torch.
awk '/^(nvidia-|cuda-|triton=)/ {skip=1; next} /^[^ ]/ {skip=0} !skip' \
    requirements/requirements-help.txt > tmp.requirements-help.txt
mv tmp.requirements-help.txt requirements/requirements-help.txt
