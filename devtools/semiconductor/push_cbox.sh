#!/bin/bash
# push_cbox.sh <host> <unit> <label> <bg> <lcool> <lfine> <lchip> <lskin> <ranks> <threads>
set -u
H=${1:-corintis-meshlab3}; U=${2:-cboxp}; LAB=${3:-cboxp}
BG=${4:-0.0014}; LC=${5:-0}; LF=${6:-1}; LH=${7:-1}; LS=${8:-0}; RK=${9:-8}; TH=${10:-4}
A=--account=rehaan@hexera.ai
Z="--zone us-central1-a --tunnel-through-iap"
S=/mnt/c/Users/rehaa/AppData/Local/Temp/claude/C--Users-rehaa-OneDrive-Documents-GitHub-chandelier/d31ea0b1-549f-4292-b3a1-c87888a1d0e2/scratchpad
C=/home/rehaan/clean_stl
cd "$C"
for f in domain_box block fluid fluid_fine chip_fit; do
  [ -s "$f.stl" ] || { echo "FATAL $f.stl missing"; exit 1; }
done

# THE GATE, before anything leaves this machine. On 2026-11-05 a fluid.stl that was already two
# surface pieces went to the box, meshed for 1,013 s, and came back as a coolant in four
# disconnected regions - which I nearly took to Corintis as a question about their CAD. The second
# piece was 1,596 triangles and 1.496762e-06 m3 and the mesh's island was 1.4979e-06, a 0.07%
# match. This refuses that surface in under a second, and it knows the chip is legitimately two
# shells and that fluid_fine is a refinement surface with no inside to check.
PY=/home/rehaan/hexera-platform-semi/.venv/bin/python
[ -x "$PY" ] || PY=python3
"$PY" /home/rehaan/hexera-platform-semi/devtools/semiconductor/surface_gate.py --coupled "$C" \
  || { echo "FATAL: the surface gate refused. Nothing pushed."; exit 1; }

for f in domain_box block fluid fluid_fine chip_fit; do
  cp "$f.stl" "cb_$f.stl"
done
ls -la cb_*.stl
gcloud $A compute scp cb_domain_box.stl cb_block.stl cb_fluid.stl cb_fluid_fine.stl cb_chip_fit.stl \
  $H:/tmp/ $Z 2>&1 | grep -viE "numpy|please see|^warning" || true
gcloud $A compute scp "$S/couple_box.sh" $H:/tmp/couple_box.sh $Z 2>&1 | grep -viE "numpy|please see|^warning" || true
gcloud $A compute ssh $H $Z --command \
  "sudo mv /tmp/cb_*.stl /mnt/work/ && sudo ls -la /mnt/work/cb_*.stl && sudo install -m 755 /tmp/couple_box.sh /root/couple_box.sh && sudo systemd-run --unit=$U --no-block /bin/bash /root/couple_box.sh $LAB $BG $LC $LF $LH $LS $RK $TH && sleep 25 && sudo systemctl is-active $U && sudo tail -14 /var/log/$LAB.log" \
  2>&1 | grep -viE "numpy|please see|^warning"
