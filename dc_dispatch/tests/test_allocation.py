import unittest

from dc_dispatch.services.allocation import (
    StoreInput,
    allocate_integer_with_caps,
    allocate_style,
    validate_related_sets,
    variant_dispatch_targets,
)


class TestAllocation(unittest.TestCase):
    def test_integer_allocation_redistributes_store_caps(self):
        result = allocate_integer_with_caps(
            20,
            weights={"A": 60, "B": 30, "C": 10},
            caps={"A": 5, "B": 20, "C": 20},
        )
        self.assertEqual(sum(result.values()), 20)
        self.assertEqual(result["A"], 5)
        self.assertGreater(result["B"], result["C"])

    def test_variant_targets_respect_stock_and_total(self):
        result = variant_dispatch_targets(
            {"S": 2, "M": 7, "L": 1},
            8,
        )
        self.assertEqual(sum(result.values()), 8)
        self.assertLessEqual(result["S"], 2)
        self.assertLessEqual(result["M"], 7)
        self.assertLessEqual(result["L"], 1)

    def test_minimum_bundle_is_all_or_zero(self):
        stores = [
            StoreInput(
                "Store A",
                100,
                tier="A",
                priority=1,
                minimum_per_variant=1,
            ),
            StoreInput(
                "Store B",
                80,
                tier="B",
                priority=2,
                minimum_per_variant=1,
            ),
        ]
        result = allocate_style(
            {"S": 1, "M": 1, "L": 1},
            3,
            stores,
        )
        self.assertEqual(
            sum(result.quantities["Store A"].values()),
            3,
        )
        self.assertEqual(
            sum(result.quantities["Store B"].values()),
            0,
        )
        self.assertIn("Store B", result.skipped_stores)

    def test_display_bundle_precedes_size_ratio_rounding(self):
        stores = [
            StoreInput(
                "Store A",
                100,
                minimum_per_variant=1,
            )
        ]
        result = allocate_style(
            {"S": 100, "M": 1},
            20,
            stores,
        )
        self.assertGreaterEqual(
            result.quantities["Store A"]["M"],
            1,
        )
        self.assertEqual(
            sum(result.quantities["Store A"].values()),
            20,
        )

    def test_zero_sales_store_gets_only_display_bundle(self):
        stores = [
            StoreInput(
                "Selling Store",
                100,
                minimum_per_variant=1,
            ),
            StoreInput(
                "Zero Store",
                0,
                minimum_per_variant=1,
            ),
        ]
        result = allocate_style(
            {"S": 10, "M": 10},
            12,
            stores,
        )
        self.assertEqual(
            result.quantities["Zero Store"],
            {"S": 1, "M": 1},
        )
        self.assertEqual(
            sum(result.quantities["Selling Store"].values()),
            10,
        )

    def test_store_maximum_is_respected(self):
        stores = [
            StoreInput(
                "A",
                100,
                minimum_per_variant=1,
                maximum_per_style=4,
            ),
            StoreInput(
                "B",
                50,
                minimum_per_variant=1,
            ),
        ]
        result = allocate_style(
            {"S": 10, "M": 10},
            16,
            stores,
        )
        self.assertLessEqual(
            sum(result.quantities["A"].values()),
            4,
        )
        self.assertEqual(
            sum(
                sum(values.values())
                for values in result.quantities.values()
            ),
            16,
        )

    def test_depth_is_budgeted_by_store_then_fulfilled(self):
        stores = [
            StoreInput(
                "A",
                100,
                tier="A",
                priority=2,
                minimum_per_variant=1,
            ),
            StoreInput(
                "B",
                50,
                tier="A",
                priority=1,
                minimum_per_variant=1,
            ),
        ]
        result = allocate_style(
            {"S": 10, "M": 10, "L": 10},
            18,
            stores,
        )

        # Both stores first get a complete S/M/L bundle.
        for warehouse in ("A", "B"):
            self.assertGreaterEqual(
                result.quantities[warehouse]["S"],
                1,
            )
            self.assertGreaterEqual(
                result.quantities[warehouse]["M"],
                1,
            )
            self.assertGreaterEqual(
                result.quantities[warehouse]["L"],
                1,
            )

        # Remaining depth follows historical demand and is assigned as a
        # coherent store budget rather than independently size by size.
        self.assertGreater(
            result.depth_targets["A"],
            result.depth_targets["B"],
        )
        self.assertGreater(
            sum(result.quantities["A"].values()),
            sum(result.quantities["B"].values()),
        )

    def test_related_set_validation(self):
        rows = [
            {
                "related_set": "SET-1",
                "store_warehouse": "Store A",
                "item_template": "Top",
                "final_qty": 2,
                "exclude": 0,
            },
            {
                "related_set": "SET-1",
                "store_warehouse": "Store A",
                "item_template": "Skirt",
                "final_qty": 0,
                "exclude": 0,
            },
        ]
        errors = validate_related_sets(
            rows,
            {"SET-1": {"Top", "Skirt"}},
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("Skirt", errors[0])


if __name__ == "__main__":
    unittest.main()
