import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class TestConfiguration(unittest.TestCase):
    def test_verified_marina_item_field_defaults(self):
        settings_path = (
            ROOT
            / "dc_dispatch"
            / "dc_dispatch"
            / "doctype"
            / "dc_dispatch_settings"
            / "dc_dispatch_settings.json"
        )
        settings = json.loads(settings_path.read_text())
        fields = {row["fieldname"]: row for row in settings["fields"]}
        self.assertEqual(fields["item_main_group_field"]["default"], "custom_item_main_group")
        self.assertEqual(fields["item_subgroup_field"]["default"], "item_sub_group")


if __name__ == "__main__":
    unittest.main()
