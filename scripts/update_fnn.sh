#!/usr/bin/env bash

set -Eeuo pipefail

REPOSITORY="${CCH_SMOKE_FNN_REPOSITORY:-nervosnetwork/fiber}"
RELEASE_TAG="${CCH_SMOKE_FNN_RELEASE_TAG:-}"
FNN_SOURCE="${CCH_SMOKE_FNN_SOURCE:-${CCH_SMOKE_FNN_VERSION:-release}}"
FNN_PR_NUMBER="${CCH_SMOKE_FNN_PR_NUMBER:-}"
FNN_CONF_URL="${CCH_SMOKE_FNN_CONF_URL:-${CCH_SMOKE_FNN_DEVELOP_CONF_URL:-https://github-test-logs.ckbapp.dev/fiber/fnn.conf}}"
NODE1_DIR="${CCH_SMOKE_NODE1_DIR:-/home/ckb/fiber-test/testnet/node1}"
NODE2_DIR="${CCH_SMOKE_NODE2_DIR:-/home/ckb/fiber-test/testnet/node2}"
SERVICE1="${CCH_SMOKE_FIBER_SERVICE1:-fiber-testnet1.service}"
SERVICE2="${CCH_SMOKE_FIBER_SERVICE2:-fiber-testnet2.service}"
F1_RPC="${CCH_SMOKE_F1_RPC:-http://127.0.0.1:8227}"
F2_RPC="${CCH_SMOKE_F2_RPC:-http://127.0.0.1:8229}"
AUTH_TOKEN="${CCH_SMOKE_FNN_AUTH_TOKEN:-}"
AUTH_TOKEN_FILE="${CCH_SMOKE_FNN_AUTH_TOKEN_FILE:-}"
# Keep the compatibility value only as a non-exported shell variable until it
# is written to a private file. Child processes must never inherit the token.
unset CCH_SMOKE_FNN_AUTH_TOKEN
BACKUP_ROOT="${CCH_SMOKE_FNN_BACKUP_ROOT:-/home/ckb/fiber-test/testnet/.binary-backups}"

TMP_DIR="$(mktemp -d)"
BACKUP_DIR=""
SERVICES_STOPPED=0
BINARIES_REPLACED=0
CLI_REPLACED=0

log() {
  printf '[fnn-update] %s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'required command not found: %s\n' "$1" >&2
    exit 1
  }
}

write_github_output() {
  local name="$1"
  local value="$2"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "$name" "$value" >>"$GITHUB_OUTPUT"
  fi
}

read_fnn_conf_value() {
  local key="$1"
  local conf="$2"
  awk -v key="$key" '
    index($0, key "=") == 1 {
      value = substr($0, length(key) + 2)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value ~ /^".*"$/) {
        value = substr(value, 2, length(value) - 2)
      }
      print value
    }
  ' "$conf" | tail -n 1
}

wait_for_service() {
  local service="$1"
  local attempt
  for attempt in $(seq 1 60); do
    if sudo -n systemctl is-active --quiet "$service"; then
      log "$service is active"
      return 0
    fi
    sleep 1
  done
  sudo -n journalctl -u "$service" -n 100 --no-pager >&2 || true
  return 1
}

start_services() {
  sudo -n systemctl start "$SERVICE1" "$SERVICE2"
  wait_for_service "$SERVICE1"
  wait_for_service "$SERVICE2"
  SERVICES_STOPPED=0
}

rollback() {
  set +e
  log "update failed; restoring previous binaries"
  if [[ "$SERVICES_STOPPED" == "1" || "$BINARIES_REPLACED" == "1" ]]; then
    sudo -n systemctl stop "$SERVICE1" "$SERVICE2"
  fi
  if [[ "$BINARIES_REPLACED" == "1" && -n "$BACKUP_DIR" ]]; then
    sudo -n install -m 0755 "$BACKUP_DIR/node1-fnn" "$NODE1_DIR/fnn"
    sudo -n install -m 0755 "$BACKUP_DIR/node2-fnn" "$NODE2_DIR/fnn"
  fi
  if [[ "$CLI_REPLACED" == "1" && -f "$BACKUP_DIR/node1-fnn-cli" ]]; then
    sudo -n install -m 0755 "$BACKUP_DIR/node1-fnn-cli" "$NODE1_DIR/fnn-cli"
  fi
  if [[ "$SERVICES_STOPPED" == "1" || "$BINARIES_REPLACED" == "1" ]]; then
    sudo -n systemctl start "$SERVICE1" "$SERVICE2"
    sudo -n systemctl is-active "$SERVICE1" "$SERVICE2" >&2
  fi
}

