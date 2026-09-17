"""Referral lifecycle. FR-07, FR-08."""

import pytest

from carepath.domain.referral_state import TRANSITIONS, ReferralStatus, can_move
from carepath.errors import Conflict, NotFound
from carepath.schemas import ReferralIn
from carepath.services import referrals

from .conftest import CLINICIAN, PARTNER

A_REFERRAL = ReferralIn(
    specialty="Cardiology", reason="Persistent tachycardia after discharge", urgency="urgent"
)


class TestLifecycleMap:
    def test_every_status_appears_in_the_map(self):
        assert set(TRANSITIONS) == set(ReferralStatus)

    def test_a_completed_referral_is_terminal(self):
        assert TRANSITIONS[ReferralStatus.COMPLETED] == set()

    def test_a_declined_referral_is_terminal(self):
        assert TRANSITIONS[ReferralStatus.DECLINED] == set()

    @pytest.mark.parametrize(
        ("current", "proposed"),
        [
            (ReferralStatus.DRAFT, ReferralStatus.SUBMITTED),
            (ReferralStatus.SUBMITTED, ReferralStatus.ACCEPTED),
            (ReferralStatus.ACCEPTED, ReferralStatus.SCHEDULED),
            (ReferralStatus.SCHEDULED, ReferralStatus.COMPLETED),
        ],
    )
    def test_the_expected_path_is_allowed(self, current, proposed):
        assert can_move(current, proposed)

    @pytest.mark.parametrize(
        ("current", "proposed"),
        [
            (ReferralStatus.DRAFT, ReferralStatus.COMPLETED),
            (ReferralStatus.SUBMITTED, ReferralStatus.SCHEDULED),
            (ReferralStatus.COMPLETED, ReferralStatus.DRAFT),
            (ReferralStatus.CANCELLED, ReferralStatus.SUBMITTED),
        ],
    )
    def test_a_shortcut_is_refused(self, current, proposed):
        assert not can_move(current, proposed)


class TestCreating:
    def test_a_new_referral_starts_as_a_draft(self, connection, principal, patient):
        created = referrals.create(connection, principal, patient["id"], A_REFERRAL)
        assert created["status"] == ReferralStatus.DRAFT
        assert created["created_by"] == principal.subject

    def test_a_referral_for_an_unknown_patient_is_not_found(self, connection, principal):
        with pytest.raises(NotFound):
            referrals.create(connection, principal, "no-such-patient", A_REFERRAL)

    def test_referrals_are_listed_newest_first_and_bounded(self, connection, principal, patient):
        for _ in range(4):
            referrals.create(connection, principal, patient["id"], A_REFERRAL)
        assert len(referrals.for_patient(connection, patient["id"], 2)) == 2


class TestTransitions:
    def test_a_permitted_move_is_applied(self, connection, principal, patient):
        created = referrals.create(connection, principal, patient["id"], A_REFERRAL)
        moved = referrals.transition(
            connection, principal, created["id"], ReferralStatus.SUBMITTED
        )
        assert moved["status"] == ReferralStatus.SUBMITTED

    def test_a_forbidden_move_is_a_conflict(self, connection, principal, patient):
        created = referrals.create(connection, principal, patient["id"], A_REFERRAL)
        with pytest.raises(Conflict):
            referrals.transition(connection, principal, created["id"], ReferralStatus.COMPLETED)

    def test_a_move_on_an_unknown_referral_is_not_found(self, connection, principal):
        with pytest.raises(NotFound):
            referrals.transition(connection, principal, "nope", ReferralStatus.SUBMITTED)

    def test_the_whole_happy_path_can_be_walked(self, connection, principal, patient):
        created = referrals.create(connection, principal, patient["id"], A_REFERRAL)
        for status in (
            ReferralStatus.SUBMITTED,
            ReferralStatus.ACCEPTED,
            ReferralStatus.SCHEDULED,
            ReferralStatus.COMPLETED,
        ):
            moved = referrals.transition(connection, principal, created["id"], status)
        assert moved["status"] == ReferralStatus.COMPLETED


class TestApi:
    def test_a_referral_is_created_and_moved(self, client, patient):
        created = client.post(
            f"/patients/{patient['id']}/referrals",
            headers=CLINICIAN,
            json={"specialty": "Cardiology", "reason": "Follow-up", "urgency": "routine"},
        )
        assert created.status_code == 201
        identifier = created.json()["id"]
        moved = client.post(
            f"/referrals/{identifier}/status", headers=CLINICIAN, json={"status": "submitted"}
        )
        assert moved.status_code == 200
        assert moved.json()["status"] == "submitted"

    def test_a_forbidden_move_is_a_409(self, client, patient):
        created = client.post(
            f"/patients/{patient['id']}/referrals",
            headers=CLINICIAN,
            json={"specialty": "Cardiology", "reason": "Follow-up"},
        )
        response = client.post(
            f"/referrals/{created.json()['id']}/status",
            headers=CLINICIAN,
            json={"status": "completed"},
        )
        assert response.status_code == 409

    def test_a_partner_may_not_raise_a_referral(self, client, patient):
        response = client.post(
            f"/patients/{patient['id']}/referrals",
            headers=PARTNER,
            json={"specialty": "Cardiology", "reason": "Follow-up"},
        )
        assert response.status_code == 403
