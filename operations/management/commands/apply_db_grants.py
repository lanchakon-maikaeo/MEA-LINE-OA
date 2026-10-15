"""Runtime database role grants (M12, E.7): the web/worker role reads and writes business tables,
history tables stay append-only and the migration ledger stays read-only. Run by the migration
(owner) role after every migrate; idempotent."""

import re

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from operations.models import AppendOnlyModel

ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def statements(role):
    if not ROLE.match(role):
        raise CommandError("Role names are lower-case letters, digits and underscores.")
    q = connection.ops.quote_name
    out = [
        f"GRANT USAGE ON SCHEMA public TO {q(role)}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {q(role)}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {q(role)}",
        f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {q('django_migrations')} FROM {q(role)}",
    ]
    for model in sorted(apps.get_models(), key=lambda m: m._meta.db_table):
        if issubclass(model, AppendOnlyModel):
            table = q(model._meta.db_table)
            out.append(f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {q(role)}")
    return out


class Command(BaseCommand):
    help = "Grant the runtime role its table rights; history tables stay append-only."

    def add_arguments(self, parser):
        parser.add_argument("--role", required=True, help="Runtime (web/worker) database role")
        parser.add_argument("--print", action="store_true", help="Show the SQL, change nothing")

    def handle(self, *args, role, **options):
        sql = statements(role)
        if options["print"]:
            self.stdout.write(";\n".join(sql) + ";")
            return
        with transaction.atomic(), connection.cursor() as cursor:
            for statement in sql:
                cursor.execute(statement)
        self.stdout.write(f"Granted {role}: {len(sql)} statements.")
