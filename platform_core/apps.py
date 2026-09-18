from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def tune_sqlite(connection, **kwargs):
    """Let readers and writers coexist on the local database.

    SQLite's default rollback journal gives a writer an exclusive lock on the
    whole file, so every reader waits behind it. This process runs six worker
    lanes beside the web server against one file, and a burst - twenty-seven
    documents converting while the graph lane rebuilds - produced "database is
    locked" for one or two of them every time. The conversion had already
    succeeded; it was the write that recorded it which failed, so a retry always
    worked and the file was never the problem.

    WAL lets readers carry on while a writer works, and the busy timeout makes a
    contended write wait rather than fail. Both are local-only: production is
    PostgreSQL, which has neither problem.
    """
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=20000")


class PlatformCoreConfig(AppConfig):
    name = "platform_core"
