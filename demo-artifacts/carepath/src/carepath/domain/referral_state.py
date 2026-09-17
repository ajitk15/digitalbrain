"""The referral lifecycle. FR-08.

A referral moves through a fixed set of states and a fixed set of transitions.
Keeping the map here rather than in the request handler means the rule is the
same whichever way the transition is requested, and it is testable without a
web server.
"""

from enum import StrEnum


class ReferralStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    DECLINED = "declined"
    CANCELLED = "cancelled"


#: Which states each state may move to. Terminal states map to an empty set.
TRANSITIONS: dict[ReferralStatus, set[ReferralStatus]] = {
    ReferralStatus.DRAFT: {ReferralStatus.SUBMITTED, ReferralStatus.CANCELLED},
    ReferralStatus.SUBMITTED: {
        ReferralStatus.ACCEPTED,
        ReferralStatus.DECLINED,
        ReferralStatus.CANCELLED,
    },
    ReferralStatus.ACCEPTED: {ReferralStatus.SCHEDULED, ReferralStatus.CANCELLED},
    ReferralStatus.SCHEDULED: {ReferralStatus.COMPLETED, ReferralStatus.CANCELLED},
    ReferralStatus.COMPLETED: set(),
    ReferralStatus.DECLINED: set(),
    ReferralStatus.CANCELLED: set(),
}


def can_move(current: ReferralStatus, proposed: ReferralStatus) -> bool:
    """True when `proposed` is a transition the lifecycle allows from `current`."""
    return proposed in TRANSITIONS[current]
