"""Internal JSON serialization for deploy.sh; no Azure SDK or third-party packages."""

import datetime
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent.parent


def signed_url(value, *, teams=False):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Expected a signed HTTPS URL")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    supported = host.endswith(".logic.azure.com")
    if teams:
        supported |= host.endswith(".environment.api.powerplatform.com")
    if not (
        parsed.scheme == "https"
        and supported
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and parse_qs(parsed.query).get("sig")
    ):
        raise ValueError("Expected a signed HTTPS workflow URL on a supported public-cloud host")
    return value


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def workflow_body(location, severities, send_resolved, url):
    allowed = severities.split(",")
    if not allowed or any(value not in {f"Sev{i}" for i in range(5)} for value in allowed):
        raise ValueError("Invalid severity list")
    if send_resolved not in ("true", "false"):
        raise ValueError("Invalid resolved-alert setting")
    return {
        "location": location,
        "properties": {
            "state": "Enabled",
            "definition": read_json(ROOT / "workflow.json"),
            "parameters": {
                "teamsWorkflowUrl": {"value": signed_url(url, teams=True)},
                "severities": {"value": allowed},
                "sendResolved": {"value": send_resolved == "true"},
            },
        },
    }


def action_group_body(callback, workflow_id, prefix):
    return {
        "location": "global",
        "properties": {
            "enabled": True,
            "groupShortName": prefix[:12],
            "logicAppReceivers": [{
                "name": "TeamsWorkflow",
                "resourceId": workflow_id,
                "callbackUrl": signed_url(callback),
                "useCommonAlertSchema": True,
            }],
        },
    }


def apr_body(scope, action_group_id):
    return {
        "location": "global",
        "properties": {
            "enabled": True,
            "description": "Route Azure Monitor alerts to Teams.",
            "scopes": [scope],
            "actions": [{"actionType": "AddActionGroups", "actionGroupIds": [action_group_id]}],
        },
    }


def test_files(directory, workflow_id, severity):
    directory = Path(directory)
    callback = signed_url(read_json(directory / "callback.json"))
    (directory / "curl.conf").write_text(
        "url = " + json.dumps(callback) + "\n", encoding="utf-8"
    )
    write_json(directory / "sample.json", {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {"essentials": {
            "alertRule": "Synthetic Teams delivery test",
            "severity": severity,
            "monitorCondition": "Fired",
            "alertTargetIDs": [workflow_id],
            "firedDateTime": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }},
    })


def main(args):
    mode, destination, *values = args
    if mode == "workflow":
        write_json(destination, workflow_body(*values, os.environ["TEAMS_WORKFLOW_URL"]))
    elif mode == "action-group":
        callback_file, workflow_id, prefix = values
        write_json(destination, action_group_body(read_json(callback_file), workflow_id, prefix))
    elif mode == "apr":
        write_json(destination, apr_body(*values))
    elif mode == "test":
        test_files(destination, *values)
    elif mode == "verify":
        if read_json(destination) != {"status": "accepted-by-teams-workflow"}:
            raise ValueError("Test was not forwarded; check deployed filters and run history")
        print("Teams workflow accepted the test. Confirm the card in Teams; acceptance is not proof of channel delivery.")
    else:
        raise ValueError("Unknown payload operation")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, OSError) as error:
        # URL parser errors can contain credentials. Never echo their values.
        if isinstance(error, json.JSONDecodeError):
            sys.exit("Error: invalid JSON response or payload file")
        if isinstance(error, ValueError):
            sys.exit("Error: invalid workflow URL, payload, or test response; check configuration and run history")
        sys.exit("Error: required configuration or payload file is unavailable")
