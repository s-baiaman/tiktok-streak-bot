#!/usr/bin/env bash
# TikTok Streak Panel installer for Debian/Ubuntu servers.
# Installs everything it needs, asks for a panel password, and starts the panel in Docker.
# Safe to re-run: it offers to update or change the password and settings.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. root -------------------------------------------------------------------
if [[ "$(uname -s)" != "Linux" ]]; then
    die "This installer supports Linux servers only."
fi
if [[ $EUID -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        info "Re-running with sudo…"
        exec sudo -E bash "$(readlink -f "$0")" "$@"
    fi
    die "Please run as root (or install sudo)."
fi

# --- 2. distro -----------------------------------------------------------------
[[ -r /etc/os-release ]] || die "Can't read /etc/os-release; unsupported system."
# shellcheck disable=SC1091
. /etc/os-release
DOCKER_DISTRO=""
CODENAME="${VERSION_CODENAME:-}"
case "${ID:-}" in
    debian) DOCKER_DISTRO=debian ;;
    ubuntu) DOCKER_DISTRO=ubuntu ;;
    *)
        if [[ " ${ID_LIKE:-} " == *" ubuntu "* ]]; then
            DOCKER_DISTRO=ubuntu
            CODENAME="${UBUNTU_CODENAME:-$CODENAME}"
        elif [[ " ${ID_LIKE:-} " == *" debian "* ]]; then
            DOCKER_DISTRO=debian
        fi
        warn "${PRETTY_NAME:-This distro} isn't officially supported (Debian and Ubuntu are). Continuing anyway."
        ;;
esac
command -v apt-get >/dev/null 2>&1 || die "apt-get not found. Only Debian/Ubuntu-based systems are supported."

