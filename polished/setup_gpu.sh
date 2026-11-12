#!/usr/bin/env bash
# Lane 2 (Vast.ai or any Ubuntu + NVIDIA box): install the SAME recipe the Modal image uses.
# Run from the repo root on the rented box:  bash polished/setup_gpu.sh
# No tokens of any kind are needed or put here (owner rule: no YouTube/Twitch tokens on Vast hosts).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg fonts-dejavu-core >/dev/null
PY=${PY:-python3}
$PY -m pip install -q -r polished/requirements-gpu.txt
SITE=$($PY -c "import site,sys;print(next(p for p in site.getsitepackages() if p.endswith('site-packages')))")
echo "export LD_LIBRARY_PATH=$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:\${LD_LIBRARY_PATH:-}" > /etc/profile.d/polished.sh
echo "export POLISHED_KIT=$(pwd)/polished/kit PYTHONUNBUFFERED=1" >> /etc/profile.d/polished.sh
. /etc/profile.d/polished.sh
$PY - <<'PYEOF'
from faster_whisper import WhisperModel
WhisperModel("large-v3", device="cpu", compute_type="int8")
import open_clip
open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
PYEOF
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
ffmpeg -v error -f lavfi -i testsrc=size=1920x1080:rate=60:duration=0.5 -c:v h264_nvenc -f null - \
  && echo "NVENC ok" || echo "NVENC unavailable here: renders fall back to libx264"
echo SETUP_DONE
