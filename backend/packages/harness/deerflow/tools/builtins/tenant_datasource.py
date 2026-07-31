"""Resolve SaaS tenant data sources from a configurable registry."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from langchain_community.utilities import SQLDatabase

from deerflow.runtime.tenant_context import TenantContext

_MYSQL_JDBC_PREFIX = "jdbc:"
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


class TenantDataSourceError(RuntimeError):
    """Raised when a SaaS tenant data source cannot be resolved safely."""


@dataclass(frozen=True)
class TenantDataSource:
    code: str
    host: str
    port: int
    database: str
    username: str
    password: str
    driver_class: str
    allowed_databases: tuple[str, ...]


def build_database_code(ctx: TenantContext) -> str:
    """Build the SaaS dynamic datasource code for a tenant and system."""
    if not _SAFE_IDENTIFIER_RE.fullmatch(ctx.system_code):
        raise TenantDataSourceError(f"Invalid system_code: {ctx.system_code!r}")
    if not _SAFE_IDENTIFIER_RE.fullmatch(ctx.tenant_code):
        raise TenantDataSourceError(f"Invalid tenant_code: {ctx.tenant_code!r}")
    return f"semantic_{ctx.system_code}_{ctx.tenant_code}"


def decrypt_password(value: str) -> str:
    """Hook for deployments that encrypt registry passwords at rest."""
    return value


def _config_db_uri() -> str:
    host = os.getenv("SAAS_CONFIG_DB_HOST")
    user = os.getenv("SAAS_CONFIG_DB_USER")
    password = os.getenv("SAAS_CONFIG_DB_PASSWORD", "")
    database = os.getenv("SAAS_CONFIG_DB_DATABASE")
    port = os.getenv("SAAS_CONFIG_DB_PORT", "3306")
    missing = [
        name
        for name, value in {
            "SAAS_CONFIG_DB_HOST": host,
            "SAAS_CONFIG_DB_USER": user,
            "SAAS_CONFIG_DB_DATABASE": database,
        }.items()
        if not value
    ]
    if missing:
        raise TenantDataSourceError(f"Missing SaaS config database environment variables: {', '.join(missing)}")
    return f"mysql+mysqlconnector://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"


def _registry_identifier(env_name: str, default: str) -> str:
    value = os.getenv(env_name, default).strip()
    if not _SAFE_IDENTIFIER_RE.fullmatch(value):
        raise TenantDataSourceError(f"Invalid datasource registry identifier in {env_name}")
    return value


def _query_datasource_registry(database_code: str) -> dict[str, str]:
    table = _registry_identifier("SAAS_CONFIG_DB_TABLE", "tenant_datasources")
    code_column = _registry_identifier("SAAS_CONFIG_DB_CODE_COLUMN", "code")
    url_column = _registry_identifier("SAAS_CONFIG_DB_URL_COLUMN", "url")
    username_column = _registry_identifier("SAAS_CONFIG_DB_USERNAME_COLUMN", "username")
    password_column = _registry_identifier("SAAS_CONFIG_DB_PASSWORD_COLUMN", "password")
    driver_column = _registry_identifier("SAAS_CONFIG_DB_DRIVER_COLUMN", "driver_class")
    db = SQLDatabase.from_uri(_config_db_uri(), sample_rows_in_table_info=0)
    result = db.run(
        f"SELECT {code_column}, {url_column}, {username_column}, {password_column}, {driver_column} FROM {table} WHERE {code_column} = :database_code LIMIT 1",
        parameters={"database_code": database_code},
        execution_options={"timeout": 10},
    )
    rows = _parse_sql_database_result(result)
    if not rows:
        raise TenantDataSourceError(f"No datasource found in the configured registry for code {database_code!r}")
    row = rows[0]
    return {
        "code": str(row[0] or ""),
        "url": str(row[1] or ""),
        "user_name": str(row[2] or ""),
        "password": str(row[3] or ""),
        "driver_class": str(row[4] or ""),
    }


def _parse_sql_database_result(result: object) -> list[tuple]:
    if not result:
        return []
    if isinstance(result, list):
        return [tuple(row) if isinstance(row, (list, tuple)) else (row,) for row in result]
    if isinstance(result, tuple):
        return [result]
    if isinstance(result, str):
        import ast

        try:
            parsed = ast.literal_eval(result)
        except (SyntaxError, ValueError):
            return []
        if isinstance(parsed, list):
            return [tuple(row) if isinstance(row, (list, tuple)) else (row,) for row in parsed]
        if isinstance(parsed, tuple):
            if parsed and isinstance(parsed[0], (list, tuple)):
                return [tuple(row) for row in parsed]
            return [parsed]
    return []


def _parse_jdbc_mysql_url(url: str) -> tuple[str, int, str]:
    raw = url.strip()
    if raw.startswith(_MYSQL_JDBC_PREFIX):
        raw = raw[len(_MYSQL_JDBC_PREFIX) :]
    parsed = urlparse(raw)
    if parsed.scheme not in {"mysql", "mysql+mysqlconnector"}:
        raise TenantDataSourceError(f"Unsupported datasource URL scheme: {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise TenantDataSourceError("Datasource URL must not embed credentials")
    host = parsed.hostname
    if not host:
        raise TenantDataSourceError("Datasource URL is missing host")
    database = unquote(parsed.path.lstrip("/")).split("/", 1)[0]
    if not database:
        query_db = parse_qs(parsed.query).get("database", [""])[0]
        database = query_db.strip()
    if not database:
        raise TenantDataSourceError("Datasource URL is missing database")
    if not _SAFE_IDENTIFIER_RE.fullmatch(database):
        raise TenantDataSourceError("Datasource URL contains an unsafe database name")
    return host, parsed.port or 3306, database


@lru_cache(maxsize=256)
def _resolve_by_code(database_code: str) -> TenantDataSource:
    row = _query_datasource_registry(database_code)
    if row["code"] != database_code:
        raise TenantDataSourceError(f"Datasource code mismatch: expected {database_code!r}, got {row['code']!r}")
    host, port, database = _parse_jdbc_mysql_url(row["url"])
    return TenantDataSource(
        code=row["code"],
        host=host,
        port=port,
        database=database,
        username=row["user_name"],
        password=decrypt_password(row["password"]),
        driver_class=row["driver_class"],
        allowed_databases=(database,),
    )


def resolve_tenant_datasource(ctx: TenantContext) -> TenantDataSource:
    """Resolve the current SaaS tenant datasource from the configured registry."""
    return _resolve_by_code(build_database_code(ctx))


def build_tenant_mysql_uri(ds: TenantDataSource, database: str | None = None) -> str:
    db = database or ds.database
    if db not in ds.allowed_databases:
        raise TenantDataSourceError(f"Database {db!r} is not allowed for datasource {ds.code!r}")
    return f"mysql+mysqlconnector://{quote_plus(ds.username)}:{quote_plus(ds.password)}@{ds.host}:{ds.port}/{db}"
