#!/usr/bin/env bash

# Daily smoke checks installed nodes; upgrades are separate manual operations.
set -Eeuo pipefail

EXPECTED_TAG="${CCH_SMOKE_FNN_EXPECTED_TAG:?set the tested FNN release tag}"
EXPECTED_COMMIT="${CCH_SMOKE_FNN_EXPECTED_COMMIT:?set the tested FNN commit}"
NODE1_DIR="${CCH_SMOKE_NODE1_DIR:-/home/ckb/fiber-test/testnet/node1}"
NODE2_DIR="${CCH_SMOKE_NODE2_DIR:-/home/ckb/fiber-test/testnet/node2}"
F1_RPC="${CCH_SMOKE_F1_RPC:-http://127.0.0.1:8227}"
F2_RPC="${CCH_SMOKE_F2_RPC:-http://127.0.0.1:8229}"
AUTH_TOKEN_FILE="${CCH_SMOKE_FNN_AUTH_TOKEN_FILE:?set the FNN auth token file}"
NODE_VERSIONS='{}'
RPC_VERSIONS='{}'
CLI_VERSION='unavailable'
PROBLEMS='[]'
VERIFY_STATUS='unavailable'

write_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"
  fi
}

record_problem() {
  PROBLEMS="$(jq -c --arg component "$1" --arg kind "$2" \
    --arg expected "$3" --arg actual "$4" \
    '. + [{component:$component,kind:$kind,expected:$expected,actual:$actual}]' <<<"$PROBLEMS")"
}

emit_verification() {
  local diagnostic
  diagnostic="$(jq -cn --arg status "$VERIFY_STATUS" \
    --arg expected_tag "$EXPECTED_TAG" --arg expected_commit "$EXPECTED_COMMIT" \
    --argjson nodes "$NODE_VERSIONS" --arg cli_version "$CLI_VERSION" \
    --argjson running "$RPC_VERSIONS" --argjson problems "$PROBLEMS" \
    '{status:$status,expected_tag:$expected_tag,expected_commit:$expected_commit,
      nodes:$nodes,cli_version:$cli_version,running:$running,problems:$problems}')"
  write_output fnn_check "$diagnostic"
  if [[ "$VERIFY_STATUS" == verified ]]; then
    write_output fnn_package "installed release $EXPECTED_TAG"
  else
    write_output fnn_package "manual installation; verification $VERIFY_STATUS"
  fi
}
trap emit_verification EXIT

# Inventory both binaries and the shared CLI before failing so the report
# retains the complete mismatch, even when node1 is the first failure.
for node in node1 node2; do
  node_dir="$NODE1_DIR"
  [[ "$node" != node2 ]] || node_dir="$NODE2_DIR"
  version='unavailable'
  if [[ -x "$node_dir/fnn" ]] && version="$("$node_dir/fnn" --version | head -n 1)"; then
    if [[ "$version" != "fnn Fiber $EXPECTED_TAG ($EXPECTED_COMMIT "* ]]; then
      record_problem "$node binary" version-mismatch "$EXPECTED_TAG ($EXPECTED_COMMIT)" "$version"
    fi
  else
    record_problem "$node binary" unavailable "$EXPECTED_TAG ($EXPECTED_COMMIT)" 'binary missing or version unreadable'
  fi
  NODE_VERSIONS="$(jq -c --arg node "$node" --arg version "$version" \
    '. + {($node):$version}' <<<"$NODE_VERSIONS")"
  [[ "$node" != node1 ]] || write_output fnn_version "$version"
  printf '%s: %s\n' "$node" "$version"
done

FNN_CLI="$NODE1_DIR/fnn-cli"
if [[ -x "$FNN_CLI" ]] && CLI_VERSION="$("$FNN_CLI" --version | head -n 1)"; then
  if [[ "$CLI_VERSION" != "fnn-cli ${EXPECTED_TAG#v}" ]]; then
    record_problem CLI version-mismatch "fnn-cli ${EXPECTED_TAG#v}" "$CLI_VERSION"
  fi
else
  record_problem CLI unavailable "fnn-cli ${EXPECTED_TAG#v}" 'CLI missing or version unreadable'
fi
printf 'CLI: %s\n' "$CLI_VERSION"

fail_if_problems() {
  if [[ "$(jq length <<<"$PROBLEMS")" != 0 ]]; then
    if jq -e 'any(.[]; .kind == "version-mismatch")' <<<"$PROBLEMS" >/dev/null; then
      VERIFY_STATUS=version-mismatch
    fi
    jq -r '.[] | "Fiber preflight: \(.component): expected \(.expected); actual \(.actual)"' <<<"$PROBLEMS" >&2
    printf 'Manually align both nodes, the CLI and the pinned CI version before rerunning smoke.\n' >&2
    exit 1
  fi
}
fail_if_problems

if [[ ! -r "$AUTH_TOKEN_FILE" ]]; then
  record_problem 'FNN auth' unavailable 'readable credential file' 'credential file unreadable'
  fail_if_problems
fi

verify_rpc() {
  local node="$1" url="$2" output attempt running
  for attempt in 1 2 3 4 5; do
    if output="$(timeout 10 "$FNN_CLI" -u "$url" -o json --no-banner \
      --auth-token-file "$AUTH_TOKEN_FILE" info 2>/dev/null)"; then
      running="$(jq -r '"\(.version // "unknown") (\(.commit_hash // "unknown"))"' <<<"$output")"
      RPC_VERSIONS="$(jq -c --arg node "$node" --arg version "$running" \
        '. + {($node):$version}' <<<"$RPC_VERSIONS")"
      if jq -e --arg tag "${EXPECTED_TAG#v}" --arg commit "$EXPECTED_COMMIT" \
        '.version == $tag and ((.commit_hash // "") | startswith($commit))' \
        <<<"$output" >/dev/null; then
        jq --arg node "$node" '{node: $node, version, commit_hash, pubkey}' <<<"$output"
      else
        record_problem "$node RPC" version-mismatch "$EXPECTED_TAG ($EXPECTED_COMMIT)" "$running"
      fi
      if ! jq -e '((.pubkey // "") | length > 0)' <<<"$output" >/dev/null; then
        record_problem "$node RPC" unavailable 'a node pubkey' 'node identity missing'
      fi
      return 0
    fi
    sleep 2
  done
  record_problem "$node RPC" unavailable 'responsive Fiber RPC' "$url unavailable"
}

verify_rpc node1 "$F1_RPC"
verify_rpc node2 "$F2_RPC"
fail_if_problems
VERIFY_STATUS=verified
printf 'Both installed Fiber nodes match %s (%s) and respond to RPC.\n' \
  "$EXPECTED_TAG" "$EXPECTED_COMMIT"
