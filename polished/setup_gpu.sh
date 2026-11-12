#!/usr/bin/env bash
# Lane 2 (Vast.ai or any Ubuntu + NVIDIA box): install the SAME recipe the Modal image uses.
#   bash polished/setup_gpu.sh <step>     steps: apt ffmpeg pip models check   (no step = all, in order)
# The conductor runs each step as its own time-boxed SSH command, so a hang names its step (12 Nov: a
# one-shot setup sat 30 min on a paid box with no clue which part hung).
# No tokens of any kind are needed or put here (owner rule: no YouTube/Twitch tokens on Vast hosts).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
PY=${PY:-python3}
FFV=${FFV:-n8.1}

step_apt() {
  apt-get -o Acquire::http::Timeout=20 -o Acquire::Retries=2 update -qq
  apt-get install -y -qq fonts-dejavu-core xz-utils curl >/dev/null
  echo "apt done"
}

step_ffmpeg() {
  # ffmpeg >= 5.1 is required (-fps_mode). Ubuntu 22.04 ships 4.4, which also could not open NVENC on a current
  # driver (12 Nov). Static GPL build with NVENC from BtbN's releases; download speed is printed.
  mkdir -p /opt/ffmpeg
  curl -fsSL --connect-timeout 20 --max-time 240 -o /tmp/ff.tar.xz -w "ffmpeg download %{size_download} B at %{speed_download} B/s\n" \
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-${FFV}-latest-linux64-gpl-${FFV#n}.tar.xz"
  tar xJf /tmp/ff.tar.xz -C /opt/ffmpeg --strip-components=1
  rm -f /tmp/ff.tar.xz
  echo 'export PATH=/opt/ffmpeg/bin:$PATH' > /etc/profile.d/00-ffmpeg.sh
  /opt/ffmpeg/bin/ffmpeg -hide_banner -version | head -1
}

step_pip() {
  $PY -m pip install -q --progress-bar off -r polished/requirements-gpu.txt
  SITE=$($PY -c "import site;print(next(p for p in site.getsitepackages() if p.endswith('site-packages')))")
  echo "export LD_LIBRARY_PATH=$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:\${LD_LIBRARY_PATH:-}" > /etc/profile.d/polished.sh
  echo "export POLISHED_KIT=$(pwd)/polished/kit PYTHONUNBUFFERED=1" >> /etc/profile.d/polished.sh
  echo "pip done"
}

step_models() {
  . /etc/profile.d/polished.sh
  $PY - <<'PYEOF'
import time
t = time.time()
from faster_whisper import WhisperModel
WhisperModel("large-v3", device="cpu", compute_type="int8")
print("whisper weights in %.0f s" % (time.time() - t))
import open_clip
open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
print("models done")
PYEOF
}

step_check() {
  export PATH=/opt/ffmpeg/bin:$PATH
  ffmpeg -hide_banner -h full > /tmp/ffhelp.txt 2>/dev/null || true      # no pipe: grep -q + pipefail = false "too old"
  grep -q -- "-fps_mode" /tmp/ffhelp.txt || { echo "ffmpeg too old (no -fps_mode)"; exit 1; }
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  if ffmpeg -v error -f lavfi -i testsrc=size=1920x1080:rate=60:duration=0.5 -c:v h264_nvenc -f null - ; then
    echo "NVENC ok"
  else
    echo "NVENC unavailable here: renders fall back to libx264"
  fi
  echo SETUP_DONE
}

if [ $# -eq 0 ]; then set -- apt ffmpeg pip models check; fi
for s in "$@"; do "step_$s"; done
