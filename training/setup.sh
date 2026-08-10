#!/usr/bin/env bash
# Bootstrap the TRAINING environment on this PC (not the Raspberry Pi).
#
# Creates a dedicated venv, installs a CUDA 12.8 PyTorch build that can drive
# an RTX 5060 Ti (Blackwell / sm_120), then ultralytics on top, then verifies
# the GPU is actually visible to torch.
#
#   bash training/setup.sh
#
# Re-runnable: skips venv creation if it already exists.
set -euo pipefail

# Resolve repo root from this script's location, so it works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv-train"
CU_INDEX="https://download.pytorch.org/whl/cu128"

cd "${REPO_ROOT}"

if [ ! -d "${VENV_DIR}" ]; then
  echo ">> Creating training venv at ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
else
  echo ">> Reusing existing venv at ${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo ">> Upgrading pip"
python -m pip install --upgrade pip

echo ">> Installing PyTorch + torchvision from CUDA 12.8 index (Blackwell support)"
pip install torch torchvision --index-url "${CU_INDEX}"

echo ">> Installing training requirements (ultralytics, pyyaml)"
pip install -r "${SCRIPT_DIR}/requirements-train.txt"

echo ">> Verifying CUDA is visible to PyTorch"
python - <<'PY'
import sys
import torch

ok = torch.cuda.is_available()
print(f"torch version      : {torch.__version__}")
print(f"CUDA available     : {ok}")
if ok:
    print(f"CUDA device count  : {torch.cuda.device_count()}")
    print(f"CUDA device 0      : {torch.cuda.get_device_name(0)}")
    print(f"CUDA capability    : sm_{''.join(map(str, torch.cuda.get_device_capability(0)))}")
else:
    print(
        "\nERROR: torch cannot see the GPU. Training would fall back to CPU.\n"
        "Check the NVIDIA driver in WSL2 (`nvidia-smi`) and that the cu128\n"
        "wheels installed cleanly above.",
        file=sys.stderr,
    )
    sys.exit(1)
PY

echo
echo ">> Setup complete. Activate the env in new shells with:"
echo "     source ${VENV_DIR}/bin/activate"
