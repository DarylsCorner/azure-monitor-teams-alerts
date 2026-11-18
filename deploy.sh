#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

usage() {
  cat <<'HELP'
Usage: bash deploy.sh -g RESOURCE_GROUP [options]
  -l, --location REGION       Required for deployment (example: westus2)
  -s, --subscription ID       Defaults to the current Azure CLI subscription
  -p, --prefix NAME           Default: alerts-teams
      --severities CSV        Default: Sev0,Sev1,Sev2
      --no-resolved           Do not forward resolved alerts
      --apr-scope ID          Opt-in routing for a subscription/resource group
      --check                 Check inputs and generate payloads locally; no Azure calls
      --test                  Deploy, then send a synthetic Fired alert
      --test-only             Send a test to an existing deployment
  -h, --help                  Show help

Supply TEAMS_WORKFLOW_URL through the environment or a hidden interactive prompt.
Use a Teams Workflows webhook with authentication set to "Anyone".
Requires Azure CLI, Python 3 (JSON serialization), and curl for tests.
No Bicep, ARM deployment templates, azd, or PowerShell scripts are used.
HELP
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
require_value() {
  [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "$1 requires a value"
}
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s\n' "$1"; fi
}
rest() {
  # Avoid Git Bash rewriting ARM resource IDs as Windows paths.
  MSYS_NO_PATHCONV=1 az rest --subscription "$subscription" --only-show-errors "$@"
}
put_resource() {
  rest --method put --url "$1" --body "@$(native_path "$2")" -o none
}

group='' location='' subscription='' prefix='alerts-teams'
severities='Sev0,Sev1,Sev2' send_resolved=true apr_scope=''
check=false test=false test_only=false
while (($#)); do
  case "$1" in
    -g|--resource-group) require_value "$@"; group=$2; shift 2 ;;
    -l|--location) require_value "$@"; location=$2; shift 2 ;;
    -s|--subscription) require_value "$@"; subscription=$2; shift 2 ;;
    -p|--prefix) require_value "$@"; prefix=$2; shift 2 ;;
    --severities) require_value "$@"; severities=$2; shift 2 ;;
    --apr-scope) require_value "$@"; apr_scope=$2; shift 2 ;;
    --no-resolved) send_resolved=false; shift ;;
    --check) check=true; shift ;;
    --test) test=true; shift ;;
    --test-only) test_only=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$group" ]] || die "--resource-group is required"
