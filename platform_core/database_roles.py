"""The role the platform runs as is not the role that owns its tables.

Out of the box the application connected as the role PostgreSQL's image creates,
which is a superuser: a SQL injection or a mistaken query there could do
anything the server can - read any file the server can, `COPY ... TO PROGRAM`,
drop any table - and row-level security never applies to a superuser at all.

So a deployment names two roles. The **owner** (`[database] user` in the
configuration the migrate step reads) applies migrations and owns every table.
The **runtime** role (`runtime_user` there, and `user` in the configuration
everything else reads) is what serves requests, runs the lanes and answers
`manage.py`: it may read and write rows and nothing more - no DDL, no new roles,
no bypassing row security, not even membership of the owner. `ensure` creates
it and grants it exactly that, after every migration run, so a table a
migration adds is granted in the same deploy.

Its password is the mounted file `database_runtime_password`, like every
secret. It is sent to the server as a SCRAM verifier computed here, never as
text, so a server logging DDL statements logs no password. Replacing the file
and deploying again rotates it.
"""

import base64
import hashlib
import hmac
import os
import re

from django.db import connections

#: A role name this module will put in a statement. Quoted as an identifier as
#: well, but a name outside this shape is refused rather than quoted.
ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
#: What the runtime role is, stated in full so a role that already existed with
#: more is brought back down to this.
ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
SCRAM_ITERATIONS = 4096


def scram_verifier(password, salt=None, iterations=SCRAM_ITERATIONS):
    """The SCRAM-SHA-256 verifier PostgreSQL stores for `password` (RFC 5802/7677).

    PostgreSQL accepts a verifier in place of a password, and stores it as is:
    the password itself never reaches the server or its statement log.
    """
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(value):
        return base64.b64encode(value).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def valid(name):
    return isinstance(name, str) and ROLE_NAME.fullmatch(name) is not None


def ensure(role, password, using="default"):
    """Create or correct the runtime role and grant it row access to every table.

    Run as the owner, which needs CREATEROLE (the image's superuser has it).
    Idempotent: every run states the role's attributes, its password and its
    grants again, so drift - a privilege added by hand - is undone.
    """
    from psycopg import sql

    if not valid(role):
        raise ValueError("The runtime role must be a lower-case PostgreSQL name.")
    if not password:
        raise ValueError("The runtime role needs a password.")
    connection = connections[using]
    owner = connection.settings_dict["USER"]
    if role == owner:
        raise ValueError("The runtime role must not be the owner.")
    name = sql.Identifier(role)
    verifier = sql.Literal(scram_verifier(password))
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        verb = sql.SQL("ALTER") if cursor.fetchone() else sql.SQL("CREATE")
        cursor.execute(
            sql.SQL("{} ROLE {} WITH {} PASSWORD {}").format(
                verb, name, sql.SQL(ATTRIBUTES), verifier
            ).as_string(cursor.cursor)
        )
        database = sql.Identifier(connection.settings_dict["NAME"])
        for statement in (
            # Membership would hand it the owner's rights through SET ROLE.
            sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(owner), name),
            sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(database, name),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(name),
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(name),
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(name),
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(name),
            # Tables a later migration adds are granted as they are created,
            # so a deploy never serves a table the runtime role cannot read.
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(sql.Identifier(owner), name),
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(sql.Identifier(owner), name),
        ):
            cursor.execute(statement.as_string(cursor.cursor))
