import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("metric_alerts", ROOT / "scripts" / "metric_alerts.py")
metric_alerts = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    SPEC.loader.exec_module(metric_alerts)

SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
GROUP_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-test"
ACTION_GROUP_ID = f"{GROUP_ID}/providers/Microsoft.Insights/actionGroups/alerts-teams-ag"
ARGS = ["-g", "rg-test", "-s", SUBSCRIPTION, "--regions", "EUS2,SCUS"]


class MetricAlertTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.writes = {}
        self.existing = []
        self.regions = ["eastus2", "southcentralus"]
        self.enabled_action_group = True
        self.supported_metric = True
        self.corrupt_readback = False

    def azure(self, *args):
        self.calls.append(args)
        if args[:3] == ("monitor", "action-group", "show"):
            return {"enabled": self.enabled_action_group,
                    "logicAppReceivers": [{"useCommonAlertSchema": True}]}
        if args[:2] == ("vm", "list"):
            return [{"id": f"{GROUP_ID}/providers/Microsoft.Compute/virtualMachines/vm-{region}",
                     "location": region} for region in self.regions]
        if args[:4] == ("monitor", "metrics", "alert", "list"):
            return self.existing
        if args[:3] == ("monitor", "metrics", "list-definitions"):
            return [{"name": {"value": "Percentage CPU"},
                     "supportedAggregationTypes": ["Average"]}] if self.supported_metric else []
        if args[0] == "rest":
            method = args[args.index("--method") + 1]
            url = args[args.index("--url") + 1]
            if method == "put":
                path = Path(args[args.index("--body") + 1].removeprefix("@"))
                import json
                self.writes[url] = json.loads(path.read_text(encoding="utf-8"))
                return self.writes[url]
            if method == "get":
                result = copy.deepcopy(self.writes[url])
                if self.corrupt_readback:
                    result["properties"]["enabled"] = not result["properties"]["enabled"]
                return result
        raise AssertionError(f"Unexpected Azure call: {args}")

    def run_script(self, *extra):
        with patch.object(metric_alerts, "azure", side_effect=self.azure):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                metric_alerts.main([*ARGS, *extra])
        return output.getvalue()

    def test_preview_normalizes_regions_and_does_not_write(self):
        output = self.run_script()
        self.assertIn("Preview only", output)
        self.assertIn("alerts-teams-vm-cpu-eastus2", output)
        self.assertIn("alerts-teams-vm-cpu-southcentralus", output)
        self.assertEqual(self.writes, {})

    def test_apply_builds_subscription_scope_per_region_and_verifies(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.calls.clear()
                self.writes.clear()
                output = self.run_script("--apply", *(["--enable"] if enabled else []))
                self.assertEqual(len(self.writes), 2)
                self.assertEqual(sum(call[0] == "rest" and "get" in call for call in self.calls), 2)
                self.assertIn("Rules applied and verified", output)
                for region, body in zip(sorted(self.regions), self.writes.values()):
                    props = body["properties"]
                    self.assertEqual(props["scopes"], [f"/subscriptions/{SUBSCRIPTION}"])
                    self.assertEqual(props["targetResourceRegion"], region)
                    self.assertEqual(props["targetResourceType"], "Microsoft.Compute/virtualMachines")
                    self.assertIs(props["enabled"], enabled)
                    self.assertTrue(props["autoMitigate"])
                    self.assertEqual(props["evaluationFrequency"], "PT1M")
                    self.assertEqual(props["windowSize"], "PT5M")
                    self.assertEqual(props["severity"], 2)
                    self.assertEqual(props["actions"], [{"actionGroupId": ACTION_GROUP_ID}])
                    self.assertEqual(props["criteria"]["odata.type"],
                                     "Microsoft.Azure.Monitor.MultipleResourceMultipleMetricCriteria")
                    criterion, = props["criteria"]["allOf"]
                    self.assertEqual(criterion["operator"], "GreaterThan")
                    self.assertEqual(criterion["threshold"], 90)
                    self.assertEqual(criterion["metricName"], "Percentage CPU")
                    self.assertEqual(criterion["timeAggregation"], "Average")

    def test_late_collision_prevents_all_writes_but_owned_rules_update(self):
        self.existing = [{"name": "alerts-teams-vm-cpu-southcentralus", "tags": {}}]
        with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
            self.run_script("--apply")
        self.assertEqual(self.writes, {})
        self.existing[0]["tags"] = {"managed-by": metric_alerts.MANAGED_BY,
                                    "rule-kind": metric_alerts.RULE_KIND}
        self.assertIn('"operation": "update"', self.run_script("--apply"))

    def test_preflight_rejects_missing_vms_disabled_destination_or_metric(self):
        cases = [
            ("regions", ["eastus2"], "No visible VMs"),
            ("enabled_action_group", False, "action group is disabled"),
            ("supported_metric", False, "metric unavailable"),
        ]
        for attribute, value, message in cases:
            with self.subTest(attribute=attribute):
                original = getattr(self, attribute)
                setattr(self, attribute, value)
                with self.assertRaisesRegex(ValueError, message):
                    self.run_script("--apply")
                self.assertEqual(self.writes, {})
                setattr(self, attribute, original)

    def test_readback_mismatch_is_failure(self):
        self.corrupt_readback = True
        with self.assertRaisesRegex(ValueError, "Read-back verification failed"):
            self.run_script("--apply")
        self.assertEqual(len(self.writes), 1)

    def test_invalid_regions_and_cross_subscription_destination_fail_before_azure(self):
        for extra in (
            ["--regions", "eastus2,"],
            ["--regions", "eastus2;unexpected"],
            ["--action-group-id", ACTION_GROUP_ID.replace(SUBSCRIPTION, "1" * 36)],
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.run_script(*extra)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
