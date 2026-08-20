from __future__ import annotations

import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import (
    get_datetime,
    now_datetime,
)

import dc_dispatch.services.run_service as rs
from dc_dispatch.services import historical_cache_service as cache_service


ACTIVE_STATUSES = {
    "Queued",
    "Running",
}
STALE_AFTER_MINUTES = 30


def _json(value):
    return json.dumps(
        value or {},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _set_status(
    run_name,
    status,
    action=None,
    message=None,
    job_id=None,
    result=None,
    started=False,
    completed=False,
):
    values = {
        "background_job_status": status,
    }

    if action is not None:
        values[
            "background_job_action"
        ] = action

    if message is not None:
        values[
            "background_job_message"
        ] = message

    if job_id is not None:
        values[
            "background_job_id"
        ] = job_id

    if result is not None:
        values[
            "background_job_result"
        ] = _json(result)

    if started:
        values[
            "background_job_started_at"
        ] = now_datetime()
        values[
            "background_job_completed_at"
        ] = None

    if completed:
        values[
            "background_job_completed_at"
        ] = now_datetime()

    frappe.db.set_value(
        "DC Dispatch Run",
        run_name,
        values,
        update_modified=False,
    )


def _active_job(run):
    status = (
        run.background_job_status
        or "Idle"
    )

    if (
        status
        not in ACTIVE_STATUSES
    ):
        return False

    started = (
        run.background_job_started_at
    )
    if not started:
        return True

    return (
        now_datetime()
        - get_datetime(started)
        < timedelta(
            minutes=(
                STALE_AFTER_MINUTES
            )
        )
    )


def _prepare_start(
    run,
    action,
):
    rs._require_editable(run)
    rs._require_saved(run)

    if _active_job(run):
        frappe.throw(
            _(
                "A DC Dispatch background task is already "
                "running for this Run: {0}."
            ).format(
                run.background_job_action
                or run.background_job_status
            )
        )

    _set_status(
        run.name,
        "Queued",
        action=action,
        message=(
            "Waiting for a background worker..."
        ),
        job_id="",
        result={},
        started=True,
    )


def _enqueue(
    run,
    action,
    method,
):
    _prepare_start(
        run,
        action,
    )

    job = frappe.enqueue(
        method,
        queue="long",
        timeout=1800,
        enqueue_after_commit=True,
        job_name=(
            f"dc_dispatch:{run.name}:{action}"
        ),
        run_name=run.name,
    )

    job_id = getattr(
        job,
        "id",
        None,
    )

    if job_id:
        _set_status(
            run.name,
            "Queued",
            action=action,
            message=(
                "Waiting for a background worker..."
            ),
            job_id=job_id,
        )

    return {
        "status": "Queued",
        "action": action,
        "job_id": job_id,
    }


@frappe.whitelist()
def start_history_analysis(
    run_name,
):
    run = frappe.get_doc(
        "DC Dispatch Run",
        run_name,
    )

    return _enqueue(
        run,
        "Check Store History",
        (
            "dc_dispatch.services.background_service."
            "run_history_analysis_job"
        ),
    )


@frappe.whitelist()
def start_proposal_calculation(
    run_name,
):
    run = frappe.get_doc(
        "DC Dispatch Run",
        run_name,
    )

    # Quick request-side validation only.
    cache_service.load_cache_data(
        run,
        require_valid=True,
    )

    return _enqueue(
        run,
        "Calculate Proposal",
        (
            "dc_dispatch.services.background_service."
            "run_proposal_calculation_job"
        ),
    )


@frappe.whitelist()
def get_background_status(
    run_name,
):
    values = frappe.db.get_value(
        "DC Dispatch Run",
        run_name,
        [
            "background_job_status",
            "background_job_action",
            "background_job_message",
            "background_job_started_at",
            "background_job_completed_at",
            "background_job_id",
            "background_job_result",
        ],
        as_dict=True,
    )

    if not values:
        frappe.throw(
            _(
                "DC Dispatch Run "
                "does not exist."
            )
        )

    result = {}
    if (
        values.background_job_result
    ):
        try:
            result = json.loads(
                values.background_job_result
            )
        except Exception:
            result = {}

    return {
        "status": (
            values.background_job_status
            or "Idle"
        ),
        "action": (
            values.background_job_action
            or ""
        ),
        "message": (
            values.background_job_message
            or ""
        ),
        "started_at": (
            values.background_job_started_at
        ),
        "completed_at": (
            values.background_job_completed_at
        ),
        "job_id": (
            values.background_job_id
            or ""
        ),
        "result": result,
    }


def run_history_analysis_job(
    run_name,
):
    action = "Check Store History"

    try:
        _set_status(
            run_name,
            "Running",
            action=action,
            message=(
                "Scanning historical sales and returns..."
            ),
        )
        frappe.db.commit()

        run = frappe.get_doc(
            "DC Dispatch Run",
            run_name,
        )

        cache_service.build_cache(
            run
        )

        _set_status(
            run_name,
            "Running",
            action=action,
            message=(
                "Applying historical scope "
                "and store history..."
            ),
        )
        frappe.db.commit()

        run = frappe.get_doc(
            "DC Dispatch Run",
            run_name,
        )
        result = (
            cache_service.history_result_from_cache(
                run
            )
        )
        cache_service.apply_history_result(
            run,
            result,
        )

        _set_status(
            run_name,
            "Completed",
            action=action,
            message=(
                "Store history analysis completed."
            ),
            result=result,
            completed=True,
        )
        frappe.db.commit()

    except Exception:
        trace = frappe.get_traceback()
        frappe.db.rollback()

        _set_status(
            run_name,
            "Failed",
            action=action,
            message=(
                "Store history analysis failed. "
                "Check Error Log."
            ),
            result={
                "error": (
                    "Background job failed."
                )
            },
            completed=True,
        )
        frappe.log_error(
            trace,
            (
                "DC Dispatch "
                "Background History"
            ),
        )
        frappe.db.commit()


def run_proposal_calculation_job(
    run_name,
):
    action = "Calculate Proposal"

    try:
        _set_status(
            run_name,
            "Running",
            action=action,
            message=(
                "Calculating dispatch proposal "
                "from cached history..."
            ),
        )
        frappe.db.commit()

        from dc_dispatch.services.proposal_service_v062 import (
            calculate_proposal_from_cache,
        )

        result = (
            calculate_proposal_from_cache(
                run_name
            )
        )

        _set_status(
            run_name,
            "Completed",
            action=action,
            message=(
                "Dispatch proposal completed."
            ),
            result=result,
            completed=True,
        )
        frappe.db.commit()

    except Exception:
        trace = frappe.get_traceback()
        frappe.db.rollback()

        _set_status(
            run_name,
            "Failed",
            action=action,
            message=(
                "Proposal calculation failed. "
                "Check Error Log."
            ),
            result={
                "error": (
                    "Background job failed."
                )
            },
            completed=True,
        )
        frappe.log_error(
            trace,
            (
                "DC Dispatch "
                "Background Proposal"
            ),
        )
        frappe.db.commit()