cleanup() {
  local status=$?
  if [[ "$status" != "0" \
    && ("$SERVICES_STOPPED" == "1" \
      || "$BINARIES_REPLACED" == "1" \
      || "$CLI_REPLACED" == "1") ]]; then
    rollback
  fi
  rm -rf "$TMP_DIR"
  exit "$status"
}

trap cleanup EXIT

for command in awk curl jq tar sha256sum sudo systemctl install seq; do
  require_command "$command"
done

if [[ -z "$AUTH_TOKEN_FILE" && -n "$AUTH_TOKEN" ]]; then
  AUTH_TOKEN_FILE="$TMP_DIR/fnn-auth-token"
  (umask 077; printf '%s' "$AUTH_TOKEN" >"$AUTH_TOKEN_FILE")
fi
# The health-check subprocesses receive only the credential file path. Do not
# leave the raw token exported in their environment after converting it.
unset AUTH_TOKEN

for file in \
  "$NODE1_DIR/fnn" \
  "$NODE2_DIR/fnn" \
  "$NODE1_DIR/config.yml" \
  "$NODE2_DIR/config.yml"; do
  [[ -e "$file" ]] || {
    printf 'required file not found: %s\n' "$file" >&2
    exit 1
  }
done

sudo -n true
sudo -n systemctl cat "$SERVICE1" >/dev/null
sudo -n systemctl cat "$SERVICE2" >/dev/null

case "$(uname -m)" in
  x86_64) ASSET_SUFFIX="x86_64-linux-portable.tar.gz" ;;
  aarch64 | arm64) ASSET_SUFFIX="aarch64-linux-portable.tar.gz" ;;
  *)
    printf 'unsupported architecture: %s\n' "$(uname -m)" >&2
    exit 1
    ;;
esac