# --- 3. basic packages ---------------------------------------------------------
APT_UPDATED=0
apt_install() {
    local missing=()
    local pkg
    for pkg in "$@"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    [[ ${#missing[@]} -eq 0 ]] && return 0
    if [[ $APT_UPDATED -eq 0 ]]; then
        info "Updating package lists…"
        apt-get update -qq
        APT_UPDATED=1
    fi
    info "Installing: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
}

apt_install ca-certificates curl git gnupg tzdata iproute2

# --- 4. docker -----------------------------------------------------------------
install_docker() {
    [[ -n "$DOCKER_DISTRO" && -n "$CODENAME" ]] || die "Can't work out which Docker repository to use. Install Docker manually, then re-run."
    info "Installing Docker Engine from Docker's official repository…"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DOCKER_DISTRO} ${CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    APT_UPDATED=0
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

if ! command -v docker >/dev/null 2>&1; then
    install_docker
fi
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl enable --now docker >/dev/null 2>&1 || true
fi
if ! docker compose version >/dev/null 2>&1; then
    info "Installing the Docker Compose plugin…"
    apt_install docker-compose-plugin 2>/dev/null || apt_install docker-compose-v2 \
        || die "Couldn't install Docker Compose. Install the docker compose plugin manually, then re-run."
fi

# --- 5. docker actually works? ---------------------------------------------------
is_lxc() {
    if command -v systemd-detect-virt >/dev/null 2>&1 && [[ "$(systemd-detect-virt -c 2>/dev/null || true)" == "lxc" ]]; then
        return 0
    fi
    grep -qa 'container=lxc' /proc/1/environ 2>/dev/null
}

info "Checking that Docker can run containers…"
for _ in $(seq 1 30); do  # a freshly installed daemon can take a few seconds to come up
    docker info >/dev/null 2>&1 && break
    sleep 1
done
if ! docker_output="$(docker run --rm hello-world 2>&1)"; then
    if is_lxc; then
        bold "This looks like a Proxmox LXC container without nesting, so Docker can't run here."
        echo "On the Proxmox host, run (replace <CTID> with this container's ID):"
        echo
        echo "    pct set <CTID> --features nesting=1,keyctl=1 && pct reboot <CTID>"
        echo
        echo "Then run ./install.sh again inside the container."
        exit 1
    fi
    printf '%s\n' "$docker_output" | tail -5 >&2
    die "Docker is installed but can't run containers (see above)."
fi

# --- 6. existing install? --------------------------------------------------------
IMAGE=tiktok-streak-bot:latest
prepare_data_dir() {  # the container runs as uid 1000 and keeps its database and keys here
    mkdir -p data
    chown 1000:1000 data
}
declare -A CURRENT=()
if [[ -f .env ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        value="${value%\'}"
        value="${value#\'}"
        CURRENT[$key]="$value"
    done < .env
    bold "The panel is already installed."
    echo "  1) Update (download the latest version and rebuild)"
    echo "  2) Change the password or settings"
    echo "  3) Exit"
    read -rp "Choose 1-3: " choice
    case "$choice" in
        1)
            info "Updating…"
            git pull --ff-only
            prepare_data_dir
            docker compose up -d --build
            docker compose ps
            exit 0
            ;;
        2) ;;
        *) exit 0 ;;
    esac
fi

# --- 7. questions ---------------------------------------------------------------
ask_password() {
    local first second
    while true; do
        read -rsp "Panel password (at least 8 characters${CURRENT[PANEL_PASSWORD_HASH]:+; Enter keeps the current one}): " first
        echo
        if [[ -z "$first" && -n "${CURRENT[PANEL_PASSWORD_HASH]:-}" ]]; then
            PANEL_PASSWORD=""
            return
        fi
        if [[ ${#first} -lt 8 ]]; then
            warn "Use at least 8 characters."
            continue
        fi
        read -rsp "Type it again: " second
        echo
        if [[ "$first" != "$second" ]]; then
            warn "The passwords didn't match. Try again."
            continue
        fi
        PANEL_PASSWORD="$first"
        return
    done
}

ask_port() {
    local value current="${CURRENT[PANEL_PORT]:-8080}"
    while true; do
        read -rp "Port for the panel [$current]: " value
        value="${value:-$current}"
        if [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )); then
            PANEL_PORT="$value"
            return
        fi
        warn "Use a number from 1 to 65535."
    done
}

ask_timezone() {
    local detected value
    detected="${CURRENT[DEFAULT_TIMEZONE]:-}"
    if [[ -z "$detected" ]] && command -v timedatectl >/dev/null 2>&1; then
        detected="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    fi
    if [[ -z "$detected" && -f /etc/timezone ]]; then
        detected="$(cat /etc/timezone)"
    fi
    detected="${detected:-UTC}"
    while true; do
        read -rp "Default timezone for new bots (e.g. Europe/Moscow) [$detected]: " value
        value="${value:-$detected}"
        if [[ "$value" == "UTC" || -f "/usr/share/zoneinfo/$value" ]]; then
            DEFAULT_TIMEZONE="$value"
            return
        fi
        warn "Unknown timezone. Use a name like Europe/Moscow or America/New_York."
    done
}

bold "Setup"
echo "The panel is protected by one password. Anyone on your home network who knows it can manage the bots."
ask_password
ask_port
ask_timezone

# --- 8. build, hash the password, write config, start ------------------------------
info "Building the panel (the first build downloads a browser, this takes a few minutes)…"
docker compose build

if [[ -n "$PANEL_PASSWORD" ]]; then
    PANEL_PASSWORD_HASH="$(printf '%s\n' "$PANEL_PASSWORD" | docker run --rm -i "$IMAGE" python -m streakbot hash-password)"
    [[ "$PANEL_PASSWORD_HASH" == scrypt\$* ]] || die "Couldn't create the password hash."
else
    PANEL_PASSWORD_HASH="${CURRENT[PANEL_PASSWORD_HASH]}"
fi
unset PANEL_PASSWORD

info "Saving settings to .env"
umask 077
{
    # Single quotes stop Docker Compose from treating the $ signs in the hash as variables.
    printf "PANEL_PASSWORD_HASH='%s'\n" "$PANEL_PASSWORD_HASH"
    printf 'PANEL_PORT=%s\n' "$PANEL_PORT"
    printf 'DEFAULT_TIMEZONE=%s\n' "$DEFAULT_TIMEZONE"
    printf 'DATA_DIR=data\n'
} > .env.tmp
mv .env.tmp .env
chmod 600 .env
umask 022

prepare_data_dir

info "Starting the panel…"
docker compose up -d
docker compose ps

LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -1)"
echo
bold "Done! The panel is running and restarts automatically after reboots."
LAN_IP="${LAN_IP:-<this server IP>}"
echo "• Open http://${LAN_IP}:${PANEL_PORT} from a device on your home network."
echo "• Don't forward this port on your router: the panel is meant for your home network only."
echo "• Logs: docker compose logs -f"
