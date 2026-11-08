#!/bin/sh
# Run explicitly as root on Ubuntu 24.04. Does not remove existing container runtimes.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root'; exit 1; }
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || { echo 'Requires Ubuntu 24.04'; exit 1; }
if command -v docker >/dev/null 2>&1; then
    docker compose version
    echo 'Docker is already installed; no packages changed.'
    exit 0
fi
if dpkg-query -W -f='${Status}' docker.io containerd podman-docker 2>/dev/null | grep -q 'install ok installed'; then
    echo 'Existing runtime detected. Resolve package compatibility before installing Docker CE.'
    exit 1
fi
apt-get update
apt-get install -y ca-certificates curl python3
install -d -m 0755 /etc/apt/keyrings
curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable\n' "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker compose version
