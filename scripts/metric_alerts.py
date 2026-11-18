"""Optional subscription-wide VM CPU rules; preview unless --apply is supplied."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from payloads import write_json


MANAGED_BY = "azure-monitor-teams-alerts"
RULE_KIND = "subscription-vm-cpu"
VM_TYPE = "Microsoft.Compute/virtualMachines"


def azure(*args):
    executable = shutil.which("az")
    if not executable:
        raise ValueError("Azure CLI is required; install it and run az login")
    result = subprocess.run(
        [executable, *args, "--only-show-errors", "-o", "json"],
        check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    return json.loads(result.stdout)


def rule_body(subscription, region, action_group_id, enabled):
    return {
        "location": "global",
        "tags": {"managed-by": MANAGED_BY, "rule-kind": RULE_KIND},
        "properties": {
            "description": "Each VM: average Percentage CPU > 90 over 5 minutes.",
            "severity": 2,
            "enabled": enabled,
            "scopes": [f"/subscriptions/{subscription}"],
            "evaluationFrequency": "PT1M",
            "windowSize": "PT5M",
            "targetResourceType": VM_TYPE,
            "targetResourceRegion": region,
            "autoMitigate": True,
            "criteria": {
                "odata.type": "Microsoft.Azure.Monitor.MultipleResourceMultipleMetricCriteria",
                "allOf": [{
                    "name": "HighCpu",
                    "criterionType": "StaticThresholdCriterion",
                    "metricName": "Percentage CPU",
                    "metricNamespace": VM_TYPE,
                    "operator": "GreaterThan",
                    "threshold": 90,
                    "timeAggregation": "Average",
                    "skipMetricValidation": False,
                }],
            },
            "actions": [{"actionGroupId": action_group_id}],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview subscription-wide VM CPU > 90% rules, one per region. "
        "Uses Azure CLI reads; no writes unless --apply. Requires existing VMs "
        "in each selected region for metric validation."
    )
    parser.add_argument("-g", "--resource-group", required=True,
                        help="Existing resource group in which to store the rules")
    parser.add_argument("-s", "--subscription", required=True, help="Subscription UUID")
    parser.add_argument("--regions", required=True,
                        help="Comma-separated region names; EUS2 and SCUS aliases accepted")
    parser.add_argument("-p", "--prefix", default="alerts-teams",
                        help="Rule prefix and default action group prefix")
    parser.add_argument("--action-group-id", help="Defaults to <prefix>-ag in the resource group")
    parser.add_argument("--apply", action="store_true", help="Create/update rules; incurs potential charges")
    parser.add_argument("--enable", action="store_true", help="Enable rules when applied (default: disabled)")
    args = parser.parse_args(argv)
    subscription = str(uuid.UUID(args.subscription))
    if not re.fullmatch(r"[a-zA-Z0-9._()-]{1,90}", args.resource_group) or args.resource_group.endswith("."):
        raise ValueError("Invalid resource group name")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,59}", args.prefix):
        raise ValueError("Prefix must be 1-60 letters, digits, or hyphens, starting with a letter or digit")
    aliases = {"eus2": "eastus2", "scus": "southcentralus"}
    regions = sorted({aliases.get(value.strip().lower(), value.strip().lower())
                      for value in args.regions.split(",")})
    if any(not re.fullmatch(r"[a-z0-9]+", region) for region in regions):
        raise ValueError("Provide comma-separated region names such as eastus2,southcentralus")
    group_id = f"/subscriptions/{subscription}/resourceGroups/{args.resource_group}"
    action_group_id = args.action_group_id or (
        f"{group_id}/providers/Microsoft.Insights/actionGroups/{args.prefix}-ag"
    )
    if not re.fullmatch(
        rf"/subscriptions/{subscription}/resourceGroups/[a-zA-Z0-9._()-]+"
        r"/providers/Microsoft.Insights/actionGroups/[^/]+", action_group_id, re.IGNORECASE
    ):
        raise ValueError("Action group ID must identify an action group in the selected subscription")

    def rest(method, resource_id, *extra):
        return azure("rest", "--subscription", subscription, "--method", method,
                     "--url", f"https://management.azure.com{resource_id}?api-version=2018-03-01",
                     *extra)

    action_group = azure(
        "monitor", "action-group", "show", "--subscription", subscription, "--ids", action_group_id
    )
    if not action_group.get("enabled"):
        raise ValueError("The action group is disabled")
    receivers = action_group.get("logicAppReceivers", [])
    if not any(receiver.get("useCommonAlertSchema") for receiver in receivers):
        raise ValueError("Action group needs a Logic App receiver with common alert schema enabled")
    vms = azure("vm", "list", "--subscription", subscription,
                "--query", "[].{id:id,location:location}")
    existing = azure("monitor", "metrics", "alert", "list",
                     "--subscription", subscription, "--resource-group", args.resource_group)
    by_name = {rule["name"].lower(): rule for rule in existing}
    plan = []
    for region in regions:
        matching = [vm for vm in vms if vm["location"].lower() == region]
        if not matching:
            raise ValueError(f"No visible VMs in {region}; check region and subscription access")
        definitions = azure("monitor", "metrics", "list-definitions",
                            "--subscription", subscription, "--resource", matching[0]["id"])
        if not any(metric["name"]["value"] == "Percentage CPU"
                   and "Average" in metric.get("supportedAggregationTypes", [])
                   for metric in definitions):
            raise ValueError(f"Percentage CPU/Average metric unavailable in {region}")
        name = f"{args.prefix}-vm-cpu-{region}"
        old = by_name.get(name.lower())
        if old is not None:
            tags = old.get("tags") or {}
            if tags.get("managed-by") != MANAGED_BY or tags.get("rule-kind") != RULE_KIND:
                raise ValueError(f"Refusing to overwrite unowned rule {name}; use a different prefix")
        resource_id = f"{group_id}/providers/Microsoft.Insights/metricAlerts/{name}"
        body = rule_body(subscription, region, action_group_id, args.enable)
        plan.append({"id": resource_id, "operation": "update" if old else "create",
                     "currentVmCount": len(matching), "body": body})

    print(json.dumps(plan, indent=2))
    print("Scope: ALL standalone VMs in the subscription's selected regions, including future VMs.")
    print("Each VM is evaluated separately; VM scale sets are not included.")
    print("Warning: enabled metric alerts and notification executions can incur charges.")
    if not args.apply:
        print("Preview only: no Azure resources changed. Supply --apply to write these rules.")
        return
    with tempfile.TemporaryDirectory(prefix="teams-metric-alerts-") as directory:
        path = Path(directory) / "rule.json"
        for item in plan:
            write_json(path, item["body"])
            rest("put", item["id"], "--body", f"@{path}")
            actual = rest("get", item["id"])["properties"]
            expected = item["body"]["properties"]
            for key in ("enabled", "severity", "scopes", "evaluationFrequency", "windowSize",
                        "targetResourceType", "targetResourceRegion", "autoMitigate"):
                if actual.get(key) != expected[key]:
                    raise ValueError(f"Read-back verification failed for {item['id']}: {key}")
            criterion = actual["criteria"]["allOf"][0]
            for key in ("metricName", "metricNamespace", "operator", "threshold", "timeAggregation"):
                if criterion.get(key) != expected["criteria"]["allOf"][0][key]:
                    raise ValueError(f"Read-back verification failed for {item['id']}: {key}")
            if [action["actionGroupId"].lower() for action in actual["actions"]] != [action_group_id.lower()]:
                raise ValueError(f"Read-back verification failed for {item['id']}: action group")
            print(f"Verified {item['operation']}: {item['id']}")
    print("Rules applied and verified. Enabled." if args.enable else
          "Rules applied and verified. Disabled; review before enabling.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Error: {error}\nNo rollback performed; inspect rules if an apply partially completed.")
