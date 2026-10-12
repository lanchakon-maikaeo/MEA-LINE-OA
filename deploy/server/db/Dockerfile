# PostgreSQL 17 with pgBackRest (M12): WAL archive and backups go to OCI Object Storage from
# inside the database container; the same image runs restores and restore drills.
FROM postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 AS base
# Security updates at build time (the release digest then fixes them). The base image already
# carries the PGDG apt repository (current pgBackRest releases).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends pgbackrest ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o postgres -g postgres -m 750 /var/log/pgbackrest /var/spool/pgbackrest /var/lib/pgbackrest \
    && rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem  # unused, same in every copy
COPY pg_hba.conf /etc/postgresql/pg_hba.conf
COPY init/ /docker-entrypoint-initdb.d/
COPY faoj-entrypoint.sh /usr/local/bin/faoj-entrypoint.sh
# gosu replaced by a setpriv shim: the upstream binary's old Go runtime has unfixed CVEs.
COPY gosu /usr/local/bin/gosu

# Flattened so the replaced gosu binary does not linger in a lower layer.
FROM scratch
COPY --from=base / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/17/bin \
    LANG=en_US.utf8 \
    PG_MAJOR=17 \
    PGDATA=/var/lib/postgresql/data
STOPSIGNAL SIGINT
EXPOSE 5432
ENTRYPOINT ["faoj-entrypoint.sh"]
CMD ["postgres"]
