# Azure Monitor alerts to Teams

Bash CLI that provisions a Consumption Logic App and an Azure Monitor action group
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

In the target channel's **Workflows** app, use the template that posts to a channel
when a webhook request is received. Configure authentication as **Anyone**, select
the channel, sign in to the Teams connection, and save the workflow. Use a template
that accepts the standard `type: message` / `attachments` Adaptive Card envelope.
Copy its signed URL privately.

This is the simplest current webhook integration, but it is **not zero-setup**.
The workflow and Teams connection have user ownership. Assign an appropriate
co-owner and follow your tenant's account/lifecycle policies. An "Anyone" URL is a
bearer credential: anyone possessing it can invoke that workflow. If your tenant
does not permit this, stop; this script does not support tenant-authenticated
webhooks. A separately designed authenticated integration is needed.

Office 365 connector-based Incoming Webhooks were retired in May 2026. They are
not the same thing as the Logic Apps managed Teams connector, which this project
does not use.

## Deploy

Run these commands **in Bash**, including on Windows:

```bash
cd /c/path/to/logic-app-alerts  # Git Bash; adjust for your machine
az login

# Hidden input avoids storing the URL in shell history.
read -r -s -p 'Teams Workflows URL: ' TEAMS_WORKFLOW_URL; echo
export TEAMS_WORKFLOW_URL

bash deploy.sh -g rg-monitoring -l westus2 \
  -s '<subscription-uuid>' --test

unset TEAMS_WORKFLOW_URL
```

Alternatively omit the environment variable and let the script prompt. CI must
inject it as a secret environment variable; do not put it on the command line.

Without an alert processing rule, attach the newly created `alerts-teams-ag`
action group to the Azure Monitor alert rules you want to forward. The receiver
uses the common alert schema. This project does not create alert rules.

```bash
# Offline input/JSON checks only; needs a real URL locally, but does not call Azure.
bash deploy.sh -g rg-monitoring -l westus2 -s '<subscription-uuid>' --check

# Test an existing Logic App without redeploying or supplying the Teams URL.
bash deploy.sh -g rg-monitoring -s '<subscription-uuid>' --test-only

# Change filtering and resource names.
bash deploy.sh -g rg-monitoring -l westus2 -p ops-alerts \
  --severities Sev0,Sev1 --no-resolved

# Opt-in routing for an entire resource group (use carefully).
bash deploy.sh -g rg-monitoring -l westus2 -s '<subscription-uuid>' \
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

## Verification and troubleshooting

```bash
python -m unittest discover -s tests -p 'test_*.py'
bash tests/test_cli.sh
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

## Public repository safety

Publication is intentionally deferred. This folder must be its own Git repository,
not part of your home-directory repository. Before publishing, verify:

```bash
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
- [Alert processing rules and limitations](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-processing-rules)
- [Logic Apps REST API](https://learn.microsoft.com/en-us/rest/api/logic/workflows/create-or-update)
- [Logic Apps security](https://learn.microsoft.com/en-us/azure/logic-apps/set-up-security-permissions)
