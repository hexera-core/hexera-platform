#!/bin/bash
# dev-raymond-bench: CAD simplification bench. Installs the solver stack once, then an idle watchdog.
set -u
if [ ! -f /opt/bench/.installed ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -yq calculix-ccx libglu1-mesa libxrender1 libxcursor1 libxft2 libxinerama1 libgl1 libegl1 libgomp1 rsync git time htop
  mkdir -p /opt/bench /scratch && chmod 1777 /scratch
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
  /usr/local/bin/uv venv --python 3.12 /opt/bench/venv
  /usr/local/bin/uv pip install --python /opt/bench/venv/bin/python gmsh==4.15.2 cadquery-ocp==7.9.3.1.1 meshio==5.3.5 numpy scipy trimesh==4.8.3
  chmod -R a+rX /opt/bench
  touch /opt/bench/.installed
fi
# Idle watchdog: power off after 45 min with no ssh session and no ccx/gmsh/python bench process.
cat > /usr/local/bin/bench-idle-check <<'W'
#!/bin/bash
f=/run/bench-idle-since
busy=0
who | grep -q . && busy=1
pgrep -f 'ccx|gmsh|/opt/bench/venv/bin/python' >/dev/null && busy=1
if [ $busy = 1 ]; then rm -f $f; exit 0; fi
[ -f $f ] || date +%s > $f
[ $(( $(date +%s) - $(cat $f) )) -ge 2700 ] && /sbin/shutdown -h now "bench idle 45 min"
W
chmod +x /usr/local/bin/bench-idle-check
echo '*/5 * * * * root /usr/local/bin/bench-idle-check' > /etc/cron.d/bench-idle
