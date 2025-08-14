#!/usr/bin/env bash
# Prepare Ubuntu 24.10 for Django 5.2 on Python 3.12
# - Installs Python toolchain, build deps, (optional) Postgres/Redis/Nginx
# - Creates venv and installs common Python packages
# - (Optional) Creates Postgres DB/user
# - (Optional) systemd service for Uvicorn + (Optional) Nginx reverse proxy
# Usage:
#   APP_USER=ubuntu PROJECT_DIR=/home/ubuntu/git/dj INSTALL_NGINX=0 INSTALL_POSTGRES=0 CREATE_DB=0 \
#   DB_NAME=mydb DB_USER=myuser DB_PASSWORD='mypassword' sudo -E bash django_ubuntu.sh

set -Eeuo pipefail
IFS=$'\n\t'

log()  { printf "\033[1;32m[INFO]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*" >&2; }
err()  { printf "\033[1;31m[ERROR]\033[0m %s\n" "$*" >&2; }

trap 'err "Failed at line $LINENO"; exit 1' ERR

# --- Config (env overrides) ---
APP_USER="${APP_USER:-${SUDO_USER:-ubuntu}}"
PROJECT_NAME="${PROJECT_NAME:-dj_tmp_prj}"
PROJECT_DIR="${PROJECT_DIR:-/home/${APP_USER}/${PROJECT_NAME}}"
TIMEZONE="${TIMEZONE:-Europe/Moscow}"

INSTALL_POSTGRES="${INSTALL_POSTGRES:-0}"
INSTALL_REDIS="${INSTALL_REDIS:-0}"
INSTALL_NGINX="${INSTALL_NGINX:-0}"
SETUP_SYSTEMD="${SETUP_SYSTEMD:-0}"
CREATE_DB="${CREATE_DB:-0}"
CONFIGURE_UFW="${CONFIGURE_UFW:-0}"
INSTALL_FASTSTREAM="${INSTALL_FASTSTREAM:-0}" 

DB_NAME="${DB_NAME:-mydb}"
DB_USER="${DB_USER:-myuser}"
DB_PASSWORD="${DB_PASSWORD:-mypassword}"

SERVER_NAME="${SERVER_NAME:-_}"    
UVICORN_PORT="${UVICORN_PORT:-8000}"
UVICORN_WORKERS="${UVICORN_WORKERS:-2}"
SERVICE_NAME="${SERVICE_NAME:-${PROJECT_NAME}}"

ENV_DIR="${ENV_DIR:-${PROJECT_DIR}}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/.env}"

# --- OS sanity checks ---
if [[ $EUID -ne 0 ]]; then err "Run as root (use sudo)."; exit 1; fi
source /etc/os-release || true
if [[ "${ID:-}" != "ubuntu" ]]; then warn "This targets Ubuntu; detected: ${ID:-unknown}"; fi
if [[ "${VERSION_ID:-}" != "24.10" ]]; then warn "Script tuned for Ubuntu 24.10; detected: ${VERSION_ID:-unknown}. Continuing in 3s..."; sleep 3; fi

# --- Ensure user exists ---
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  err "User '${APP_USER}' does not exist."
  exit 1
fi

# --- Apt packages ---
export DEBIAN_FRONTEND=noninteractive
log "Updating apt cache and upgrading..."
apt-get update -y
apt-get -o Dpkg::Options::="--force-confold" upgrade -y

log "Installing base packages..."
BASE_PKGS=(python3 python3-venv python3-pip python3-dev build-essential git curl pkg-config openssl
           libpq-dev libssl-dev libffi-dev zlib1g-dev libjpeg-dev)
APT_PKGS=("${BASE_PKGS[@]}")
(( INSTALL_POSTGRES )) && APT_PKGS+=(postgresql postgresql-contrib)
(( INSTALL_REDIS ))    && APT_PKGS+=(redis-server)
(( INSTALL_NGINX ))    && APT_PKGS+=(nginx)
(( CONFIGURE_UFW ))    && APT_PKGS+=(ufw)

apt-get install -y "${APT_PKGS[@]}"

# --- Services enable/start ---
(( INSTALL_POSTGRES )) && systemctl enable --now postgresql
(( INSTALL_REDIS ))    && systemctl enable --now redis-server
(( INSTALL_NGINX ))    && systemctl enable --now nginx || true

# --- Timezone ---
if command -v timedatectl >/dev/null 2>&1; then
  log "Setting timezone to ${TIMEZONE}..."
  timedatectl set-timezone "${TIMEZONE}" || warn "Could not set timezone."
fi

# --- Project directory & venv ---
log "Preparing project directory: ${PROJECT_DIR}"
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 "${PROJECT_DIR}"

log "Creating Python 3.12 virtualenv and installing packages..."
sudo -u "${APP_USER}" bash -lc "
  cd '${PROJECT_DIR}' && \
  python3 -m venv .venv && \
  source .venv/bin/activate && \
  python -m pip install --upgrade pip setuptools wheel && \
  pip install \
    'Django~=5.2.0' \
    'uvicorn[standard]>=0.29' \
    'starlette>=0.37,<1' \
    'whitenoise>=6.6' \
    'django-redis>=5.4' \
    'structlog>=24.1' \
    'psycopg[binary]>=3.2' \
    'django-environ>=0.11' \
    'pydantic-settings>=2.2'
"

