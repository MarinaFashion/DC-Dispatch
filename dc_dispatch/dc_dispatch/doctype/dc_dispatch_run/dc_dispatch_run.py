import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate


FINAL_STATUSES = {"Approved", "Material Requests Created", "Cancelled"}


class DCDispatchRun(Document):
    def before_insert(self):
        self._apply_defaults()

    def validate(self):
        self._validate_dates()
        self._validate_rows()
        self._protect_finalized_run()

    def on_trash(self):
        material_requests = frappe.get_all(
            "Material Request",
            filters={"custom_dc_dispatch_run": self.name},
            pluck="name",
            limit_page_length=0,
        )
        if material_requests:
            preview = ", ".join(material_requests[:15])
            more = (
                f" (+{len(material_requests) - 15} more)"
                if len(material_requests) > 15
                else ""
            )
            frappe.throw(
                _(
                    "Cannot delete DC Dispatch Run {0}. Cancel and delete all "
                    "Material Requests created by this run first: {1}{2}"
                ).format(self.name, preview, more)
            )

    def _apply_defaults(self):
        settings = frappe.get_single("DC Dispatch Settings")
        self.company = self.company or settings.company
        self.source_warehouse = (
            self.source_warehouse or settings.default_source_warehouse
        )
        for item in self.items:
            if not item.dispatch_percentage:
                item.dispatch_percentage = (
                    settings.default_dispatch_percentage or 80
                )

    def _validate_dates(self):
        if self.sales_from_date and self.sales_to_date:
            if getdate(self.sales_from_date) > getdate(self.sales_to_date):
                frappe.throw(
                    _("Sales From Date cannot be after Sales To Date.")
                )
        if (
            flt(self.minimum_match_percent) <= 0
            or flt(self.minimum_match_percent) > 100
        ):
            frappe.throw(
                _(
                    "Minimum Field Match % must be greater than 0 "
                    "and no more than 100."
                )
            )

    def _validate_rows(self):
        duplicate_fields = set()
        seen_fields = set()
        for row in self.reference_fields:
            key = (row.main_group, row.fieldname)
            if key in seen_fields:
                duplicate_fields.add(
                    f"{row.main_group}: {row.fieldname}"
                )
            seen_fields.add(key)

        if duplicate_fields:
            frappe.throw(
                _("Duplicate matching fields: {0}").format(
                    ", ".join(sorted(duplicate_fields))
                )
            )

        stores = [row.store_warehouse for row in self.store_rules]
        if len(stores) != len(set(stores)):
            frappe.throw(
                _("Each eligible store can appear only once.")
            )

        store_rows = {
            row.store_warehouse: row
            for row in self.store_rules
        }
        for row in self.store_rules:
            if (
                row.decision == "Use Reference Store"
                and not row.reference_store
            ):
                frappe.throw(
                    _("Reference Store is required for {0}.").format(
                        row.store_warehouse
                    )
                )
            if row.reference_store == row.store_warehouse:
                frappe.throw(
                    _("A store cannot use itself as its reference store.")
                )
            if (
                row.decision == "Use Reference Store"
                and row.reference_store not in store_rows
            ):
                frappe.throw(
                    _(
                        "Reference Store {0} is not an eligible store "
                        "in this run."
                    ).format(row.reference_store)
                )
            if (
                row.decision == "Use Reference Store"
                and store_rows[row.reference_store].decision == "Exclude"
            ):
                frappe.throw(
                    _("Reference Store {0} cannot be excluded.").format(
                        row.reference_store
                    )
                )
            if (
                row.minimum_per_variant < 0
                or row.maximum_per_style < 0
            ):
                frappe.throw(
                    _(
                        "Store minimums and maximums "
                        "cannot be negative."
                    )
                )

        templates = [row.item_template for row in self.items]
        if len(templates) != len(set(templates)):
            frappe.throw(
                _("Each Item Template can appear only once.")
            )

        for row in self.items:
            if (
                flt(row.dispatch_percentage) < 0
                or flt(row.dispatch_percentage) > 100
            ):
                frappe.throw(
                    _(
                        "Dispatch percentage for {0} must be "
                        "between 0 and 100."
                    ).format(row.item_template)
                )

    def _protect_finalized_run(self):
        if self.is_new():
            return

        previous = self.get_doc_before_save()
        if (
            previous
            and previous.status in FINAL_STATUSES
            and not self.flags.get("allow_final_status_update")
        ):
            frappe.throw(_("A finalized run cannot be edited."))

    @frappe.whitelist()
    def load_eligible_stores(self):
        from dc_dispatch.services.run_service import load_eligible_stores
        return load_eligible_stores(self)

    @frappe.whitelist()
    def load_target_items(self):
        from dc_dispatch.services.run_service import load_target_items
        return load_target_items(self)

    @frappe.whitelist()
    def analyze_store_history(self):
        from dc_dispatch.services.history_policy_service import (
            analyze_store_history,
        )
        return analyze_store_history(self)

    @frappe.whitelist()
    def calculate_proposal(self):
        from dc_dispatch.services.history_policy_service import (
            calculate_proposal,
        )
        return calculate_proposal(self)

    @frappe.whitelist()
    def export_proposal(self):
        from dc_dispatch.services.excel_service import export_proposal
        return export_proposal(self)

    @frappe.whitelist()
    def import_proposal(self):
        from dc_dispatch.services.excel_service import import_proposal
        return import_proposal(self)

    @frappe.whitelist()
    def approve_proposal(self):
        from dc_dispatch.services.run_service import approve_proposal
        return approve_proposal(self)

    @frappe.whitelist()
    def create_material_requests(self):
        from dc_dispatch.services.material_request_service import (
            create_material_requests,
        )
        return create_material_requests(self)

    @frappe.whitelist()
    def cancel_run(self):
        if self.status in {
            "Draft",
            "Items Loaded",
            "Reference Review Required",
            "Calculated",
            "Proposal Imported",
        }:
            self.flags.allow_final_status_update = True
            self.status = "Cancelled"
            self.save()
            return {"status": self.status}

        if self.status in {"Approved", "Material Requests Created"}:
            material_requests = frappe.get_all(
                "Material Request",
                filters={"custom_dc_dispatch_run": self.name},
                fields=["name", "docstatus"],
                limit_page_length=0,
            )
            active = [
                row.name
                for row in material_requests
                if int(row.docstatus or 0) != 2
            ]
            if active:
                preview = ", ".join(active[:15])
                more = (
                    f" (+{len(active) - 15} more)"
                    if len(active) > 15
                    else ""
                )
                frappe.throw(
                    _(
                        "Cancel the generated Material Requests first. "
                        "Active requests: {0}{1}"
                    ).format(preview, more)
                )

            self.flags.allow_final_status_update = True
            self.status = "Cancelled"
            self.save()
            return {"status": self.status}

        frappe.throw(
            _("This DC Dispatch Run cannot be cancelled from status {0}.").format(
                self.status
            )
        )


@frappe.whitelist()
def get_eligible_item_fields():
    from dc_dispatch.services.metadata import get_eligible_item_fields
    return get_eligible_item_fields()


@frappe.whitelist()
def get_target_filter_options(
    item_year=None,
    season=None,
    collection=None,
    drop=None,
    main_group=None,
    subgroup=None,
):
    from dc_dispatch.services.metadata import get_target_filter_options

    return get_target_filter_options(
        item_year=item_year,
        season=season,
        collection=collection,
        drop=drop,
        main_group=main_group,
        subgroup=subgroup,
    )
