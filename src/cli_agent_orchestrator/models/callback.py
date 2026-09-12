"""Models and state vocabulary for durable assignment callbacks."""

from enum import Enum


class CallbackTaskState(str, Enum):
    """Durable lifecycle states for an assigned worker's callback obligation."""

    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    CALLBACK_RECEIVED = "callback_received"
    OVERDUE = "overdue"
    NOT_DELIVERABLE = "not_deliverable"
    FAILED = "failed"


ACTIVE_CALLBACK_TASK_STATES = frozenset(
    {
        CallbackTaskState.ASSIGNED.value,
        CallbackTaskState.PICKED_UP.value,
        CallbackTaskState.OVERDUE.value,
    }
)
