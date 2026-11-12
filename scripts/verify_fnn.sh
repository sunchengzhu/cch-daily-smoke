#!/usr/bin/env bash

# Daily smoke checks the installed nodes. FNN installation is a separate,
# operator-triggered operation and must never happen in this workflow.
set -Eeuo pipefail

EXPECTED_TAG="${CCH_SMOKE_FNN_EXPECTED_TAG:?set the tested FNN release tag}"
EXPECTED_COMMIT="${CCH_SMOKE_FNN_EXPECTED_COMMIT:?set the tested FNN commit}"
NODE1_DIR="${CCH_SMOKE_NODE1_DIR:-/home/ckb/fiber-test/testnet/node1}"
NODE2_DIR="${CCH_SMOKE_NODE2_DIR:-/home/ckb/fiber-test/testnet/node2}"
F1_RPC="${CCH_SMOKE_F1_RPC:-http://127.0.0.1:8227}"
F2_RPC="${CCH_SMOKE_F2_RPC:-http://127.0.0.1:8229}"
AUTH_TOKEN_FILE="${CCH_SMOKE_FNN_AUTH_TOKEN_FILE:?set the FNN auth token file}"

write_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"
  fi
}

write_output fnn_package "installed release $EXPECTED_TAG"

for node_dir in "$NODE1_DIR" "$NODE2_DIR"; do
  [[ -x "$node_dir/fnn" ]] || {
    printf 'FNN binary is missing or not executable: %s/fnn\n' "$node_dir" >&2
    exit 1
  }
  version="$("$node_dir/fnn" --version | head -n 1)"
  [[ "$node_dir" != "$NODE1_DIR" ]] || write_output fnn_version "$version"
  printf '%s: %s\n' "$node_dir" "$version"
  if [[ "$version" != "fnn Fiber $EXPECTED_TAG ($EXPECTED_COMMIT "* ]]; then
    printf 'Installed FNN differs from tested %s (%s); upgrade it manually before running smoke.\n' \
      "$EXPECTED_TAG" "$EXPECTED_COMMIT" >&2
    exit 1
  fi
done

FNN_CLI="$NODE1_DIR/fnn-cli"
[[ -x "$FNN_CLI" ]] || {
  printf 'FNN CLI is missing or not executable: %s\n' "$FNN_CLI" >&2
  exit 1
}
cli_version="$("$FNN_CLI" --version | head -n 1)"
if [[ "$cli_version" != "fnn-cli ${EXPECTED_TAG#v}" ]]; then
  printf 'Installed FNN CLI differs from tested %s: %s\n' "$EXPECTED_TAG" "$cli_version" >&2
  exit 1
fi
[[ -r "$AUTH_TOKEN_FILE" ]] || {
  printf 'FNN auth token file is not readable: %s\n' "$AUTH_TOKEN_FILE" >&2
  exit 1
}

verify_rpc() {
  local node="$1" url="$2" output attempt
  for attempt in 1 2 3 4 5; do
    if output="$(timeout 10 "$FNN_CLI" -u "$url" -o json --no-banner \
      --auth-token-file "$AUTH_TOKEN_FILE" info 2>/dev/null)"; then
      if jq -e --arg tag "${EXPECTED_TAG#v}" --arg commit "$EXPECTED_COMMIT" \
        '.version == $tag and ((.commit_hash // "") | startswith($commit)) and ((.pubkey // "") | length > 0)' \
        <<<"$output" >/dev/null; then
        jq --arg node "$node" '{node: $node, version, commit_hash, pubkey}' <<<"$output"
        return 0
      fi
      printf '%s RPC returned an unexpected FNN version or identity\n' "$node" >&2
      return 1
    fi
    sleep 2
  done
  printf '%s RPC is unavailable: %s\n' "$node" "$url" >&2
  return 1
}

verify_rpc node1 "$F1_RPC"
verify_rpc node2 "$F2_RPC"
printf 'Both installed Fiber nodes match %s (%s) and respond to RPC.\n' \
  "$EXPECTED_TAG" "$EXPECTED_COMMIT"
