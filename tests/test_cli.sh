#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
trap 'status=$?; [[ ! -f "$work/output.txt" ]] || cat "$work/output.txt" >&2; exit "$status"' ERR
export MOCK_LOG="$work/commands.log" MOCK_WORK="$work"
if command -v cygpath >/dev/null 2>&1; then
  MOCK_WORK=$(cygpath -w "$work")
fi
export MOCK_PYTHON=''
for candidate in python3 python; do
  if "$candidate" -c 'import sys; sys.exit(sys.version_info.major != 3)' >/dev/null 2>&1; then
    MOCK_PYTHON=$candidate; break
  fi
done
[[ -n "$MOCK_PYTHON" ]] || { echo 'Python 3 required' >&2; exit 1; }
subscription='00000000-0000-0000-0000-000000000000'
export TEAMS_WORKFLOW_URL='https://example.environment.api.powerplatform.com/workflows/fake?sig=TEST_ONLY'

az() {
  printf 'az %s\n' "$*" >>"$MOCK_LOG"
  if [[ "${MOCK_FAIL:-}" == az ]]; then return 19; fi
  if [[ "$1 $2" == 'account show' ]]; then
    printf '%s\n' '00000000-0000-0000-0000-000000000000'
  elif [[ "$1" == rest ]]; then
    local method='' url='' body=''
    while (($#)); do
      case "$1" in
        --method) method=$2; shift 2 ;;
        --url) url=$2; shift 2 ;;
        --body) body=${2#@}; shift 2 ;;
        *) shift ;;
      esac
    done
    if [[ "$method" == post && "$url" == *'/triggers/When_an_HTTP_request_is_received/listCallbackUrl?'* ]]; then
      printf '"https://example.logic.azure.com/workflows/fake/triggers/When_an_HTTP_request_is_received/invoke?sig=TEST_ONLY"\n'
    elif [[ "$method" == put && -n "$body" ]]; then
      "$MOCK_PYTHON" - "$body" "$url" "$MOCK_WORK" <<'PY'
import json
from pathlib import Path
import sys
body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
url = sys.argv[2]
if "/Microsoft.Logic/workflows/" in url:
    assert body["properties"]["parameters"]["teamsWorkflowUrl"]["value"].endswith("sig=TEST_ONLY")
    assert body["properties"]["definition"]["parameters"]["teamsWorkflowUrl"]["type"] == "SecureString"
elif "/Microsoft.Insights/actionGroups/" in url:
    receiver, = body["properties"]["logicAppReceivers"]
    assert receiver["useCommonAlertSchema"] is True
    assert receiver["callbackUrl"].endswith("sig=TEST_ONLY")
    assert receiver["resourceId"].startswith("/subscriptions/")
elif "/Microsoft.AlertsManagement/actionRules/" in url:
    assert body["properties"]["actions"][0]["actionType"] == "AddActionGroups"
    assert body["properties"]["scopes"][0].startswith("/subscriptions/")
else:
    raise AssertionError("Unexpected resource URL")
(Path(sys.argv[3]) / "last-body-path.txt").write_text(sys.argv[1], encoding="utf-8")
PY
    else
      echo 'Unexpected mock Azure CLI request' >&2; return 20
    fi
  elif [[ "$1 $2" != 'group create' ]]; then
    echo 'Unexpected mock Azure CLI command' >&2; return 21
  fi
}
curl() {
  printf 'curl %s\n' "$*" >>"$MOCK_LOG"
  if [[ "${MOCK_FAIL:-}" == curl ]]; then return 22; fi
  local output='' config='' data=''
  while (($#)); do
    case "$1" in
      --output) output=$2; shift 2 ;;
      --config) config=$2; shift 2 ;;
      --data-binary) data=${2#@}; shift 2 ;;
      *) shift ;;
    esac
  done
  "$MOCK_PYTHON" - "$config" "$data" "$output" "${MOCK_RESPONSE:-accepted-by-teams-workflow}" <<'PY'
import json
from pathlib import Path
import sys
assert json.loads(Path(sys.argv[1]).read_text().removeprefix("url = ")).endswith("sig=TEST_ONLY")
assert json.loads(Path(sys.argv[2]).read_text())["data"]["essentials"]["monitorCondition"] == "Fired"
Path(sys.argv[3]).write_text(json.dumps({"status": sys.argv[4]}), encoding="utf-8")
PY
}
export -f az curl

run() { bash deploy.sh "$@" >"$work/output.txt" 2>&1; }
reject() {
  if run "$@"; then echo "Unexpected success: $*" >&2; exit 1; fi
}

bash -n deploy.sh
run --help
reject
reject -g
reject -g rg-test -l westus2 --severities Sev5
reject -g rg-test -l westus2 --prefix 'invalid/name'
reject -g rg-test -l westus2 --check --test
reject -g rg-test --test --test-only
reject -g rg-test -l westus2 --unknown
reject -g rg-test -l westus2 -s not-a-uuid --check
reject -g rg-test -l westus2 -s "$subscription" --apr-scope '/subscriptions/11111111-1111-1111-1111-111111111111' --check
TEAMS_WORKFLOW_URL='https://evil.invalid/?sig=TEST_ONLY' reject -g rg-test -l westus2 -s "$subscription" --check
run -g rg-test -l westus2 -s "$subscription" --check
[[ ! -e "$MOCK_LOG" ]] || { echo 'Offline check called Azure' >&2; exit 1; }

run -g rg-test -l westus2 --test --no-resolved --severities Sev1,Sev2 \
  --apr-scope "/subscriptions/$subscription/resourceGroups/rg-workloads"
grep -q 'not proof of channel delivery' "$work/output.txt"
[[ $(grep -c -- '--method put' "$MOCK_LOG") == 3 ]]
grep -q 'actionRules/alerts-teams-apr' "$MOCK_LOG"
grep -q '/triggers/When_an_HTTP_request_is_received/listCallbackUrl' "$MOCK_LOG"
! grep -q 'TEST_ONLY' "$MOCK_LOG"
! grep -q 'TEST_ONLY' "$work/output.txt"
last_body=$("$MOCK_PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())' "$work/last-body-path.txt")
[[ ! -e "$last_body" ]] || { echo 'Secret payload file not cleaned up' >&2; exit 1; }

: >"$MOCK_LOG"
TEAMS_WORKFLOW_URL='' run -g rg-test -s "$subscription" --test-only
! grep -q -- '--method put' "$MOCK_LOG"
! grep -q 'group create' "$MOCK_LOG"
MOCK_RESPONSE=filtered reject -g rg-test -s "$subscription" --test-only
MOCK_FAIL=curl reject -g rg-test -s "$subscription" --test-only
MOCK_FAIL=az reject -g rg-test -l westus2 -s "$subscription"

: >"$MOCK_LOG"
run -g rg-test -l westus2 -s "$subscription"
[[ $(grep -c -- '--method put' "$MOCK_LOG") == 2 ]]
! grep -q 'actionRules/' "$MOCK_LOG"
echo 'Bash CLI tests passed (mocked Azure CLI and curl; no cloud changes).'