[[ "$group" =~ ^[a-zA-Z0-9._()-]+$ && ${#group} -le 90 && "$group" != *. ]] || die "Invalid resource group name"
[[ "$prefix" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,59}$ ]] || die "Prefix must be 1-60 letters, digits, or hyphens, starting with a letter or digit"
[[ "$severities" =~ ^Sev[0-4](,Sev[0-4])*$ ]] || die "Severities must be comma-separated Sev0 through Sev4"
[[ "$check" != true || ( "$test" != true && "$test_only" != true ) ]] || die "--check cannot be combined with testing"
[[ "$test" != true || "$test_only" != true ]] || die "Choose --test or --test-only"
[[ "$test_only" == true || -n "$location" ]] || die "--location is required"
[[ -z "$location" || "$location" =~ ^[a-z0-9]+$ ]] || die "Use a region name such as westus2"
[[ -z "$apr_scope" || "$apr_scope" =~ ^/subscriptions/[a-fA-F0-9-]{36}(/resourceGroups/[a-zA-Z0-9._()-]+)?$ ]] || die "APR scope must be a subscription or resource group resource ID"

python=''
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info.major != 3)' >/dev/null 2>&1; then
    python=$candidate
    break
  fi
done
[[ -n "$python" ]] || die "Python 3 is required"
if [[ "$check" != true ]]; then
  command -v az >/dev/null 2>&1 || die "Azure CLI is required"
fi
if [[ "$test" == true || "$test_only" == true ]]; then
  command -v curl >/dev/null 2>&1 || die "curl is required for testing"
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT

if [[ -z "$subscription" ]]; then
  if [[ "$check" == true ]]; then
    die "--subscription is required for offline --check"
  fi
  subscription=$(az account show --query id -o tsv --only-show-errors)
fi
[[ "$subscription" =~ ^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$ ]] || die "Provide a valid subscription UUID or run az login"
if [[ -n "$apr_scope" ]]; then
  scope_subscription=${apr_scope#*/subscriptions/}
  scope_subscription=${scope_subscription%%/*}
  [[ "${scope_subscription,,}" == "${subscription,,}" ]] || die "APR scope must be in the deployment subscription"
fi
workflow_id="/subscriptions/$subscription/resourceGroups/$group/providers/Microsoft.Logic/workflows/${prefix}-la"
action_group_id="/subscriptions/$subscription/resourceGroups/$group/providers/Microsoft.Insights/actionGroups/${prefix}-ag"
management='https://management.azure.com'
payload() {
  local mode=$1 destination=$2
  shift 2
  if [[ "$mode" == action-group ]]; then
    local callback_file
    callback_file=$(native_path "$1")
    shift
    set -- "$callback_file" "$@"
  fi
  MSYS_NO_PATHCONV=1 "$python" "$(native_path "$script_dir/scripts/payloads.py")" \
    "$mode" "$(native_path "$destination")" "$@"
}

if [[ "$test_only" != true ]]; then
  if [[ -z "${TEAMS_WORKFLOW_URL:-}" ]]; then
    [[ -t 0 ]] || die "Set TEAMS_WORKFLOW_URL for noninteractive deployment"
    read -r -s -p 'Teams Workflows webhook URL: ' TEAMS_WORKFLOW_URL
    printf '\n' >&2
  fi
  export TEAMS_WORKFLOW_URL
  payload workflow "$temp_dir/workflow.json" "$location" "$severities" "$send_resolved"
  unset TEAMS_WORKFLOW_URL
  if [[ -n "$apr_scope" ]]; then
    payload apr "$temp_dir/apr.json" "$apr_scope" "$action_group_id"
  fi
  if [[ "$check" == true ]]; then
    printf 'Local input and payload checks passed. No Azure resources changed.\n'
    exit 0
  fi
  az group create --name "$group" --location "$location" --subscription "$subscription" --only-show-errors -o none
  put_resource "$management$workflow_id?api-version=2019-05-01" "$temp_dir/workflow.json"
fi

# These are credentials: keep them in private temporary files, never command arguments.
rest --method post --url "$management$workflow_id/triggers/manual/listCallbackUrl?api-version=2016-06-01" \
  --query value -o json >"$temp_dir/callback.json"
if [[ "$test_only" != true ]]; then
  payload action-group "$temp_dir/action-group.json" "$temp_dir/callback.json" "$workflow_id" "$prefix"
  put_resource "$management$action_group_id?api-version=2023-01-01" "$temp_dir/action-group.json"
  if [[ -n "$apr_scope" ]]; then
    apr_id="/subscriptions/$subscription/resourceGroups/$group/providers/Microsoft.AlertsManagement/actionRules/${prefix}-apr"
    put_resource "$management$apr_id?api-version=2021-08-08" "$temp_dir/apr.json"
  fi
  printf 'Deployed %s-la and %s-ag.\n' "$prefix" "$prefix"
  printf 'Attach action group %s-ag to alert rules unless using the opt-in APR.\n' "$prefix"
fi

if [[ "$test" == true || "$test_only" == true ]]; then
  payload test "$temp_dir" "$workflow_id" "${severities%%,*}"
  # Do not retry POSTs here: retries can create duplicate cards.
  if ! curl --config "$temp_dir/curl.conf" --silent --fail \
    --connect-timeout 15 --max-time 120 --request POST \
    --header 'Content-Type: application/json' --data-binary "@$temp_dir/sample.json" \
    --output "$temp_dir/response.json"; then
    die "Test request failed or timed out; inspect Logic App run history"
  fi
  payload verify "$temp_dir/response.json"
fi