if (( INSTALL_FASTSTREAM )); then
  log "Installing FastStream (optional)..."
  sudo -u "${APP_USER}" bash -lc "
    source '${PROJECT_DIR}/.venv/bin/activate' && \
    pip install 'faststream[redis]>=0.5'
  "
fi

# --- Optional: Postgres DB/user ---
if (( INSTALL_POSTGRES && CREATE_DB )); then
  log "Ensuring Postgres role '${DB_USER}' and database '${DB_NAME}'..."
  ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" || true)
  if [[ "${ROLE_EXISTS}" != "1" ]]; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE ROLE \"${DB_USER}\" LOGIN PASSWORD '${DB_PASSWORD}';"
  else
    log "Role '${DB_USER}' already exists."
  fi
  DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" || true)
  if [[ "${DB_EXISTS}" != "1" ]]; then
    sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
  else
    log "Database '${DB_NAME}' already exists."
  fi
fi

# --- Environment file ---
log "Creating environment file at ${ENV_FILE}..."
install -d -m 0755 -o root -g root "${ENV_DIR}"
SECRET="$(openssl rand -hex 32)"
{
  echo "DJANGO_SETTINGS_MODULE=config.settings.local"
  echo "DJANGO_DEBUG=1"
  echo "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,${SERVER_NAME}"
  echo "DJANGO_SECRET_KEY=${SECRET}"
  if (( INSTALL_POSTGRES )); then
    echo "DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"
  else
    echo "# DATABASE_URL=sqlite:////${PROJECT_DIR}/db.sqlite3"
  fi
  echo "REDIS_CACHE_URL=redis://127.0.0.1:6379/1"
  echo "FASTSTREAM_REDIS_URL=redis://127.0.0.1:6379/2"
  echo "DJANGO_LOG_JSON=0"
  echo "LOG_LEVEL=INFO"
} > "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"
chown root:"${APP_USER}" "${ENV_FILE}"

# --- Optional: systemd service for Uvicorn ---
if (( SETUP_SYSTEMD )); then
  log "Creating systemd service: ${SERVICE_NAME}.service"
  cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Django ASGI (Uvicorn) - ${PROJECT_NAME}
After=network.target $( (( INSTALL_POSTGRES )) && echo postgresql.service ) $( (( INSTALL_REDIS )) && echo redis-server.service )

[Service]
User=${APP_USER}
Group=www-data
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${PROJECT_DIR}/.venv/bin/uvicorn config.asgi:application --host 0.0.0.0 --port ${UVICORN_PORT} --workers ${UVICORN_WORKERS} --proxy-headers
Restart=always
RestartSec=5
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"

  if [[ -f "${PROJECT_DIR}/config/asgi.py" ]]; then
    log "Starting ${SERVICE_NAME} service..."
    systemctl start "${SERVICE_NAME}" || warn "Service failed to start. Ensure project code is present."
  else
    warn "ASGI module not found at ${PROJECT_DIR}/config/asgi.py. Skipping service start."
  fi
fi

# --- Optional: Nginx reverse proxy ---
if (( INSTALL_NGINX )); then
  log "Configuring Nginx reverse proxy (server_name: ${SERVER_NAME})..."
  NGINX_CONF="/etc/nginx/sites-available/${PROJECT_NAME}.conf"
  cat > "${NGINX_CONF}" <<NGINX
server {
    listen 80;
    server_name ${SERVER_NAME};

    client_max_body_size 25m;

    location /static/ {
        alias ${PROJECT_DIR}/staticfiles/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location /media/ {
        alias ${PROJECT_DIR}/media/;
    }

    location / {
        proxy_pass http://127.0.0.1:${UVICORN_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
NGINX

  ln -sf "${NGINX_CONF}" "/etc/nginx/sites-enabled/${PROJECT_NAME}.conf"
  # Remove default site if present
  if [[ -f /etc/nginx/sites-enabled/default ]]; then rm -f /etc/nginx/sites-enabled/default; fi
  nginx -t && systemctl reload nginx
fi

# --- Optional: UFW firewall ---
if (( CONFIGURE_UFW )); then
  log "Configuring UFW firewall..."
  ufw allow OpenSSH
  if (( INSTALL_NGINX )); then
    ufw allow 'Nginx Full'
  else
    ufw allow "${UVICORN_PORT}/tcp"
  fi
  ufw --force enable
fi

# --- Summary ---
log "Done! Summary:"
echo "  Python:     $(python3 --version 2>/dev/null || echo 'python3 not found')"
echo "  Project:    ${PROJECT_DIR}"
echo "  Venv:       ${PROJECT_DIR}/.venv"
echo "  Service:    ${SERVICE_NAME} (systemd) $(systemctl is-enabled "${SERVICE_NAME}" 2>/dev/null || echo 'disabled')"
if (( INSTALL_NGINX )); then
  echo "  Nginx:      reverse proxy enabled (server_name: ${SERVER_NAME})"
fi
echo "  Env file:   ${ENV_FILE}"
echo ""
echo "Next steps:"
echo "  1) Put your Django project code in ${PROJECT_DIR}"
echo "  2) source ${PROJECT_DIR}/.venv/bin/activate && pip install -r requirements.txt (if you have one)"
echo "  3) source ${PROJECT_DIR}/.venv/bin/activate && python manage.py collectstatic --noinput"
echo "  4) systemctl restart ${SERVICE_NAME}"