case "$FNN_SOURCE" in
  latest | release)
    FNN_SOURCE="release"
    [[ -z "$FNN_PR_NUMBER" ]] || {
      printf 'CCH_SMOKE_FNN_PR_NUMBER can only be used with source=pr\n' >&2
      exit 1
    }
    if [[ -n "$RELEASE_TAG" ]]; then
      log "resolving requested release $RELEASE_TAG"
      RELEASE_JSON="$(
        curl -fsSL --retry 3 \
          "https://api.github.com/repos/$REPOSITORY/releases/tags/$RELEASE_TAG"
      )"
    else
      log "resolving latest published release, including prereleases"
      RELEASE_JSON="$(
        curl -fsSL --retry 3 \
          "https://api.github.com/repos/$REPOSITORY/releases?per_page=30" \
          | jq -c '[.[] | select(.draft | not)] | sort_by(.published_at) | last'
      )"
    fi

    TARGET_TAG="$(jq -r '.tag_name // empty' <<<"$RELEASE_JSON")"
    [[ -n "$TARGET_TAG" ]] || {
      printf 'unable to resolve a Fiber release\n' >&2
      exit 1
    }

    ASSET_JSON="$(
      jq -c --arg suffix "$ASSET_SUFFIX" \
        '.assets[] | select(.name | endswith($suffix))' <<<"$RELEASE_JSON"
    )"
    ASSET_NAME="$(jq -r '.name // empty' <<<"$ASSET_JSON")"
    ASSET_URL="$(jq -r '.browser_download_url // empty' <<<"$ASSET_JSON")"
    ASSET_DIGEST="$(jq -r '.digest // empty' <<<"$ASSET_JSON")"

    [[ -n "$ASSET_NAME" && -n "$ASSET_URL" ]] || {
      printf 'release %s has no %s asset\n' "$TARGET_TAG" "$ASSET_SUFFIX" >&2
      exit 1
    }
    [[ "$ASSET_DIGEST" == sha256:* ]] || {
      printf 'release asset %s has no SHA-256 digest\n' "$ASSET_NAME" >&2
      exit 1
    }
    TARGET_LABEL="release $TARGET_TAG"
    ;;
  develop | pr)
    [[ -z "$RELEASE_TAG" ]] || {
      printf 'CCH_SMOKE_FNN_RELEASE_TAG cannot be used with %s\n' \
        "$FNN_SOURCE" >&2
      exit 1
    }

    if [[ "$FNN_SOURCE" == "develop" ]]; then
      [[ -z "$FNN_PR_NUMBER" ]] || {
        printf 'CCH_SMOKE_FNN_PR_NUMBER can only be used with source=pr\n' >&2
        exit 1
      }
      PACKAGE_CHANNEL="develop"
      PACKAGE_DESCRIPTION="develop"
    else
      [[ "$FNN_PR_NUMBER" =~ ^[1-9][0-9]*$ ]] || {
        printf 'source=pr requires a positive CCH_SMOKE_FNN_PR_NUMBER\n' >&2
        exit 1
      }
      PACKAGE_CHANNEL="pr${FNN_PR_NUMBER}"
      PACKAGE_DESCRIPTION="PR #${FNN_PR_NUMBER}"
    fi

    log "resolving $PACKAGE_DESCRIPTION package from $FNN_CONF_URL"
    PACKAGE_CONF="$TMP_DIR/fnn.conf"
    curl -fsSL --retry 3 -o "$PACKAGE_CONF" "$FNN_CONF_URL"

    PACKAGE_BASE_URL="$(read_fnn_conf_value FNN_BASE_URL "$PACKAGE_CONF")"
    ASSET_KEY="TARBALL_${PACKAGE_CHANNEL}"
    ASSET_NAME="$(read_fnn_conf_value "$ASSET_KEY" "$PACKAGE_CONF")"
    [[ -n "$PACKAGE_BASE_URL" && -n "$ASSET_NAME" ]] || {
      printf 'fnn.conf has no FNN_BASE_URL or %s\n' "$ASSET_KEY" >&2
      exit 1
    }

    ESCAPED_ASSET_SUFFIX="${ASSET_SUFFIX//./\\.}"
    PACKAGE_ASSET_PATTERN="^fnn_${PACKAGE_CHANNEL}_[0-9]{8}_[0-9a-f]{7,40}-${ESCAPED_ASSET_SUFFIX}$"
    [[ "$ASSET_NAME" =~ $PACKAGE_ASSET_PATTERN ]] || {
      printf 'unexpected %s package for this architecture: %s\n' \
        "$PACKAGE_DESCRIPTION" "$ASSET_NAME" >&2
      exit 1
    }

    if [[ "$PACKAGE_BASE_URL" == http://* ]]; then
      PACKAGE_BASE_URL="https://${PACKAGE_BASE_URL#http://}"
    fi
    ASSET_URL="${PACKAGE_BASE_URL%/}/$ASSET_NAME"
    ASSET_DIGEST=""
    TARGET_TAG="${ASSET_NAME%.tar.gz}"
    TARGET_LABEL="$PACKAGE_DESCRIPTION package $ASSET_NAME"
    ;;
  *)
    printf 'unsupported Fiber package source: %s (expected release, develop, or pr)\n' \
      "$FNN_SOURCE" >&2
    exit 1
    ;;
esac

write_github_output fnn_package "$TARGET_LABEL"

log "downloading $ASSET_NAME"
curl -fL --retry 3 -o "$TMP_DIR/$ASSET_NAME" "$ASSET_URL"

ACTUAL_SHA256="$(sha256sum "$TMP_DIR/$ASSET_NAME" | awk '{print $1}')"
if [[ -n "$ASSET_DIGEST" ]]; then
  EXPECTED_SHA256="${ASSET_DIGEST#sha256:}"
  [[ "$ACTUAL_SHA256" == "$EXPECTED_SHA256" ]] || {
    printf 'SHA-256 mismatch for %s\n' "$ASSET_NAME" >&2
    exit 1
  }
  log "SHA-256 verified: $ACTUAL_SHA256"
else
  log "downloaded SHA-256 (no upstream digest): $ACTUAL_SHA256"
fi

tar -xzf "$TMP_DIR/$ASSET_NAME" -C "$TMP_DIR"
[[ -f "$TMP_DIR/fnn" ]] || {
  printf 'package %s has no fnn binary\n' "$ASSET_NAME" >&2
  exit 1
}
chmod 0755 "$TMP_DIR/fnn"

TARGET_CLI_AVAILABLE=0
TARGET_CLI_VERSION="not included"
if [[ -f "$TMP_DIR/fnn-cli" ]]; then
  TARGET_CLI_AVAILABLE=1
  chmod 0755 "$TMP_DIR/fnn-cli"
  TARGET_CLI_VERSION="$("$TMP_DIR/fnn-cli" --version | head -n 1)"
elif [[ "$FNN_SOURCE" == "release" ]]; then
  printf 'release package %s has no fnn-cli binary\n' "$ASSET_NAME" >&2
  exit 1
fi

TARGET_FNN_VERSION="$("$TMP_DIR/fnn" --version | head -n 1)"
write_github_output fnn_version "$TARGET_FNN_VERSION"
NODE1_FNN_VERSION="$("$NODE1_DIR/fnn" --version | head -n 1)"
NODE2_FNN_VERSION="$("$NODE2_DIR/fnn" --version | head -n 1)"
NODE1_CLI_VERSION="missing"
if [[ -x "$NODE1_DIR/fnn-cli" ]]; then
  NODE1_CLI_VERSION="$("$NODE1_DIR/fnn-cli" --version | head -n 1)"
fi

log "target package : $TARGET_LABEL"
log "target fnn     : $TARGET_FNN_VERSION"
log "node1 fnn      : $NODE1_FNN_VERSION"
log "node2 fnn      : $NODE2_FNN_VERSION"
log "target fnn-cli : $TARGET_CLI_VERSION"
log "node1 fnn-cli  : $NODE1_CLI_VERSION"

FNN_NEEDS_UPDATE=0
CLI_NEEDS_UPDATE=0
if [[ "$NODE1_FNN_VERSION" != "$TARGET_FNN_VERSION" \
  || "$NODE2_FNN_VERSION" != "$TARGET_FNN_VERSION" ]]; then
  FNN_NEEDS_UPDATE=1
fi
if [[ "$TARGET_CLI_AVAILABLE" == "1" \
  && "$NODE1_CLI_VERSION" != "$TARGET_CLI_VERSION" ]]; then
  CLI_NEEDS_UPDATE=1
fi

BACKUP_TAG="${TARGET_TAG//\//_}"

if [[ "$FNN_NEEDS_UPDATE" == "1" ]]; then
  log "stopping Fiber services before validation and replacement"
  SERVICES_STOPPED=1
  sudo -n systemctl stop "$SERVICE1" "$SERVICE2"

  log "validating node1 and node2 stores in parallel with $TARGET_TAG"
  VALIDATION_STARTED_AT="$(date +%s)"
  "$TMP_DIR/fnn" \
    --config "$NODE1_DIR/config.yml" \
    --dir "$NODE1_DIR" \
    --check-validate >"$TMP_DIR/node1-validation.log" 2>&1 &
  NODE1_VALIDATION_PID=$!
  "$TMP_DIR/fnn" \
    --config "$NODE2_DIR/config.yml" \
    --dir "$NODE2_DIR" \
    --check-validate >"$TMP_DIR/node2-validation.log" 2>&1 &
  NODE2_VALIDATION_PID=$!

  if wait "$NODE1_VALIDATION_PID"; then
    NODE1_VALIDATION_STATUS=0
  else
    NODE1_VALIDATION_STATUS=$?
  fi
  if wait "$NODE2_VALIDATION_PID"; then
    NODE2_VALIDATION_STATUS=0
  else
    NODE2_VALIDATION_STATUS=$?
  fi

  cat "$TMP_DIR/node1-validation.log"
  cat "$TMP_DIR/node2-validation.log"
  VALIDATION_ELAPSED="$(( $(date +%s) - VALIDATION_STARTED_AT ))"

  if [[ "$NODE1_VALIDATION_STATUS" != "0" \
    || "$NODE2_VALIDATION_STATUS" != "0" ]]; then
    printf '%s\n' \
      "database validation failed (node1=$NODE1_VALIDATION_STATUS, node2=$NODE2_VALIDATION_STATUS)" \
      "The new binary may require a migration. No binary was replaced." \
      "Back up the node data and perform the documented migration manually." >&2
    exit 1
  fi
  log "both node stores validated in ${VALIDATION_ELAPSED}s"

  BACKUP_DIR="$BACKUP_ROOT/$(date -u '+%Y%m%dT%H%M%SZ')-$BACKUP_TAG"
  sudo -n install -d -m 0755 "$BACKUP_DIR"
  sudo -n cp -a "$NODE1_DIR/fnn" "$BACKUP_DIR/node1-fnn"
  sudo -n cp -a "$NODE2_DIR/fnn" "$BACKUP_DIR/node2-fnn"
  if [[ "$TARGET_CLI_AVAILABLE" == "1" && -e "$NODE1_DIR/fnn-cli" ]]; then
    sudo -n cp -a "$NODE1_DIR/fnn-cli" "$BACKUP_DIR/node1-fnn-cli"
  fi

  BINARIES_REPLACED=1
  sudo -n install -m 0755 "$TMP_DIR/fnn" "$NODE1_DIR/fnn"
  sudo -n install -m 0755 "$TMP_DIR/fnn" "$NODE2_DIR/fnn"
  if [[ "$TARGET_CLI_AVAILABLE" == "1" ]]; then
    CLI_REPLACED=1
    sudo -n install -m 0755 "$TMP_DIR/fnn-cli" "$NODE1_DIR/fnn-cli"
  fi

  log "installed $TARGET_LABEL; previous binaries saved in $BACKUP_DIR"
  start_services
elif [[ "$CLI_NEEDS_UPDATE" == "1" ]]; then
  BACKUP_DIR="$BACKUP_ROOT/$(date -u '+%Y%m%dT%H%M%SZ')-$BACKUP_TAG"
  sudo -n install -d -m 0755 "$BACKUP_DIR"
  if [[ -e "$NODE1_DIR/fnn-cli" ]]; then
    sudo -n cp -a "$NODE1_DIR/fnn-cli" "$BACKUP_DIR/node1-fnn-cli"
  fi

  CLI_REPLACED=1
  sudo -n install -m 0755 "$TMP_DIR/fnn-cli" "$NODE1_DIR/fnn-cli"
  log "updated fnn-cli without restarting Fiber; previous CLI saved in $BACKUP_DIR"
else
  log "Fiber binaries are already current; restart is not needed"
fi

[[ -n "$AUTH_TOKEN_FILE" ]] || {
  printf 'CCH_SMOKE_FNN_AUTH_TOKEN or CCH_SMOKE_FNN_AUTH_TOKEN_FILE is required for RPC health checks\n' >&2
  exit 1
}
[[ -r "$AUTH_TOKEN_FILE" ]] || {
  printf 'FNN auth token file is not readable: %s\n' "$AUTH_TOKEN_FILE" >&2
  exit 1
}

FNN_CLI="$NODE1_DIR/fnn-cli"
wait_for_rpc() {
  local node_name="$1"
  local rpc_url="$2"
  local output
  local attempt
  local error_file="$TMP_DIR/$node_name-rpc-error.log"

  for attempt in $(seq 1 90); do
    if output="$(
      "$FNN_CLI" \
        -u "$rpc_url" \
        -o json \
        --no-banner \
        --auth-token-file "$AUTH_TOKEN_FILE" \
        info 2>"$error_file"
    )"; then
      jq --arg node "$node_name" \
        '{node: $node, version, commit_hash, pubkey}' <<<"$output"
      return 0
    fi
    sleep 2
  done

  printf 'RPC health check failed for %s (%s)\n' "$node_name" "$rpc_url" >&2
  cat "$error_file" >&2
  return 1
}

log "installed binary versions"
"$NODE1_DIR/fnn" --version
"$NODE2_DIR/fnn" --version
"$NODE1_DIR/fnn-cli" --version

log "waiting for Fiber RPC health"
wait_for_rpc node1 "$F1_RPC"
wait_for_rpc node2 "$F2_RPC"
log "both Fiber nodes are healthy; smoke test may proceed"
