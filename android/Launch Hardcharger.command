#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
android/scripts/run-emulator.sh
