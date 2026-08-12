import unittest

from dc_dispatch.services.filtering import cascading_options


class TestCascadingFilters(unittest.TestCase):
    def setUp(self):
        self.fieldnames = {
            "season": "season",
            "collection": "collection",
            "main_group": "custom_main_group",
            "subgroup": "custom_subgroup",
        }
        self.rows = [
            {
                "season": "Summer",
                "collection": "Core",
                "custom_main_group": "Dresses",
                "custom_subgroup": "Maxi",
            },
            {
                "season": "Summer",
                "collection": "Fashion",
                "custom_main_group": "Uppers",
                "custom_subgroup": "Blouse",
            },
            {
                "season": "Winter",
                "collection": "Core",
                "custom_main_group": "Dresses",
                "custom_subgroup": "Midi",
            },
        ]

    def test_each_option_ignores_its_own_selection(self):
        result = cascading_options(
            self.rows,
            {
                "season": "Summer",
                "collection": "Core",
                "main_group": None,
                "subgroup": None,
            },
            self.fieldnames,
        )
        self.assertEqual(result["season"], ["Summer", "Winter"])
        self.assertEqual(result["collection"], ["Core", "Fashion"])
        self.assertEqual(result["main_group"], ["Dresses"])
        self.assertEqual(result["subgroup"], ["Maxi"])

    def test_blank_values_are_not_offered(self):
        rows = [*self.rows, {"season": "Summer", "collection": None}]
        result = cascading_options(rows, {}, self.fieldnames)
        self.assertNotIn(None, result["collection"])
        self.assertEqual(result["collection"], ["Core", "Fashion"])

    def test_missing_optional_mapping_does_not_blank_other_filters(self):
        fieldnames = {**self.fieldnames, "subgroup": None}
        result = cascading_options(
            self.rows,
            {
                "season": "Summer",
                "collection": None,
                "main_group": None,
                "subgroup": "Old Saved Value",
            },
            fieldnames,
        )
        self.assertEqual(result["collection"], ["Core", "Fashion"])
        self.assertEqual(result["main_group"], ["Dresses", "Uppers"])
        self.assertEqual(result["subgroup"], [])


if __name__ == "__main__":
    unittest.main()
