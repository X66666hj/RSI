#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
BASE="$ROOT/base"
mkdir -p "$BASE"
if [ ! -f "$BASE/.extracted" ]; then
  tar --exclude='./dev/*' -xzf "$ROOT/ubuntu-base.tar.gz" -C "$BASE"
  touch "$BASE/.extracted"
fi
mkdir -p "$BASE/dev" "$BASE/proc" "$BASE/tmp" "$BASE/app" "$BASE/logs/verifier"
chmod 1777 "$BASE/tmp"
for item in 'null 1 3' 'zero 1 5' 'random 1 8' 'urandom 1 9' 'tty 5 0'; do
  read -r name major minor <<< "$item"
  [ -e "$BASE/dev/$name" ] || mknod -m 666 "$BASE/dev/$name" c "$major" "$minor"
done
cp /etc/resolv.conf "$BASE/etc/resolv.conf"
mkdir -p "$BASE/etc/ssl/certs"
cp /etc/ssl/certs/ca-certificates.crt "$BASE/etc/ssl/certs/ca-certificates.crt"
cat > "$BASE/etc/apt/sources.list.d/ubuntu.sources" <<'EOF'
Types: deb
URIs: https://mirrors.ustc.edu.cn/ubuntu/
Suites: noble noble-updates noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
cat > "$BASE/etc/apt/apt.conf.d/99native" <<'EOF'
APT::Sandbox::User "root";
Acquire::Retries "3";
Acquire::Languages "none";
EOF
chroot "$BASE" /bin/bash -c 'export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv python-is-python3 ca-certificates bash coreutils findutils grep sed gawk tar gzip xz-utils unzip git gcc g++ make libc6-dev curl wget openssl procps time file patch jq'
chroot "$BASE" /bin/bash -c 'python -m pip install --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple pytest==8.4.1 pytest-json-ctrf==0.3.5 uv==0.9.5'
touch "$BASE/.ready"
du -sh "$BASE"
