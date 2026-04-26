#!/usr/bin/env bash
# Job Hunt AI Agent — one-shot deploy for a fresh Ubuntu 24.04 droplet.
#
# USAGE:
#   1. SSH into your VPS as a sudo user (root, ubuntu, etc.)
#   2. Clone the repo:   git clone <your-repo-url> hunt && cd hunt
#   3. Copy your secrets: nano .env   (or scp .env from your laptop)
#   4. Run:              sudo ./deploy.sh
#
# What it does:
#   - Installs Docker + Compose if missing
#   - Opens firewall ports 80 + 443
#   - Asks if you have a domain (for HTTPS via Let's Encrypt)
#   - Builds the image + brings up the stack
#   - Prints the URL where the app is live
#
# Idempotent: re-running just rebuilds the image and restarts services.

set -euo pipefail

GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; RESET=$'\e[0m'
log()  { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s!!%s %s\n'  "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%sxx%s %s\n'  "$RED"    "$RESET" "$*" >&2; exit 1; }

# --- 1. Sanity checks --------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run with sudo: sudo $0"
[[ -f Dockerfile ]] || die "Run from the repo root (Dockerfile must be in cwd)"

if [[ ! -f .env ]]; then
    warn ".env file is missing. Copy .env.example -> .env and fill it in first."
    if [[ -f .env.example ]]; then
        warn "A template exists at .env.example."
    fi
    die "Cannot continue without .env"
fi

# --- 2. Install Docker if needed --------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker..."
    apt-get update -qq
    apt-get install -y --no-install-recommends ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
else
    log "Docker already installed: $(docker --version)"
fi

# --- 3. Firewall -------------------------------------------------------------
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    log "Opening UFW ports 80, 443"
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
fi

# --- 4. Domain / HTTPS setup -------------------------------------------------
mkdir -p nginx/conf.d
DOMAIN=""
read -rp "Domain name (e.g. hunt.example.com), or blank to use raw IP / HTTP: " DOMAIN
DOMAIN="${DOMAIN// /}"

if [[ -n "$DOMAIN" ]]; then
    log "Configuring HTTPS for $DOMAIN"

    read -rp "Email for Let's Encrypt expiry alerts: " LE_EMAIL
    [[ -n "$LE_EMAIL" ]] || die "Email required for Let's Encrypt"

    log "Verifying DNS: $DOMAIN should resolve to this server's public IP"
    SERVER_IP=$(curl -fsS https://api.ipify.org || echo "?")
    DOMAIN_IP=$(getent hosts "$DOMAIN" | awk 'NR==1 {print $1}' || echo "?")
    if [[ "$SERVER_IP" != "$DOMAIN_IP" ]]; then
        warn "DNS mismatch: $DOMAIN -> $DOMAIN_IP, but this server is $SERVER_IP"
        warn "Let's Encrypt will likely fail unless you fix the A record first."
        read -rp "Continue anyway? [y/N] " yn
        [[ "$yn" =~ ^[Yy]$ ]] || die "Aborted by user"
    fi

    # Stage 1: bring up nginx with HTTP-only so certbot can solve the challenge.
    cp nginx/conf.d/http.conf.template nginx/conf.d/default.conf
    docker compose up -d --build hunt nginx
    sleep 3

    log "Requesting Let's Encrypt certificate for $DOMAIN"
    docker compose run --rm \
        -v ./nginx/conf.d:/etc/nginx/conf.d \
        certbot certonly \
            --webroot -w /var/www/certbot \
            -d "$DOMAIN" \
            --email "$LE_EMAIL" \
            --agree-tos --no-eff-email --non-interactive \
        || die "Let's Encrypt failed. Check that $DOMAIN points to $SERVER_IP and port 80 is open."

    # Stage 2: switch nginx to the HTTPS config.
    sed "s/DOMAIN_PLACEHOLDER/$DOMAIN/g" \
        nginx/conf.d/https.conf.template > nginx/conf.d/default.conf

    docker compose up -d --build
    docker compose restart nginx

    log "Live at: https://$DOMAIN"
else
    log "No domain — using HTTP-only on raw IP."
    cp nginx/conf.d/http.conf.template nginx/conf.d/default.conf
    docker compose up -d --build
    SERVER_IP=$(curl -fsS https://api.ipify.org || echo "your-server-ip")
    log "Live at: http://$SERVER_IP"
    warn "HTTP only — credentials sent to this app are NOT encrypted in transit."
    warn "Get a domain (Namecheap .xyz ~ Rs100/year) and re-run for HTTPS."
fi

# --- 5. Final status --------------------------------------------------------
log "Container status:"
docker compose ps

log "Recent logs (Ctrl+C to exit):"
docker compose logs --tail=20 hunt
