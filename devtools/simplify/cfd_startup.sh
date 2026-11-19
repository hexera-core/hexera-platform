#!/bin/bash
# Responsibility: Startup script of the CFD bench VM dev-raymond-cfd (CAD simplification phase P4): docker plus the repo's mesh image (OpenFOAM v2412, snappyHexMesh, cfMesh) pinned by digest, a Python venv for the OCP geometry side, and the 45-minute idle watchdog of bench_startup.sh.
# Boundaries: runs as root at every boot of that one hand-made VM; installs once (marker /opt/bench/.installed), pulls the image only when it is missing. Nothing in the product uses it.
# Collaborates with: devtools/simplify/bench_startup.sh (the watchdog it copies), devtools/simplify/cfd_harness.py (expects image tag hexera-mesh:cfd and /opt/bench/venv), Dockerfile stage `mesh`.
# The VM's service account token (metadata server) logs docker in to Artifact Registry.
set -u
MESH_IMAGE=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh@sha256:02756d09ddc29a5e109cdd466567011fdfc1ef22a54a46ac5d4ed485d1989c3e
if [ ! -f /opt/bench/.installed ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -yq docker.io libglu1-mesa libxrender1 libxcursor1 libxft2 libxinerama1 libgl1 libegl1 libgomp1 rsync git time htop jq
  systemctl enable --now docker
  mkdir -p /opt/bench /scratch && chmod 1777 /scratch
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
  /usr/local/bin/uv venv --python 3.12 /opt/bench/venv
  /usr/local/bin/uv pip install --python /opt/bench/venv/bin/python cadquery-ocp==7.9.3.1.1 numpy scipy
  chmod -R a+rX /opt/bench
  touch /opt/bench/.installed
fi
if ! docker image inspect "$MESH_IMAGE" >/dev/null 2>&1; then
  tok=$(curl -fsS -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token | jq -r .access_token)
  echo "$tok" | docker login -u oauth2accesstoken --password-stdin https://us-central1-docker.pkg.dev \
    && docker pull "$MESH_IMAGE" && docker tag "$MESH_IMAGE" hexera-mesh:cfd
fi
# Idle watchdog: power off after 45 min with no ssh session, no running container and no bench
# python or OpenFOAM process.
cat > /usr/local/bin/bench-idle-check <<'W'
#!/bin/bash
f=/run/bench-idle-since
busy=0
who | grep -q . && busy=1
[ -n "$(docker ps -q 2>/dev/null)" ] && busy=1
pgrep -f 'simpleFoam|snappyHexMesh|blockMesh|/opt/bench/venv/bin/python' >/dev/null && busy=1
if [ $busy = 1 ]; then rm -f $f; exit 0; fi
[ -f $f ] || date +%s > $f
[ $(( $(date +%s) - $(cat $f) )) -ge 2700 ] && /sbin/shutdown -h now "bench idle 45 min"
W
chmod +x /usr/local/bin/bench-idle-check
echo '*/5 * * * * root /usr/local/bin/bench-idle-check' > /etc/cron.d/bench-idle
