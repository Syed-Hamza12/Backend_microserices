from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def _enable_sqlite_wal_mode(sender, connection, **kwargs):
    """WAL journal mode lets readers proceed while a write is in progress
    (the default rollback-journal mode blocks all readers during a write) —
    meaningful for SQLite under real concurrent production traffic (see
    settings.py's DJANGO_ALLOW_SQLITE_IN_PRODUCTION path). No-op for
    Postgres connections. Applied per-connection via this signal since
    Django's sqlite3 backend has no OPTIONS key for arbitrary PRAGMAs.
    """
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
