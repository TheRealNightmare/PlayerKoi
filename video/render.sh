#!/usr/bin/env bash
# Render every scene at 1080p60 and join them into out/player_koi_explainer.mp4.
# Usage: ./render.sh          (final, 1080p60)
#        ./render.sh -ql      (quick 480p15 preview)
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/.conda/bin:$PATH"

QUALITY="${1:--qh}"
case "$QUALITY" in
  -ql) DIR=480p15 ; EXTRA=() ;;
  *)   DIR=1080p60; EXTRA=(--fps 60) ;;
esac
SCENES=(Hook CoreIdea Vision RobotArm CoachSafety Outro)

for s in "${SCENES[@]}"; do
  manim "$QUALITY" "${EXTRA[@]}" --progress_bar none scenes.py "$s"
done

mkdir -p out
LIST=out/concat.txt
: > "$LIST"
for s in "${SCENES[@]}"; do
  echo "file '$PWD/media/videos/scenes/$DIR/$s.mp4'" >> "$LIST"
done
# re-encode so audio/video timestamps line up cleanly across the cuts
# loudnorm brings the voice to YouTube's -14 LUFS target
ffmpeg -y -v error -f concat -safe 0 -i "$LIST" \
  -af loudnorm=I=-14:TP=-1.5:LRA=11 -ar 48000 \
  -c:v libx264 -crf 18 -preset slow -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart \
  out/player_koi_explainer.mp4
echo "→ out/player_koi_explainer.mp4"
