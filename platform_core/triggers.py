"""What a change asks the background lanes to do.

A graph is built from an application's answerable sources and its graph
generation settings (`graphs.fingerprint`), and only while Knowledge is
switched on. Anything that writes one of those asks for a `graph_refresh` job
here, in the writer's own transaction (`jobs.enqueue`). The handler still
compares fingerprints, so a request that changed nothing costs one query.

Saves and deletes arrive as signals. Queryset `update` and `delete` send none,
so `KnowledgeEntryQuerySet` reports those itself. What neither covers - a bulk
`update` of a feature switch, an organization reactivated - the graph lane's
reconcile pass finds within `graphs.RECONCILE_INTERVAL`.
"""

from django.db.models.signals import post_delete, post_save

from .models import GRAPH_FIELDS, AIConfiguration, Application, ApplicationFeature, KnowledgeEntry


def sources_changed(application_ids):
    from .graphs import request_refresh

    for app_id in application_ids:
        if app_id is not None:
            request_refresh(app_id)


def entry_saved(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    if update_fields is not None and GRAPH_FIELDS.isdisjoint(update_fields):
        return
    sources_changed([instance.application_id])


def entry_deleted(sender, instance, **kwargs):
    sources_changed([instance.application_id])


def graph_setting_changed(sender, instance, raw=False, **kwargs):
    if raw or instance.purpose != "graph_generation":
        return
    sources_changed([instance.application_id])


def feature_changed(sender, instance, raw=False, **kwargs):
    if raw or instance.key != "knowledge":
        return
    sources_changed([instance.application_id])


def application_saved(sender, instance, raw=False, created=False, **kwargs):
    # A new application has nothing to build from yet.
    if raw or created:
        return
    sources_changed([instance.pk])


def connect():
    """Called once from `PlatformCoreConfig.ready`."""
    uid = "platform_core.triggers"
    post_save.connect(entry_saved, sender=KnowledgeEntry, dispatch_uid=f"{uid}.entry_saved")
    post_delete.connect(entry_deleted, sender=KnowledgeEntry, dispatch_uid=f"{uid}.entry_deleted")
    for signal, name in ((post_save, "saved"), (post_delete, "deleted")):
        signal.connect(
            graph_setting_changed, sender=AIConfiguration, dispatch_uid=f"{uid}.ai_{name}"
        )
        signal.connect(
            feature_changed, sender=ApplicationFeature, dispatch_uid=f"{uid}.feature_{name}"
        )
    post_save.connect(application_saved, sender=Application, dispatch_uid=f"{uid}.application")
