"""The platform's own machinery: stream leases, lane heartbeats, jobs."""

from django.db import models


class StreamLease(models.Model):
    """A chat answer being streamed, visible to every server process.

    Only written when `settings.SHARED_STREAMS` is on. The process streaming the
    answer renews `heartbeat_at`; any process may set `stop_requested`, which
    the streaming process polls. A lease whose heartbeat has gone quiet belongs
    to a process that died, and is treated as gone.
    """

    message = models.OneToOneField("ChatMessage", primary_key=True, on_delete=models.CASCADE)
    owner = models.CharField(max_length=64)
    heartbeat_at = models.DateTimeField()
    stop_requested = models.BooleanField(default=False)


class LaneHeartbeat(models.Model):
    """Which process is running a worker lane, and when it last said so.

    Written by `document_worker.LaneKeeper` while it holds the lane. Once the
    worker can run apart from the web process, nothing else tells anyone that
    a lane has died: a quiet heartbeat is how `queue_status` says so.
    """

    lane = models.CharField(max_length=32, primary_key=True)
    owner = models.CharField(max_length=80)
    started_at = models.DateTimeField()
    beat_at = models.DateTimeField()


class Job(models.Model):
    """One piece of background work somebody's change asked for (`jobs.py`).

    The graph and connector lanes used to find their work by scanning: every
    application's fingerprint every two seconds, every connector every ten. A
    change now writes a row here in the same transaction as the change itself,
    so the row exists exactly when the change does - that is the outbox, and
    there is no second table for it.

    `key` names what the work is about. At most one row per key is waiting, so
    a burst of changes is one job; a running job does not count, so a change
    made while it runs is picked up after it. Nothing here records who asked:
    the handler decides whose run it is from the thing itself - the connector's
    creator, the person who switched on automatic refresh - and re-checks them.
    """

    STATUSES = [
        ("queued", "Queued"),
        ("running", "Running"),
        ("done", "Done"),
        ("dead", "Dead"),
    ]

    kind = models.CharField(max_length=40)
    lane = models.CharField(max_length=32)
    key = models.CharField(max_length=200)
    #: Not a foreign key, deliberately: deleting an application deletes its
    #: sources, each deletion asks for a refresh, and a job referencing the row
    #: being deleted would fail the whole delete at commit. A job whose
    #: application has gone finds nothing to do and ages out.
    application_id = models.UUIDField(null=True, blank=True)
    subject = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="queued")
    run_after = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=1)
    lease_owner = models.CharField(max_length=80, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_type = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key"], condition=models.Q(status="queued"), name="job_one_queued_per_key"
            )
        ]
        indexes = [
            models.Index(fields=["lane", "status", "run_after"]),
            models.Index(fields=["status", "lease_expires_at"]),
            models.Index(fields=["status", "finished_at"]),
        ]
