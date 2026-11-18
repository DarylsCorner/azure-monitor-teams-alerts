# Azure Monitor alerts to Teams

Deployment scripts that provision a Consumption Logic App and an Azure Monitor action group
using **Azure CLI directly** (`az group create`, `az rest`).

```
Azure Monitor -> action group -> Logic App -> Teams Workflows webhook -> channel
```

Default behavior: forward Sev0-Sev2, include resolved alerts, display an Adaptive
Card with resource IDs and UTC times, and link to the first resource in Azure
Portal. Failed outbound HTTP calls use exponential backoff (four retries for
retryable failures). This is at-least-once delivery: duplicate cards are possible.

## One-time prerequisites

- Bash 4+ (Git Bash on Windows, WSL, Linux, or an updated Bash on macOS).
- Azure CLI, Python 3 (`python3` or `python`, standard library only), and curl for tests.
- `az login`, an Azure subscription, and permission to create resource groups,
  Logic Apps, action groups, and optionally alert processing rules. Contributor
  at the required scope is sufficient but broader than a custom least-privilege
  role. The script does not create identities or grant permissions.
- Register `Microsoft.Logic` and `Microsoft.Insights` with your subscription
  administrator if not already registered; also `Microsoft.AlertsManagement`
  when using an alert processing rule.
- Commercial/public Azure cloud; sovereign clouds are not supported.

### Set up the Teams destination

1. Choose a test Team and a Standard channel that represents the intended customer
   destination.
2. Open the channel's **Workflows** app, then **Create workflows** to open the full
   template catalog. Searching only suggested workflows can return no results.
3. Search for **webhook** and select **Send webhook alerts to a channel**. Template
   names can vary by tenant and Teams version.
4. Sign in to the Teams connection, select the team/channel, and configure
   authentication as **Anyone** if prompted. The workflow must accept the standard
   `type: message` / `attachments` Adaptive Card envelope.
5. Save the workflow and copy its signed webhook URL privately. Supply it using the
   hidden prompt or secret environment variable shown below; do not paste it
   into chat, screenshots, issues, or source control.

This is the simplest current webhook integration, but it is **not zero-setup**.
The workflow and Teams connection have user ownership. Assign an appropriate
co-owner and follow your tenant's account/lifecycle policies. An "Anyone" URL is a
bearer credential: anyone possessing it can invoke that workflow. If your tenant
does not permit this, stop; this script does not support tenant-authenticated
webhooks. A separately designed authenticated integration is needed.

