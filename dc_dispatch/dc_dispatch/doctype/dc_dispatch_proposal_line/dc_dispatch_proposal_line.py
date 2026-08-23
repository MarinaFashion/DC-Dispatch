import frappe
from frappe import _
from frappe.model.document import Document


class DCDispatchProposalLine(Document):
    def on_trash(self):
        """Protect the approved dispatch audit trail from manual row deletion."""
        if not self.run:
            return

        run_status = frappe.db.get_value(
            "DC Dispatch Run",
            self.run,
            "status",
        )

        if run_status in {
            "Approved",
            "Material Requests Created",
        }:
            frappe.throw(
                _(
                    "DC Dispatch Proposal Line {0} cannot be deleted because "
                    "DC Dispatch Run {1} is already {2}. "
                    "Cancel the DC Dispatch Run workflow instead of deleting "
                    "individual proposal lines."
                ).format(
                    self.name,
                    self.run,
                    run_status,
                )
            )

