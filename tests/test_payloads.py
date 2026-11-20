import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("payloads", ROOT / "scripts" / "payloads.py")
payloads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(payloads)

# Deliberately fake signed URLs for offline tests; not usable credentials.
CALLBACK = "https://example.logic.azure.com/workflows/fake/triggers/When_an_HTTP_request_is_received/invoke?sig=TEST_ONLY"
TEAMS = "https://example.environment.api.powerplatform.com/powerautomate/automations/direct/workflows/fake/triggers/manual/paths/invoke?sig=TEST_ONLY"


class PayloadTests(unittest.TestCase):
    def test_workflow_configuration(self):
        body = payloads.workflow_body("westus2", "Sev0,Sev2", "false", TEAMS)
        self.assertEqual(body["location"], "westus2")
        props = body["properties"]
        self.assertEqual(props["state"], "Enabled")
        self.assertEqual(props["parameters"]["severities"]["value"], ["Sev0", "Sev2"])
        self.assertIs(props["parameters"]["sendResolved"]["value"], False)
        self.assertEqual(props["parameters"]["teamsWorkflowUrl"]["value"], TEAMS)
        self.assertEqual(props["definition"]["parameters"]["teamsWorkflowUrl"]["type"], "SecureString")

    def test_invalid_configuration(self):
        for severity in ("", "Sev5", "Sev0,", "Sev0,sev1"):
            with self.subTest(severity=severity), self.assertRaises(ValueError):
                payloads.workflow_body("westus2", severity, "true", TEAMS)
        with self.assertRaises(ValueError):
            payloads.workflow_body("westus2", "Sev0", "yes", TEAMS)

    def test_url_validation(self):
        self.assertEqual(payloads.signed_url(CALLBACK), CALLBACK)
        self.assertEqual(payloads.signed_url(TEAMS, teams=True), TEAMS)
        invalid = (
            "http://example.logic.azure.com/?sig=TEST_ONLY",
            "https://example.logic.azure.com.evil.invalid/?sig=TEST_ONLY",
            "https://example.logic.azure.com/",
            "https://user:password@example.logic.azure.com/?sig=TEST_ONLY",
            CALLBACK + "#fragment",
            CALLBACK + "\n",
            CALLBACK.replace(".com/", ".com:444/"),
            CALLBACK.replace(".com/", ".com:invalid/"),
            None,
            {"url": CALLBACK},
            TEAMS,
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                payloads.signed_url(url)

    def test_action_group_uses_common_schema(self):
        body = payloads.action_group_body(CALLBACK, "/fake-workflow", "a" * 60)
        self.assertEqual(body["location"], "global")
        props = body["properties"]
        self.assertEqual(len(props["groupShortName"]), 12)
        receiver, = props["logicAppReceivers"]
        self.assertTrue(receiver["useCommonAlertSchema"])
        self.assertEqual(receiver["resourceId"], "/fake-workflow")
        self.assertEqual(receiver["callbackUrl"], CALLBACK)

    def test_processing_rule_is_additive(self):
        props = payloads.apr_body("/fake-scope", "/fake-action-group")["properties"]
        self.assertEqual(props["scopes"], ["/fake-scope"])
        self.assertEqual(props["actions"], [{
            "actionType": "AddActionGroups", "actionGroupIds": ["/fake-action-group"]
        }])

    def test_test_payload_and_private_curl_config(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            payloads.write_json(directory / "callback.json", CALLBACK)
            payloads.test_files(directory, "/fake-workflow", "Sev2")
            config = (directory / "curl.conf").read_text(encoding="utf-8")
            self.assertEqual(json.loads(config.removeprefix("url = ")), CALLBACK)
            sample = payloads.read_json(directory / "sample.json")
            self.assertEqual(sample["schemaId"], "azureMonitorCommonAlertSchema")
            essentials = sample["data"]["essentials"]
            self.assertEqual(essentials["severity"], "Sev2")
            self.assertEqual(essentials["monitorCondition"], "Fired")
            self.assertEqual(essentials["alertTargetIDs"], ["/fake-workflow"])

    def test_response_must_confirm_forwarding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            for response in ({}, {"status": "filtered"}, {"status": "teams-workflow-request-failed"}):
                payloads.write_json(path, response)
                with self.subTest(response=response), self.assertRaises(ValueError):
                    payloads.main(["verify", str(path)])
            payloads.write_json(path, {"status": "accepted-by-teams-workflow"})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                payloads.main(["verify", str(path)])
            self.assertIn("not proof", output.getvalue())

    def test_workflow_delivery_and_error_contract(self):
        workflow = payloads.read_json(ROOT / "workflow.json")
        trigger = workflow["triggers"]["When_an_HTTP_request_is_received"]
        self.assertEqual(trigger["inputs"]["method"], "POST")
        self.assertEqual(trigger.get("operationOptions"), "EnableSchemaValidation")
        self.assertEqual(trigger["runtimeConfiguration"]["secureData"]["properties"], ["outputs"])
        schema = trigger["inputs"]["schema"]
        self.assertEqual(schema["properties"]["schemaId"]["enum"], ["azureMonitorCommonAlertSchema"])
        essentials = schema["properties"]["data"]["properties"]["essentials"]
        self.assertEqual(essentials["properties"]["monitorCondition"]["enum"], ["Fired", "Resolved"])
        self.assertEqual(essentials["properties"]["alertTargetIDs"]["minItems"], 1)
        filter_action = workflow["actions"]["Filter_alert"]
        self.assertIn("contains(parameters('severities')", filter_action["expression"])
        self.assertIn("parameters('sendResolved')", filter_action["expression"])
        actions = filter_action["actions"]
        post = actions["Post_to_Teams_workflow"]
        self.assertEqual(post["inputs"]["uri"], "@parameters('teamsWorkflowUrl')")
        self.assertEqual(post["inputs"]["retryPolicy"]["type"], "exponential")
        self.assertEqual(post["inputs"]["retryPolicy"]["count"], 4)
        self.assertEqual(post["runtimeConfiguration"]["secureData"]["properties"], ["inputs", "outputs"])
        card = post["inputs"]["body"]["attachments"][0]["content"]
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertIn("'Good', 'Attention'", card["body"][0]["color"])
        self.assertEqual(card["actions"][0]["type"], "Action.OpenUrl")
        self.assertEqual(actions["Delivery_failed"]["inputs"]["statusCode"], 502)
        self.assertEqual(actions["Mark_run_failed"]["inputs"]["runStatus"], "Failed")
        self.assertEqual(actions["Delivery_accepted"]["runAfter"], {"Post_to_Teams_workflow": ["Succeeded"]})
        self.assertEqual(filter_action["else"]["actions"]["Filtered"]["inputs"]["body"], {"status": "filtered"})


if __name__ == "__main__":
    unittest.main()