Check the workflow's status in [Power Automate](https://make.powerautomate.com/)
under **My flows**, in the environment where it was created. Organizational
policies can suspend the webhook trigger even when Teams displays **Active**.
See [Workflow suspended or disabled](#workflow-suspended-or-disabled) below.

Office 365 connector-based Incoming Webhooks were retired in May 2026. They are
not the same thing as the Logic Apps managed Teams connector, which this project
does not use.

## Deploy

The examples below use a Linux terminal. Scripts include a Bash shebang and are
tracked as executable, so no interpreter prefix is needed. If a downloaded
archive loses executable permissions, restore them with
`chmod +x deploy.sh deploy-alert-rules.sh tests/test_cli.sh`.

```shell
cd /path/to/azure-monitor-teams-alerts  # Adjust for your machine.
az login

# Hidden input avoids storing the URL in shell history.
read -r -s -p 'Teams Workflows URL: ' TEAMS_WORKFLOW_URL; echo
export TEAMS_WORKFLOW_URL

./deploy.sh -g rg-monitoring -l westus2 \
  -s '<subscription-uuid>' --test

unset TEAMS_WORKFLOW_URL
```

Alternatively omit the environment variable and let the script prompt. CI must
inject it as a secret environment variable; do not put it on the command line.

Without an alert processing rule, attach the newly created `alerts-teams-ag`
action group to the Azure Monitor alert rules you want to forward. The receiver
uses the common alert schema. The main deployment script does not create alert
rules; the optional script below does.

```shell
# Offline input/JSON checks only; needs a real URL locally, but does not call Azure.
./deploy.sh -g rg-monitoring -l westus2 -s '<subscription-uuid>' --check

# Test an existing Logic App without redeploying or supplying the Teams URL.
./deploy.sh -g rg-monitoring -s '<subscription-uuid>' --test-only

# Change filtering and resource names.
./deploy.sh -g rg-monitoring -l westus2 -p ops-alerts \
  --severities Sev0,Sev1 --no-resolved

# Opt-in routing for an entire resource group (use carefully).
./deploy.sh -g rg-monitoring -l westus2 -s '<subscription-uuid>' \
  --apr-scope '/subscriptions/<subscription-uuid>/resourceGroups/rg-workloads'
```

An APR adds the action group to matching **existing alerts**; it does not create
alerts or guarantee every alert type supports processing rules. Service Health
alerts, for example, are not supported by APRs. Existing suppression rules can
take precedence. Subscription-wide scopes can be noisy and increase costs.
Scope must be in the deployment subscription.

Re-running updates resources with the same names. Dedicated resources are
recommended: definitions and receivers are replaced, not merged. Omitting
`--apr-scope` on a later run does **not** delete or disable an existing APR.
Disable or remove it explicitly in Azure if no longer wanted. Resources are
provisioned sequentially: a failed run may leave partial resources; fix the
reported error and rerun. The script never deletes cloud resources.

## Optional subscription-wide VM CPU rules

After deploying the notification pipeline, use `deploy-alert-rules.sh` separately
to create metric alert rules. It requires the prerequisites above, an existing
resource group/action group, read access to all target
VMs/metrics, and permission to read/write `Microsoft.Insights/metricAlerts`.
No Teams webhook URL is needed.

```shell
# Preview only: Azure reads, no resource changes.
./deploy-alert-rules.sh -g rg-monitoring -s '<subscription-uuid>' \
  --regions eastus2,southcentralus

# Apply disabled rules for customer review.
./deploy-alert-rules.sh -g rg-monitoring -s '<subscription-uuid>' \
  --regions eastus2,southcentralus --apply

# Explicitly enable (or update and enable) after policy/destination approval.
./deploy-alert-rules.sh -g rg-monitoring -s '<subscription-uuid>' \
  --regions eastus2,southcentralus --apply --enable
```

`EUS2` and `SCUS` aliases are accepted. Regions are always explicit; the script
does not discover additional regions or deploy to the whole subscription blindly.
It creates one subscription-scoped, multi-resource rule per selected region:
`alerts-teams-vm-cpu-eastus2` and `alerts-teams-vm-cpu-southcentralus` in this example.
Each rule evaluates **each VM separately**, not the fleet average:

- Platform metric `Percentage CPU`, average **strictly greater than 90%** over
  five minutes, evaluated every minute, severity **Sev2**, automatic resolution.
- All `Microsoft.Compute/virtualMachines` in that subscription/region, across
  resource groups. Future VMs in those regions are included automatically.
  New regions require another explicit deployment. VM scale sets are not included.
- No guest agent or Log Analytics workspace is needed for this platform metric.
- The default destination is `<prefix>-ag` in the supplied resource group.
  Use `-p/--prefix` to match another deployment, or `--action-group-id` to specify
  an existing action group in the same subscription.

The script validates the enabled action group/common-schema Logic App receiver,
checks a representative VM's CPU metric in each region, and checks all rule-name
collisions before any writes. At least one visible VM per region is required.
It does not prove subscription-wide visibility; use an identity with read access
across the subscription. Preview prints the exact proposed resource bodies.
Only rules with this script's ownership tags are updated; unrelated rules are
never adopted. Existing equivalent rules under other names are not deduplicated.

**Every apply without `--enable` writes disabled rules, including updates.**
Removing a region from the command does not delete or disable its existing rule.
Applies are sequential, not transactional; failure can leave partial changes.
Each written rule is read back and checked. No rules are deleted automatically.
Enabled metric alerts are billed based on monitored time series, not just rule
count; notification workflow executions also incur charges. A disabled or
policy-blocked Teams workflow will not deliver notifications even if the rules work.
This script has offline mocked validation only; validate it in an approved test
subscription before production use.

### Multiple subscriptions

Run the script once per subscription, listing the regions to cover in that
subscription. Two subscriptions across two regions require four rules in total:

```shell
# Preview two regional rules in subscription 1.
./deploy-alert-rules.sh -g rg-monitoring -s '<subscription-1-uuid>' \
  --regions eastus2,southcentralus

# Preview two regional rules in subscription 2.
./deploy-alert-rules.sh -g rg-monitoring -s '<subscription-2-uuid>' \
  --regions eastus2,southcentralus
```

Each subscription needs the specified existing resource group and an enabled
action group with a common-schema Logic App receiver in that subscription.
By default, the script references `alerts-teams-ag` in the supplied resource
group; use `--action-group-id` or `--prefix` for a different destination.
The deployment identity needs the required permissions in each subscription,
and each selected region needs at least one visible VM for validation.
Both notification pipelines can post to the same approved Teams channel.

These commands only preview. Add `--apply` to each command to create disabled
rules, then use `--apply --enable` after reviewing the rules and confirming
destination/policy approval. Neither subscription is changed automatically
when the other is deployed.

## Verification and troubleshooting

```shell
python -m unittest discover -s tests -p 'test_*.py'
./tests/test_cli.sh
./deploy-alert-rules.sh --help
```

These are offline tests using mocked Azure CLI and curl. They do not verify
live Azure deployment, WDL runtime behavior, tenant policy, or Teams delivery.
`--check` similarly checks local inputs and JSON, not Azure service validation.

`--test` / `--test-only` send a synthetic Fired alert using the first selected
severity (Sev0 by default). For an existing deployment, select a severity its
deployed filter allows. The test waits up to 120 seconds for the Logic App
response. A success means the Teams workflow accepted the HTTP request, **not**
that its downstream post succeeded. Confirm the card in Teams and inspect the
Teams workflow's run history. The deployed Logic App also returns `filtered`
for excluded alerts and HTTP 502 if its outbound request fails.
Request-schema validation is explicitly enabled: payloads that violate the
declared schema are rejected with HTTP 400 before filtering or forwarding.

Inspect Logic App runs for failures, and the Teams workflow for authentication
or posting failures. A test can time out while retries are still running; avoid
blindly repeating it. Missing resolved notifications can also mean the alert
source/rule does not emit resolved events. Unexpected severity formats are
rejected; this implementation targets the Azure Monitor common alert schema.
Large alert payloads can exceed Teams card limits (28 KB); delivery then fails.
Monitor these resources separately so failures of the alert pipeline are visible.
Consumption executions and actions incur Azure charges; Teams/Power Automate
licensing and tenant policies also apply.

### Workflow suspended or disabled

If the outbound HTTP action fails with `WorkflowTriggerIsNotEnabled`, open the
destination workflow in Power Automate and inspect its status and warnings.
A Teams **Active** label or enabled toggle alone does not establish that the
trigger can run.

- If the flow is simply turned off and policy permits it, turn it on and send one
  controlled test.
- If an admin data policy restricts `TeamsWebhookRequestReceived`, contact your
  Power Platform administrator or internal support team. Ask which approved
  environment permits this trigger and **Anyone** authentication. An Azure
  subscription owner or Teams channel owner is not necessarily the policy owner.
- Do not bypass the restriction or weaken a tenant-wide policy for testing.
  Recreating the workflow in the same restricted environment does not resolve it.
  Even a successful test after enabling the flow does not establish policy
  approval or permanent remediation.
- If anonymous webhooks are prohibited, use a separately designed, approved
  authenticated integration.

An approved development tenant can host the Teams workflow while the Logic App
stays in the original Azure subscription: this integration calls a signed HTTPS
URL and does not require a shared tenant identity. Obtain authorization before
sending data across tenants. Even synthetic cards include Azure resource IDs.
After replacing the destination URL, rerun deployment to update the workflow
parameter; `--test-only` does not update it.

### Live validation results

The following checks passed in a dedicated test deployment on November 17, 2026:

| Check | Observed result |
| --- | --- |
| Invalid schema ID, invalid monitor condition, empty target array, or missing required field | HTTP 400 |
| Valid Sev4 request with the default severity filter | HTTP 200 with `filtered`; no card |
| Synthetic Fired alert | Logic App succeeded and the card appeared in Teams |
| Real Sev2 Azure Monitor metric alert routed through the action group | Logic App succeeded and the matching alert card appeared in Teams |
| Synthetic Sev2 Resolved alert | Green Resolved card appeared with a populated resolved timestamp |
| Automatic resolution of the real Sev2 metric alert | Matching green Resolved card appeared with the original fired time and a populated resolved timestamp |

These are point-in-time results, not guarantees for other tenants or policies.
Suppression with `--no-resolved`, outbound retry exhaustion, and duplicate-delivery
behavior remain unverified live.

To repeat the full-path test, attach the deployed action group to a dedicated
Sev2 alert rule, trigger it safely, and verify the matching alert name in Teams.
The synthetic `--test-only` command bypasses Azure Monitor and the action group.
Disable temporary alert rules after testing to stop ongoing evaluation and
potential charges; delete test resources only after confirming they are no longer
needed. Avoid rules that repeatedly fire on their own alert-processing activity:
the temporary `RunsStarted > 0` test rule fired again when the Logic App processed
its resolution notification. The rule was disabled after verification. Do not
use that self-monitoring condition as a production failure alert.

## Public repository safety

Keep this folder in its own Git repository, not part of a home-directory
repository. Before committing or publishing changes, verify:

```shell
git rev-parse --show-toplevel
git status --short --untracked-files=all
git diff --cached
```

- Commit only reviewed source, documentation, and tests. Do not copy deployment
  exports, `.env` files, parameter files, logs, tenant data, or actual alert payloads.
- Teams webhook URLs **and** Logic App callback URLs are credentials. The script
  disables tracing, passes secrets in private temporary files instead of process
  arguments, and removes those files on normal exit/error. SIGKILL or machine
  crashes can leave files behind. Use a trusted machine and secure temporary storage;
  POSIX permissions in Git Bash do not replace Windows account/ACL protections.
- Workflow parameters use `SecureString`; the HTTP action hides inputs/outputs
  and the request trigger hides outputs in run history. Azure administrators and
  principals able to list callback URLs or modify workflows can still access or
  misuse the integration. Use least privilege; do not run with CLI debug logging.
- Ignore rules are a guardrail, not a secret scanner. Enable GitHub secret scanning
  and push protection where available, review all files and commit history before
  making the repository public, and rotate any URL accidentally disclosed.
- Runtime cards contain resource identifiers and alert names. Only use a channel
  whose membership is authorized for those details. Runtime payloads stay out of Git.

## References

- [Teams webhook guidance and Office 365 connector retirement](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook)
- [Azure Monitor common alert schema](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-common-schema)
- [Metric alerts across multiple resources](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-create-rule-cli-powershell-arm)
- [Alert processing rules and limitations](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-processing-rules)
- [Logic Apps REST API](https://learn.microsoft.com/en-us/rest/api/logic/workflows/create-or-update)
- [Logic Apps security](https://learn.microsoft.com/en-us/azure/logic-apps/set-up-security-permissions)
- [Troubleshoot Power Automate triggers and policy suspensions](https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/flow-run-issues/triggers-troubleshoot)
