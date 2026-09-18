# Developer entry points. `just parity` is the one-shot local parity check.

set shell := ["bash", "-euo", "pipefail", "-c"]

port := "3999"
captures_repo := "https://github.com/riftresearch/hypercore-simulator-captures"
bin := "target/release/hypercore-simulator"
protocol_meta := "captures/testnet/protocol-parity/info-valid-spot-meta/response.body"

# Mismatch counts the current build is expected to reproduce. golden_extended's
# default selection runs without the recorded market-context files, so its
# price-dependent comparisons cannot be reproduced. Anything higher is a regression.
expected_readonly_mismatches := "0"
expected_extended_mismatches := "94"

default:
    @just --list

# Build the release binary.
build:
    cargo build --release --locked

# Rust unit tests and lints, with and without the command-line feature.
test:
    cargo clippy --locked --all-targets -- -D warnings
    cargo clippy --locked --no-default-features -- -D warnings
    cargo test --locked

# Fetch the parity evidence into ./captures (gitignored) unless it is already there.
captures:
    test -d captures/testnet || git clone --depth 1 {{captures_repo}} captures

# Run the simulator with mainnet defaults (pass extra flags, e.g. `just run --network testnet`).
run *args: build
    {{bin}} {{args}}

# Build, boot a testnet-mode simulator, replay every parity suite against it, and report.
parity: build captures
    #!/usr/bin/env bash
    set -uo pipefail
    cd "{{justfile_directory()}}"
    uv sync --locked
    base="http://127.0.0.1:{{port}}"
    root=$(mktemp -d /tmp/hypercore-parity-XXXXXX)
    pid=""

    stop() {
        if [ -n "$pid" ]; then kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; pid=""; fi
    }
    trap stop EXIT

    boot() {
        stop
        {{bin}} --network testnet --bind "127.0.0.1:{{port}}" "$@" > "$root/server.log" 2>&1 &
        pid=$!
        for _ in $(seq 100); do
            if curl -sf -o /dev/null -X POST -H 'content-type: application/json' \
                -d '{"type":"spotMeta"}' "$base/info"; then return; fi
            sleep 0.1
        done
        echo "simulator did not start:"; cat "$root/server.log"; exit 1
    }

    declare -A code
    suite() {
        local name=$1; shift
        echo; echo "=== $name ==="
        "$@" --base-url "$base" --root "$root/$name" > "$root/$name.stdout" 2> "$root/$name.stderr"
        code[$name]=$?
        python3 - "$root/$name.stdout" <<'EOF'
    import json, sys
    text = open(sys.argv[1]).read()
    start = text.find("{")
    try:
        report = json.loads(text[start:])
    except ValueError:
        print(text[-600:]); sys.exit()
    keys = ("checks", "assertions_passed", "requests_recorded", "counts", "mismatched_cases", "coverage_gaps")
    print(json.dumps({k: report[k] for k in keys if k in report}))
    EOF
        echo "exit ${code[$name]}"
    }

    mismatches() {   # report file -> counts.mismatch
        jq -r '.counts.mismatch // 0' "$1" 2>/dev/null || echo "?"
    }

    boot
    suite golden    uv run python -m probes.golden
    suite verify    uv run python -m probes.verify
    suite readonly  uv run python -m probes.differential --source captures/testnet/readonly-contract
    suite shapes    uv run python -m probes.differential --source captures/testnet/unsigned-shapes
    suite extended  uv run python -m probes.golden_extended
    boot --meta "{{protocol_meta}}"
    suite protocol  uv run python -m probes.differential --source captures/testnet/protocol-parity
    stop

    echo; echo "=== verdict (evidence in $root) ==="
    failed=0
    verdict() {   # name, ok(0/1), detail
        if [ "$2" = 0 ]; then printf '  %-10s ok    %s\n' "$1" "$3"; else printf '  %-10s FAIL  %s\n' "$1" "$3"; failed=1; fi
    }
    verdict golden   $([ "${code[golden]}" = 0 ] && echo 0 || echo 1)  "exit ${code[golden]}"
    verdict verify   $([ "${code[verify]}" = 0 ] && echo 0 || echo 1)  "exit ${code[verify]}"
    m=$(mismatches "$root/readonly/comparison.json");  verdict readonly $([ "$m" = "{{expected_readonly_mismatches}}" ] && echo 0 || echo 1) "$m mismatches (expected {{expected_readonly_mismatches}})"
    m=$(mismatches "$root/shapes/comparison.json");    verdict shapes   $([ "$m" = 0 ] && echo 0 || echo 1) "$m mismatches"
    m=$(mismatches "$root/protocol/comparison.json");  verdict protocol $([ "$m" = 0 ] && echo 0 || echo 1) "$m mismatches"
    m=$(mismatches "$root/extended/golden-extended.json"); verdict extended $([ "$m" = "{{expected_extended_mismatches}}" ] && echo 0 || echo 1) "$m mismatches (expected {{expected_extended_mismatches}}, all missing-prerequisite cases)"
    exit $failed
