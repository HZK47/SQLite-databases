import logging
import os
import re
import sqlite3
import string
import tempfile
from contextlib import closing
from functools import cache, wraps
from math import isfinite
from pathlib import Path


# Global configuration
BASE_DIR = Path(__file__).resolve().parent
MIN_SQLITE_VERSION = (3, 37, 0)
logger = logging.getLogger(__name__)

PERMITTED_DATABASE_FIELDS = {
    "path", "version", "schema_version", "timeout",
    "foreign_keys", "journal_mode", "description", "backup_path",
}

PERMITTED_JOURNAL_MODES = {
    "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF",
}

databases = {
    "materials": {
        "database_info": {
            "path": BASE_DIR / "materials.db",
            "version": "1.0",
            "schema_version": 3,
            "timeout": 10.0,
            "foreign_keys": True,
            "journal_mode": "WAL",
            "description": "Database for storing materials",
            "backup_path": BASE_DIR / "backups" / "materials.db",
        },
        "tables": {
            "school": {
                "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
                "name": "TEXT NOT NULL",
                "description": "TEXT",
                "quantity": "INTEGER NOT NULL CHECK(quantity >= 0)",
                "price": "REAL NOT NULL CHECK(price >= 0)",
            },
        },
    },
}

query_builders = {}


# Initialization checks
@cache
def _check_sqlite_version():
    """
    Check whether the SQLite library supports STRICT tables.

    Uses the SQLite version loaded by Python, not the interpreter version.
    A successful check is cached for the lifetime of the process.

    Returns:
        None: If SQLite meets the minimum requirement.

    Raises:
        RuntimeError: If SQLite is older than the required version.

    Usage:
        _check_sqlite_version()
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        required = ".".join(map(str, MIN_SQLITE_VERSION))
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is unsupported. Version {required} or newer is required."
        )


# Decorators
def _handle_errors(func):
    """
    Log SQLite exceptions while preserving the original error and result.

    Other exceptions, such as TypeError and ValueError, are propagated
    without being logged by this decorator.

    Args:
        func (callable): Function whose SQLite errors should be logged.

    Returns:
        callable: A wrapped function with the same return value as func.

    Raises:
        sqlite3.Error: Re-raises SQLite errors after logging their traceback.

    Usage:
        @_handle_errors
        def run_database_operation():
            ...
    """
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.Error:
            logger.exception("SQLite error in function '%s'", func.__name__)
            raise

    return wrapped


def _register_query(action):
    """
    Register a query-building function for a specific SQL action.

    Stores the function in query_builders without executing it.
    Each uppercase SQL action can be registered only once.

    Args:
        action (str): SQL action using uppercase letters A-Z.

    Returns:
        callable: Decorator that registers and returns the original function.

    Raises:
        TypeError: If action is not a string or func is not callable.
        ValueError: If action is invalid or already registered.

    Usage:
        @_register_query("SELECT")
        def _build_select(database, table):
            ...
    """
    if not isinstance(action, str):
        raise TypeError(f"Query action must be a string, received {type(action).__name__}.")
    if not action or any(char not in string.ascii_uppercase for char in action):
        raise ValueError(f"Invalid query action {action!r}. Use uppercase letters A-Z only.")

    def decorator(func):
        if not callable(func):
            raise TypeError(f"Query builder for action '{action}' must be callable.")
        if action in query_builders:
            raise ValueError(f"Query action '{action}' is already registered.")

        query_builders[action] = func
        return func

    return decorator


# Schema and configuration validation
def _check_columns(database, table=None, columns=()):
    """
    Validate registered database, table, and column definitions.

    Checks the Python configuration dictionary. Does not inspect
    the physical structure stored in the SQLite database.

    Args:
        database (str): Name of the registered database.
        table (str | None): Table to check, or None for database-only validation.
        columns (str | iterable | None): Column names to validate.

    Returns:
        None: If the requested definitions are valid.

    Raises:
        TypeError: If a name or definition has an invalid type.
        ValueError: If a database, table, or column is unknown or empty.

    Usage:
        _check_columns("materials")
        _check_columns("materials", "school", ["name", "price"])
    """
    if not isinstance(database, str):
        raise TypeError(f"Database name must be a string, received {type(database).__name__}.")
    if database not in databases:
        raise ValueError(f"Unknown database '{database}'. Available databases: {sorted(databases)}.")
    if not isinstance(databases[database], dict):
        raise TypeError(f"Configuration for database '{database}' must be a dictionary.")
    if table is None:
        return
    if not isinstance(table, str):
        raise TypeError(f"Table name must be a string, received {type(table).__name__}.")

    tables = databases[database].get("tables")

    if not isinstance(tables, dict):
        raise TypeError(f"Table definitions for database '{database}' must be a dictionary.")
    if table not in tables:
        raise ValueError(f"Unknown table '{table}' in database '{database}'. Available tables: {sorted(tables)}.")

    column_definitions = tables[table]

    if not isinstance(column_definitions, dict):
        raise TypeError(f"Column definitions for table '{table}' must be a dictionary.")
    if not column_definitions:
        raise ValueError(f"Table '{table}' must contain at least one column.")

    for name, definition in column_definitions.items():
        _quote_identifier(name)

        if not isinstance(definition, str):
            raise TypeError(f"Definition of column '{name}' must be a string.")
        if not definition.strip():
            raise ValueError(f"Definition of column '{name}' cannot be empty.")

    if columns is None:
        return
    if isinstance(columns, str):
        columns = (columns,)

    try:
        columns = iter(columns)
    except TypeError as error:
        raise TypeError("Columns must be a string, iterable of names, or None.") from error

    for column in columns:
        if not isinstance(column, str):
            raise TypeError(f"Column name must be a string, received {type(column).__name__}.")
        if column not in column_definitions:
            raise ValueError(
                f"Unknown column '{column}' in table '{table}'. "
                f"Available columns: {sorted(column_definitions)}."
            )


def _quote_identifier(name):
    """
    Quote a SQL identifier without changing its contents.

    Escapes internal double quotes and preserves surrounding spaces.

    Args:
        name (str): Table, column, or index name.

    Returns:
        str: Identifier enclosed in double quotes.

    Raises:
        TypeError: If name is not a string.
        ValueError: If name is empty or contains only whitespace.

    Usage:
        _quote_identifier("school")
        _quote_identifier('a"b')
    """
    if not isinstance(name, str):
        raise TypeError(f"SQL identifier must be a string, received {type(name).__name__}.")
    if not name.strip():
        raise ValueError("SQL identifier cannot be empty or contain only whitespace.")

    return '"' + name.replace('"', '""') + '"'


def _validate_config(database):
    """
    Validate the configuration of a registered SQLite database.

    Checks required fields, types, and allowed values without opening
    the database or modifying its files.

    Args:
        database (str): Registered database name.

    Returns:
        None: If all supported settings are valid.

    Raises:
        TypeError: If a configuration field has an invalid type.
        ValueError: If a field is missing or contains an invalid value.

    Usage:
        _validate_config("materials")
    """
    _check_columns(database)
    database_info = databases[database].get("database_info")

    if not isinstance(database_info, dict):
        raise TypeError("'database_info' must be a dictionary.")

    missing = PERMITTED_DATABASE_FIELDS - database_info.keys()
    unknown = database_info.keys() - PERMITTED_DATABASE_FIELDS

    if missing or unknown:
        raise ValueError(
            f"Invalid database configuration. Missing fields: {sorted(missing)}. "
            f"Unknown fields: {sorted(unknown)}."
        )

    path = database_info["path"]

    if not isinstance(path, Path):
        raise TypeError(f"'path' must be a Path object, received {type(path).__name__}.")

    version = database_info["version"]

    if not isinstance(version, str):
        raise TypeError(f"'version' must be a string, received {type(version).__name__}.")
    if not all(part.isascii() and part.isdigit() for part in version.split(".")):
        raise ValueError(f"Invalid version '{version}'. Use numeric components separated by dots.")

    schema_version = database_info["schema_version"]

    if type(schema_version) is not int:
        raise TypeError(f"'schema_version' must be an integer, received {type(schema_version).__name__}.")
    if schema_version < 0:
        raise ValueError(f"'schema_version' cannot be negative, received {schema_version}.")

    timeout = database_info["timeout"]

    if type(timeout) not in (int, float):
        raise TypeError(f"'timeout' must be an integer or float, received {type(timeout).__name__}.")
    if not isfinite(timeout) or timeout < 0:
        raise ValueError(f"'timeout' must be finite and non-negative, received {timeout}.")

    foreign_keys = database_info["foreign_keys"]

    if type(foreign_keys) is not bool:
        raise TypeError(f"'foreign_keys' must be a boolean, received {type(foreign_keys).__name__}.")

    journal_mode = database_info["journal_mode"]

    if not isinstance(journal_mode, str):
        raise TypeError(f"'journal_mode' must be a string, received {type(journal_mode).__name__}.")
    if journal_mode not in PERMITTED_JOURNAL_MODES:
        raise ValueError(f"Invalid journal mode '{journal_mode}'. Allowed modes: {sorted(PERMITTED_JOURNAL_MODES)}.")

    description = database_info["description"]

    if not isinstance(description, str):
        raise TypeError(f"'description' must be a string, received {type(description).__name__}.")
    if not description or description.isspace():
        raise ValueError("'description' cannot be empty or contain only whitespace.")

    backup_path = database_info["backup_path"]

    if not isinstance(backup_path, Path):
        raise TypeError(f"'backup_path' must be a Path object, received {type(backup_path).__name__}.")
    if path.resolve() == backup_path.resolve():
        raise ValueError("'backup_path' cannot point to the main database file.")


def _where(database, table, filters=None):
    """
    Build a parameterized WHERE clause joined by AND conditions.

    Supports eq, ne, gt, gte, lt, lte, like, in, and is_null.
    Direct values use equality, and SQL values use placeholders.

    Args:
        database (str): Registered database name.
        table (str): Registered table name.
        filters (dict | None): Column names mapped to filter conditions.

    Returns:
        tuple[str, tuple]: WHERE clause and parameters.
            Returns ('', ()) when no filters are supplied.

    Raises:
        TypeError: If filters or an operator value has an invalid type.
        ValueError: If a column or operator is invalid.

    Usage:
        _where("materials", "school", {"price": {"gte": 10}})
        _where("materials", "school", {"quantity": {"in": [5, 10]}})
    """
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise TypeError(f"Filters must be a dictionary or None, received {type(filters).__name__}.")

    _check_columns(database, table, filters)

    operators = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "LIKE"}
    conditions = []
    parameters = []

    for column, condition in filters.items():
        quoted_column = _quote_identifier(column)

        if isinstance(condition, dict):
            if len(condition) != 1:
                raise ValueError(f"Filter for column '{column}' must contain exactly one operator.")
            operator, value = next(iter(condition.items()))
        else:
            operator, value = "eq", condition

        if not isinstance(operator, str):
            raise TypeError(f"Filter operator for column '{column}' must be a string.")
        if operator not in (*operators, "in", "is_null"):
            raise ValueError(f"Unknown filter operator '{operator}' for column '{column}'.")

        if operator == "is_null":
            if type(value) is not bool:
                raise TypeError(f"'is_null' filter for column '{column}' requires a boolean.")
            conditions.append(f"{quoted_column} IS {'NULL' if value else 'NOT NULL'}")

        elif operator == "in":
            if not isinstance(value, (list, tuple)):
                raise TypeError(f"'in' filter for column '{column}' requires a list or tuple.")
            if not value:
                raise ValueError(f"'in' filter for column '{column}' cannot be empty.")
            if any(item is None for item in value):
                raise ValueError(f"'in' filter for column '{column}' cannot contain None; use is_null.")

            placeholders = ", ".join("?" for _ in value)
            conditions.append(f"{quoted_column} IN ({placeholders})")
            parameters.extend(value)

        elif value is None:
            if operator not in ("eq", "ne"):
                raise ValueError(f"Operator '{operator}' cannot compare column '{column}' with None.")
            conditions.append(f"{quoted_column} IS {'NULL' if operator == 'eq' else 'NOT NULL'}")

        else:
            conditions.append(f"{quoted_column} {operators[operator]} ?")
            parameters.append(value)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    return where_clause, tuple(parameters)


def _validate_write_filters(database, table, filters, allow_all, require_unique):
    """
    Validate WHERE conditions and safety options for UPDATE and DELETE.

    Unfiltered operations require explicit authorization. The physical
    uniqueness check is performed separately by _require_unique_filter().

    Args:
        database (str): Registered database name.
        table (str): Table being modified.
        filters (dict | None): Conditions selecting records.
        allow_all (bool): Allow modifications without filters.
        require_unique (bool): Require a unique equality condition.

    Returns:
        tuple[str, tuple]: Validated WHERE clause and parameters.

    Raises:
        TypeError: If a safety option or filter has an invalid type.
        ValueError: If an unfiltered operation is not authorized.

    Usage:
        _validate_write_filters("materials", "school", {"id": 1}, False, True)
    """
    if type(allow_all) is not bool:
        raise TypeError("'allow_all' must be a boolean.")
    if type(require_unique) is not bool:
        raise TypeError("'require_unique' must be a boolean.")

    where_clause, parameters = _where(database, table, filters)

    if not where_clause and not allow_all:
        raise ValueError(f"Unfiltered modifications to table '{table}' require allow_all=True.")
    if not where_clause and require_unique:
        raise ValueError("Set require_unique=False to allow modifying all records.")

    return where_clause, parameters


# Connection management
class _ConnectionManager:
    """
    Open, configure, and close registered SQLite connections.

    Supports read-write and read-only connections. Successful transactions
    are committed, failed transactions are rolled back, and connections
    are always closed when leaving the context manager.

    Args:
        database (str): Registered database name.
        read_only (bool): Whether the database should be opened read-only.

    Usage:
        with _ConnectionManager("materials") as connection:
            connection.execute("SELECT 1")

        with _ConnectionManager("materials", read_only=True) as connection:
            connection.execute("SELECT 1")
    """

    def __init__(self, database, read_only=False):
        if type(read_only) is not bool:
            raise TypeError("'read_only' must be a boolean.")

        self.database = database
        self.read_only = read_only
        self._connection = None

    @_handle_errors
    def __enter__(self):
        """
        Open and configure the database connection.

        Returns:
            sqlite3.Connection: An open, configured connection.

        Raises:
            RuntimeError: If SQLite is outdated or the manager is already open.
            ValueError: If the database configuration is invalid.
            sqlite3.Error: If SQLite cannot open or configure the database.
            OSError: If a required directory cannot be created.
        """
        if self._connection is not None:
            raise RuntimeError("Connection manager is already open.")

        _check_sqlite_version()
        _validate_config(self.database)

        database_info = databases[self.database]["database_info"]
        database_path = database_info["path"]

        if self.read_only:
            path = database_path.resolve().as_uri() + "?mode=ro"
        else:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            path = database_path


        connection = sqlite3.connect(path, timeout=database_info["timeout"], uri=self.read_only)

        try:
            foreign_keys = "ON" if database_info["foreign_keys"] else "OFF"

            with closing(connection.execute(f"PRAGMA foreign_keys = {foreign_keys}")):
                pass

            with closing(connection.execute("PRAGMA foreign_keys")) as cursor:
                result = cursor.fetchone()

            if result is None or bool(result[0]) != database_info["foreign_keys"]:
                raise sqlite3.DatabaseError(
                    f"Could not configure foreign keys for database '{self.database}'. "
                    f"Expected: {foreign_keys}."
                )

        except Exception:
            connection.close()
            raise

        self._connection = connection
        return connection

    @_handle_errors
    def __exit__(self, exc_type, exc_value, traceback):
        """
        Finalize the transaction and close the SQLite connection.

        Args:
            exc_type: Exception type, if present.
            exc_value: Exception instance, if present.
            traceback: Exception traceback, if present.

        Returns:
            bool | None: Context manager result; exceptions are not suppressed.
        """
        try:
            return self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self._connection.close()
            self._connection = None


# Database initialization
@_handle_errors
def initialize_database(database):
    """
    Initialize a registered database and validate its table schemas.

    Applies journal settings, creates missing STRICT tables, and verifies
    their structures before committing. Existing tables are not migrated.

    Args:
        database (str): Registered database name.

    Returns:
        None: If initialization succeeds.

    Raises:
        TypeError: If the database configuration contains an invalid type.
        ValueError: If a configuration or table schema is incompatible.
        sqlite3.Error: If SQLite cannot initialize the database.

    Usage:
        initialize_database("materials")
    """
    _validate_config(database)

    table_definitions = databases[database].get("tables")

    if not isinstance(table_definitions, dict):
        raise TypeError(f"Tables for database '{database}' must be a dictionary.")

    for table in table_definitions:
        _check_columns(database, table)

    database_info = databases[database]["database_info"]
    journal_mode = database_info["journal_mode"]

    with _ConnectionManager(database) as connection:
        with closing(connection.execute(f"PRAGMA journal_mode = {journal_mode}")) as cursor:
            result = cursor.fetchone()

        actual_mode = result[0].upper() if result else None

        if actual_mode != journal_mode:
            raise sqlite3.DatabaseError(
                f"Could not set journal mode '{journal_mode}' for database '{database}'. "
                f"SQLite reported {actual_mode!r}."
            )


        connection.execute("BEGIN IMMEDIATE").close()

        for table in table_definitions:
            query, parameters = query_builders["CREATE"](database, table)
            connection.execute(query, parameters).close()
            _require_matching_schema(connection, database, table)


# SQLite schema inspection
@_handle_errors
def _table_exists(connection, table):
    """
    Check whether a table exists in the physical SQLite database.

    Queries sqlite_schema instead of the registered Python configuration.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Name of the table to find.

    Returns:
        bool: True if the table exists, otherwise False.

    Raises:
        sqlite3.Error: If SQLite cannot execute the query.

    Usage:
        _table_exists(connection, "school")
    """
    query = "SELECT name FROM main.sqlite_schema WHERE type = 'table' AND name = ?"

    with closing(connection.execute(query, (table,))) as cursor:
        return cursor.fetchone() is not None


@_handle_errors
def _get_table_columns(connection, table):
    """
    Retrieve metadata for every column of a SQLite table.

    Uses PRAGMA table_xinfo, including generated and hidden columns.

    Each metadata tuple contains:
        0. cid: Column identifier.
        1. name: Column name.
        2. type: Declared data type.
        3. notnull: NOT NULL restriction.
        4. dflt_value: Default expression.
        5. pk: Primary key position.
        6. hidden: Hidden or generated column indicator.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table to inspect.

    Returns:
        dict[str, tuple]: Column names mapped to metadata tuples.

    Raises:
        ValueError: If the table does not exist.
        sqlite3.Error: If SQLite cannot retrieve the columns.

    Usage:
        columns = _get_table_columns(connection, "school")
        print(columns["price"][2])
    """
    query = f"PRAGMA main.table_xinfo({_quote_identifier(table)})"

    with closing(connection.execute(query)) as cursor:
        rows = cursor.fetchall()

    if not rows and not _table_exists(connection, table):
        raise ValueError(f"Table '{table}' does not exist in the SQLite database.")

    return {row[1]: row for row in rows}


@_handle_errors
def _get_table_sql(connection, table):
    """
    Retrieve the CREATE TABLE statement stored in sqlite_schema.

    The statement contains constraints and structural details that
    PRAGMA table_xinfo does not describe completely.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table whose SQL definition should be retrieved.

    Returns:
        str: The stored CREATE TABLE statement.

    Raises:
        ValueError: If the table does not exist.
        sqlite3.Error: If SQLite cannot retrieve the definition.

    Usage:
        sql = _get_table_sql(connection, "school")
    """
    query = "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = ?"

    with closing(connection.execute(query, (table,))) as cursor:
        result = cursor.fetchone()

    if result is None:
        raise ValueError(f"Table '{table}' does not exist in the SQLite database.")

    return result[0]


@_handle_errors
def _get_unique_indexes(connection, table):
    """
    Retrieve the names of unique indexes belonging to a SQLite table.

    Includes ordinary, composite, expression-based, and partial unique
    indexes. These do not always guarantee individual column uniqueness.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table whose indexes should be inspected.

    Returns:
        list[str]: Names of unique indexes.

    Raises:
        sqlite3.Error: If SQLite cannot retrieve index information.

    Usage:
        indexes = _get_unique_indexes(connection, "school")
    """
    query = f"PRAGMA main.index_list({_quote_identifier(table)})"

    with closing(connection.execute(query)) as cursor:
        return [row[1] for row in cursor.fetchall() if row[2] == 1]


@_handle_errors
def _get_index_columns(connection, index):
    """
    Retrieve the components of a SQLite index.

    Uses PRAGMA index_xinfo to obtain column names, expressions,
    sorting directions, collations, and index-key information.

    Args:
        connection (sqlite3.Connection): Open database connection.
        index (str): Index to inspect.

    Returns:
        list[tuple]: SQLite index metadata rows.

    Raises:
        sqlite3.Error: If SQLite cannot inspect the index.

    Usage:
        columns = _get_index_columns(connection, "idx_school_name")
    """
    query = f"PRAGMA main.index_xinfo({_quote_identifier(index)})"

    with closing(connection.execute(query)) as cursor:
        return cursor.fetchall()


@_handle_errors
def _get_unique_columns(connection, table):
    """
    Identify columns individually protected by SQLite uniqueness constraints.

    Includes single-column primary keys and non-partial UNIQUE indexes.
    Excludes composite keys, expression indexes, and partial indexes.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table to inspect.

    Returns:
        set[str]: Names of individually unique columns.

    Raises:
        ValueError: If the table does not exist.
        sqlite3.Error: If SQLite cannot inspect its constraints.

    Usage:
        unique_columns = _get_unique_columns(connection, "school")
    """
    columns = _get_table_columns(connection, table)
    primary_keys = [name for name, metadata in columns.items() if metadata[5] > 0]
    unique_columns = set(primary_keys) if len(primary_keys) == 1 else set()

    query = f"PRAGMA main.index_list({_quote_identifier(table)})"

    with closing(connection.execute(query)) as cursor:
        indexes = cursor.fetchall()

    for index in indexes:
        if index[2] != 1 or index[4] == 1:
            continue

        components = [row for row in _get_index_columns(connection, index[1]) if row[5] == 1]

        if len(components) == 1 and components[0][1] >= 0 and components[0][2] is not None:
            unique_columns.add(components[0][2])

    return unique_columns


@_handle_errors
def _require_unique_filter(connection, database, table, filters):
    """
    Require a unique equality filter for a single-record modification.

    Checks the physical uniqueness constraints and verifies that the
    complete filter currently matches no more than one record.

    Must be called inside a write transaction to prevent concurrent
    writers from changing the validated rows before the modification.

    Args:
        connection (sqlite3.Connection): Connection in an active write transaction.
        database (str): Registered database name.
        table (str): Table being modified.
        filters (dict): Conditions identifying records.

    Returns:
        None: If the filter satisfies the uniqueness requirements.

    Raises:
        ValueError: If no unique equality condition exists or multiple rows match.
        sqlite3.Error: If SQLite cannot inspect or query the table.

    Usage:
        _require_unique_filter(connection, "materials", "school", {"id": 1})
    """
    if not isinstance(filters, dict) or not filters:
        raise ValueError("A unique equality filter is required.")

    where_clause, parameters = _where(database, table, filters)
    unique_columns = _get_unique_columns(connection, table)
    has_unique_filter = False

    for column, condition in filters.items():
        if column not in unique_columns:
            continue

        if isinstance(condition, dict):
            operator, value = next(iter(condition.items()))
        else:
            operator, value = "eq", condition

        if operator == "eq" and value is not None:
            has_unique_filter = True
            break

    if not has_unique_filter:
        raise ValueError(
            f"Modification of table '{table}' requires equality on a physically unique column. "
            f"Available unique columns: {sorted(unique_columns)}."
        )

    query = f"SELECT 1 FROM {_quote_identifier(table)}{where_clause} LIMIT 2"

    with closing(connection.execute(query, parameters)) as cursor:
        if len(cursor.fetchmany(2)) > 1:
            raise ValueError(f"Filter for table '{table}' matches multiple records.")


@_handle_errors
def _get_table_foreign_keys(connection, table):
    """
    Retrieve the foreign key relationships defined by a SQLite table.

    Uses PRAGMA foreign_key_list to obtain referenced tables, columns,
    and the actions configured for updates and deletions.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table to inspect.

    Returns:
        list[tuple]: Foreign key metadata rows.

    Raises:
        sqlite3.Error: If SQLite cannot retrieve the relationships.

    Usage:
        foreign_keys = _get_table_foreign_keys(connection, "school")
    """
    query = f"PRAGMA main.foreign_key_list({_quote_identifier(table)})"

    with closing(connection.execute(query)) as cursor:
        return cursor.fetchall()


@_handle_errors
def _get_table_options(connection, table):
    """
    Retrieve structural settings for an existing SQLite table.

    Uses PRAGMA table_list to obtain the object type, column count,
    WITHOUT ROWID setting, and STRICT setting.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table to inspect.

    Returns:
        dict | None: Table options, or None if not found.

    Raises:
        sqlite3.Error: If SQLite cannot retrieve table information.

    Usage:
        options = _get_table_options(connection, "school")
    """
    _quote_identifier(table)

    with closing(connection.execute("PRAGMA main.table_list")) as cursor:
        for row in cursor:
            if row[0] == "main" and row[1] == table:
                return {
                    "name": row[1],
                    "type": row[2],
                    "column_count": row[3],
                    "without_rowid": bool(row[4]),
                    "strict": bool(row[5]),
                }

    return None


@_handle_errors
def _get_table_triggers(connection, table):
    """
    Retrieve trigger names and SQL definitions associated with a table.

    Reads sqlite_schema without executing triggers or modifying the database.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table to inspect.

    Returns:
        list[tuple]: Trigger names and SQL statements.

    Raises:
        sqlite3.Error: If SQLite cannot retrieve trigger definitions.

    Usage:
        triggers = _get_table_triggers(connection, "school")
    """
    query = "SELECT name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND tbl_name = ? ORDER BY name"

    with closing(connection.execute(query, (table,))) as cursor:
        return cursor.fetchall()


@_handle_errors
def _inspect_schema(connection, table):
    """
    Inspect the physical schema of an existing SQLite table.

    Combines column definitions, CREATE TABLE SQL, foreign keys,
    triggers, table options, and unique index components.

    Uses the supplied connection without opening another database.

    Args:
        connection (sqlite3.Connection): Open database connection.
        table (str): Table to inspect.

    Returns:
        dict: Complete available structural information.

    Raises:
        ValueError: If the table does not exist.
        sqlite3.Error: If SQLite cannot inspect its structure.

    Usage:
        schema = _inspect_schema(connection, "school")
        print(schema["columns"])
    """
    if not _table_exists(connection, table):
        raise ValueError(f"Table '{table}' does not exist in the SQLite database.")

    columns = _get_table_columns(connection, table)
    sql = _get_table_sql(connection, table)
    foreign_keys = _get_table_foreign_keys(connection, table)
    triggers = _get_table_triggers(connection, table)
    options = _get_table_options(connection, table)
    unique_indexes = _get_unique_indexes(connection, table)
    unique_index_columns = {}

    for index_name in unique_indexes:
        unique_index_columns[index_name] = _get_index_columns(connection, index_name)


    return {
        "columns": columns,
        "sql": sql,
        "foreign_keys": foreign_keys,
        "triggers": triggers,
        "options": options,
        "unique_indexes": unique_index_columns,
    }


def _sql_tokens(sql):
    """
    Normalize SQL tokens for conservative structural comparisons.

    Ignores comments, external whitespace, unquoted identifier casing,
    and CREATE TABLE's optional IF NOT EXISTS clause. Preserves quoted
    strings and identifiers without modifying their contents.

    This is not a full SQL parser. Some equivalent SQL statements may
    still produce different tokens.

    Args:
        sql (str | None): SQL definition to normalize.

    Returns:
        tuple | None: Normalized token sequence.

    Raises:
        TypeError: If sql is not a string or None.

    Usage:
        tokens = _sql_tokens("CREATE TABLE school (id INTEGER)")
    """
    if sql is None:
        return None
    if not isinstance(sql, str):
        raise TypeError("'sql' must be a string or None.")

    pattern = (
        r"--[^\r\n]*|/\*.*?\*/|'(?:''|[^'])*'|"
        r'"(?:""|[^"])*"|`(?:``|[^`])*`|\[(?:[^\]]|\]\])*\]|'
        r"[A-Za-z_][A-Za-z_0-9]*|[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|[^\s]"
    )
    tokens = []

    for token in re.findall(pattern, sql, flags=re.DOTALL):
        if token.startswith(("--", "/*")):
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", token):
            token = token.upper()

        tokens.append(token)

    if tokens[:2] == ["CREATE", "TABLE"] and tokens[2:5] == ["IF", "NOT", "EXISTS"]:
        del tokens[2:5]

    return tuple(tokens)


@_handle_errors
def _compare_schema(connection, database, table):
    """
    Compare a registered table definition with its actual SQLite structure.

    Creates the expected table in memory and inspects both structures.
    Detects differences in columns, options, unique indexes, foreign keys,
    triggers, and stored SQL definitions.

    Indexes are compared by structure rather than automatically generated
    names. SQL text is normalized conservatively before comparison.

    Args:
        connection (sqlite3.Connection): Connection to the actual database.
        database (str): Registered database name.
        table (str): Registered table name.

    Returns:
        dict: Report containing database, table, matches, and differences.

    Raises:
        TypeError: If an argument or configuration has an invalid type.
        ValueError: If the database or table is not registered.
        sqlite3.Error: If SQLite cannot construct or inspect the schemas.

    Usage:
        report = _compare_schema(connection, "materials", "school")
        print(report["matches"])
    """
    _check_columns(database, table)

    if not _table_exists(connection, table):
        return {
            "database": database,
            "table": table,
            "matches": False,
            "differences": {"missing_table": table},
        }


    # Normalize unique index definitions
    def index_definitions(source_connection, schema):
        query = f"PRAGMA main.index_list({_quote_identifier(table)})"

        with closing(source_connection.execute(query)) as cursor:
            index_info = {row[1]: row for row in cursor.fetchall() if row[2] == 1}

        definitions = []

        for index_name, components in schema["unique_indexes"].items():
            info = index_info[index_name]
            sql = None

            if info[3] == "c":
                query = "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = ?"

                with closing(source_connection.execute(query, (index_name,))) as cursor:
                    result = cursor.fetchone()

                sql = _sql_tokens(result[0]) if result and result[0] else None

            keys = tuple(
                (row[2], row[3], row[4], row[1] if row[2] is None else None)
                for row in components if row[5] == 1
            )

            definitions.append({
                "origin": info[3],
                "partial": bool(info[4]),
                "keys": keys,
                "sql": sql,
            })

        return sorted(definitions, key=repr)


    # Retrieve the physical schema
    actual_schema = _inspect_schema(connection, table)

    # Build the expected schema in memory
    query, parameters = _build_create(database, table)

    with closing(sqlite3.connect(":memory:")) as memory_connection:
        memory_connection.execute(query, parameters).close()
        expected_schema = _inspect_schema(memory_connection, table)
        expected_indexes = index_definitions(memory_connection, expected_schema)

    actual_indexes = index_definitions(connection, actual_schema)


    # Compare columns
    expected_columns = expected_schema["columns"]
    actual_columns = actual_schema["columns"]
    missing_columns = sorted(expected_columns.keys() - actual_columns.keys())
    extra_columns = sorted(actual_columns.keys() - expected_columns.keys())
    changed_columns = {}

    for column in expected_columns.keys() & actual_columns.keys():
        if expected_columns[column] != actual_columns[column]:
            changed_columns[column] = {
                "expected": expected_columns[column],
                "actual": actual_columns[column],
            }


    # Compare table options
    expected_options = expected_schema["options"]
    actual_options = actual_schema["options"]
    changed_options = {}

    for option, expected_value in expected_options.items():
        actual_value = actual_options.get(option)

        if expected_value != actual_value:
            changed_options[option] = {
                "expected": expected_value,
                "actual": actual_value,
            }


    # Compare other structural definitions
    changed_structures = {}

    for field in ("foreign_keys", "triggers", "sql"):
        expected_value = expected_schema[field]
        actual_value = actual_schema[field]

        if field == "sql":
            different = _sql_tokens(expected_value) != _sql_tokens(actual_value)
        else:
            different = expected_value != actual_value

        if different:
            changed_structures[field] = {
                "expected": expected_value,
                "actual": actual_value,
            }

    if expected_indexes != actual_indexes:
        changed_structures["unique_indexes"] = {
            "expected": expected_indexes,
            "actual": actual_indexes,
        }


    # Assemble the comparison report
    differences = {}

    if missing_columns:
        differences["missing_columns"] = missing_columns
    if extra_columns:
        differences["extra_columns"] = extra_columns
    if changed_columns:
        differences["changed_columns"] = changed_columns
    if changed_options:
        differences["changed_options"] = changed_options
    if changed_structures:
        differences["changed_structures"] = changed_structures

    return {
        "database": database,
        "table": table,
        "matches": not differences,
        "differences": differences,
    }


@_handle_errors
def _require_matching_schema(connection, database, table):
    """
    Require a SQLite table to match its registered Python definition.

    Uses _compare_schema() and blocks operations when structural
    differences are detected. Does not perform migrations.

    Args:
        connection (sqlite3.Connection): Open database connection.
        database (str): Registered database name.
        table (str): Table to validate.

    Returns:
        None: If both schemas match.

    Raises:
        ValueError: If the table schema is incompatible.
        sqlite3.Error: If SQLite cannot inspect the table.

    Usage:
        _require_matching_schema(connection, "materials", "school")
    """
    result = _compare_schema(connection, database, table)

    if not result["matches"]:
        raise ValueError(f"Schema mismatch in '{database}.{table}': {result['differences']}")


# SQL query builders
@_register_query("CREATE")
def _build_create(database, table):
    """
    Build a STRICT CREATE TABLE statement from registered definitions.

    Uses IF NOT EXISTS, but does not itself verify existing table structures.

    Args:
        database (str): Registered database name.
        table (str): Table to create.

    Returns:
        tuple[str, tuple]: CREATE TABLE statement and empty parameters.

    Raises:
        ValueError: If the database or table is not registered.

    Usage:
        query, parameters = query_builders["CREATE"]("materials", "school")
    """
    _check_columns(database, table)

    column_definitions = databases[database]["tables"][table]
    columns = ", ".join(f"{_quote_identifier(name)} {definition}" for name, definition in column_definitions.items())
    query = f"CREATE TABLE IF NOT EXISTS {_quote_identifier(table)} ({columns}) STRICT"

    return query, ()


@_register_query("INSERT")
def _build_insert(database, table, values):
    """
    Build a parameterized INSERT statement for one record.

    SQLite enforces the target table's constraints during execution.

    Args:
        database (str): Registered database name.
        table (str): Target table.
        values (dict): Column names mapped to values.

    Returns:
        tuple[str, tuple]: INSERT statement and parameters.

    Raises:
        TypeError: If values is not a dictionary.
        ValueError: If values is empty or contains unknown columns.

    Usage:
        query, parameters = query_builders["INSERT"](
            "materials", "school", {"name": "Pencil", "quantity": 10, "price": 2.50}
        )
    """
    if not isinstance(values, dict):
        raise TypeError(f"Insert values must be a dictionary, received {type(values).__name__}.")
    if not values:
        raise ValueError(f"Cannot insert an empty record into table '{table}'.")

    _check_columns(database, table, values)

    columns = ", ".join(_quote_identifier(column) for column in values)
    placeholders = ", ".join("?" for _ in values)
    query = f"INSERT INTO {_quote_identifier(table)} ({columns}) VALUES ({placeholders})"

    return query, tuple(values.values())


@_register_query("SELECT")
def _build_select(database, table, columns="*", filters=None, order_by=None, descending=False, limit=20, offset=0):
    """
    Build a SELECT statement with filtering, sorting, and pagination.

    Uses SQLite placeholders for filter values, LIMIT, and OFFSET.

    Args:
        database (str): Registered database name.
        table (str): Table to query.
        columns (str | list | tuple): Columns to retrieve, or "*".
        filters (dict | None): Optional filter conditions.
        order_by (str | None): Sorting column.
        descending (bool): Whether to sort in descending order.
        limit (int | None): Maximum rows, or None for all.
        offset (int): Number of matching rows to skip.

    Returns:
        tuple[str, tuple]: SELECT statement and parameters.

    Raises:
        TypeError: If an argument has an invalid type.
        ValueError: If a column, filter, or pagination value is invalid.

    Usage:
        query, parameters = query_builders["SELECT"](
            "materials", "school", filters={"price": {"lte": 10}}
        )
    """
    _check_columns(database, table)

    if limit is not None and type(limit) is not int:
        raise TypeError(f"'limit' must be an integer or None, received {type(limit).__name__}.")
    if limit is not None and limit < 0:
        raise ValueError(f"'limit' cannot be negative, received {limit}.")
    if type(offset) is not int:
        raise TypeError(f"'offset' must be an integer, received {type(offset).__name__}.")
    if offset < 0:
        raise ValueError(f"'offset' cannot be negative, received {offset}.")
    if type(descending) is not bool:
        raise TypeError(f"'descending' must be a boolean, received {type(descending).__name__}.")

    if isinstance(columns, str):
        if columns == "*":
            selected_columns = "*"
        else:
            _check_columns(database, table, (columns,))
            selected_columns = _quote_identifier(columns)
    else:
        if not isinstance(columns, (list, tuple)):
            raise TypeError("'columns' must be '*', a column name, list, or tuple.")
        if not columns:
            raise ValueError("'columns' cannot be empty.")

        _check_columns(database, table, columns)
        selected_columns = ", ".join(_quote_identifier(column) for column in columns)

    query = f"SELECT {selected_columns} FROM {_quote_identifier(table)}"
    where_clause, where_parameters = _where(database, table, filters)
    query += where_clause
    parameters = list(where_parameters)

    if order_by is not None:
        if not isinstance(order_by, str):
            raise TypeError(f"'order_by' must be a string or None, received {type(order_by).__name__}.")
        _check_columns(database, table, (order_by,))
        query += f" ORDER BY {_quote_identifier(order_by)} {'DESC' if descending else 'ASC'}"
    elif descending:
        raise ValueError("'descending=True' requires an 'order_by' column.")

    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    elif offset:
        query += " LIMIT -1"

    if offset:
        query += " OFFSET ?"
        parameters.append(offset)

    return query, tuple(parameters)


@_register_query("UPDATE")
def _build_update(database, table, values, filters=None, allow_all=False, require_unique=True):
    """
    Build a parameterized UPDATE statement.

    Validates WHERE conditions and bulk-update permissions.
    Physical uniqueness is checked during execution.

    Args:
        database (str): Registered database name.
        table (str): Table to update.
        values (dict): Columns mapped to new values.
        filters (dict | None): Conditions selecting records.
        allow_all (bool): Permit unfiltered updates.
        require_unique (bool): Require a unique equality condition.

    Returns:
        tuple[str, tuple]: UPDATE statement and parameters.

    Raises:
        TypeError: If values or safety options have invalid types.
        ValueError: If values or filtering conditions are invalid.

    Usage:
        query, parameters = query_builders["UPDATE"](
            "materials", "school", {"price": 3.50}, filters={"id": 1}
        )
    """
    if not isinstance(values, dict):
        raise TypeError(f"Update values must be a dictionary, received {type(values).__name__}.")
    if not values:
        raise ValueError(f"Update values cannot be empty for table '{table}'.")

    _check_columns(database, table, values)

    where_clause, where_parameters = _validate_write_filters(database, table, filters, allow_all, require_unique)
    assignments = ", ".join(f"{_quote_identifier(column)} = ?" for column in values)
    query = f"UPDATE {_quote_identifier(table)} SET {assignments}{where_clause}"

    return query, (*values.values(), *where_parameters)


@_register_query("DELETE")
def _build_delete(database, table, filters=None, allow_all=False, require_unique=True):
    """
    Build a parameterized DELETE statement.

    Validates WHERE conditions and bulk-deletion permissions.
    Physical uniqueness is checked during execution.

    Args:
        database (str): Registered database name.
        table (str): Table from which records will be deleted.
        filters (dict | None): Conditions selecting records.
        allow_all (bool): Permit deletion without filters.
        require_unique (bool): Require a unique equality condition.

    Returns:
        tuple[str, tuple]: DELETE statement and parameters.

    Raises:
        TypeError: If filters or safety options have invalid types.
        ValueError: If deletion conditions are not permitted.

    Usage:
        query, parameters = query_builders["DELETE"]("materials", "school", filters={"id": 1})
    """
    where_clause, parameters = _validate_write_filters(database, table, filters, allow_all, require_unique)
    return f"DELETE FROM {_quote_identifier(table)}{where_clause}", parameters


@_register_query("COUNT")
def _build_count(database, table, filters=None):
    """
    Build a SELECT COUNT(*) statement for matching records.

    Counts records without retrieving their complete data.

    Args:
        database (str): Registered database name.
        table (str): Table to query.
        filters (dict | None): Conditions restricting the count.

    Returns:
        tuple[str, tuple]: COUNT statement and parameters.

    Raises:
        TypeError: If filters has an invalid type.
        ValueError: If the table or filter columns are invalid.

    Usage:
        query, parameters = query_builders["COUNT"]("materials", "school")
    """
    where_clause, parameters = _where(database, table, filters)
    return f"SELECT COUNT(*) FROM {_quote_identifier(table)}{where_clause}", parameters


# Database backups
@_handle_errors
def backup_database(database, overwrite=False):
    """
    Create a consistent backup of an existing SQLite database.

    Uses SQLite's backup API instead of copying the database file directly.
    Writes to a temporary file, checks its integrity with PRAGMA quick_check,
    and publishes the backup only after completion.

    The destination is defined by backup_path in the database configuration.
    Existing backups are preserved unless overwrite=True is explicitly used.

    Args:
        database (str): Registered database name.
        overwrite (bool): Whether an existing backup may be replaced.

    Returns:
        Path: Location of the completed backup.

    Raises:
        TypeError: If overwrite is not a boolean.
        FileExistsError: If the backup exists and overwrite is False.
        sqlite3.Error: If backup creation or integrity checking fails.
        OSError: If a filesystem operation fails.

    Usage:
        backup_database("materials")
        backup_database("materials", overwrite=True)
    """
    if type(overwrite) is not bool:
        raise TypeError("'overwrite' must be a boolean.")

    _validate_config(database)

    backup_path = databases[database]["database_info"]["backup_path"]

    if backup_path.exists() and not overwrite:
        raise FileExistsError(f"Backup already exists: '{backup_path}'.")

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            dir=backup_path.parent, prefix=f".{backup_path.name}.",
            suffix=".tmp", delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)


        with _ConnectionManager(database, read_only=True) as source:
            with closing(sqlite3.connect(temporary_path)) as destination:
                source.backup(destination)

                with closing(destination.execute("PRAGMA quick_check")) as cursor:
                    result = cursor.fetchone()

                if result != ("ok",):
                    raise sqlite3.DatabaseError(f"Backup integrity check failed: {result!r}.")


        if overwrite:
            os.replace(temporary_path, backup_path)
        else:
            os.link(temporary_path, backup_path)

        return backup_path

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


# Statement execution
@_handle_errors
def _execute(action, database, table, **kwargs):
    """
    Build and execute a registered SQL operation.

    Retrieves the corresponding query builder, executes the statement,
    and returns the result. The connection manager automatically commits
    successful transactions or rolls back failed operations.

    Validates the physical schema before executing data operations.
    CREATE statements are verified after execution and before commit.

    UPDATE and DELETE require a physically unique equality filter by default.
    Write operations use an explicit transaction during validation and execution.

    Args:
        action (str): Registered SQL action.
        database (str): Registered database name.
        table (str): Target table.
        **kwargs: Additional arguments accepted by the query builder.

    Returns:
        int: Inserted row ID, affected row count, or COUNT result.
        list[tuple]: Rows returned by SELECT.
        None: For CREATE or operations without a result.

    Raises:
        TypeError: If an argument has an invalid type.
        ValueError: If the action, schema, or conditions are invalid.
        sqlite3.Error: If SQLite cannot execute or commit the operation.

    Usage:
        _execute("CREATE", "materials", "school")
        _execute("SELECT", "materials", "school", limit=10)
    """
    if not isinstance(action, str):
        raise TypeError(f"SQL action must be a string, received {type(action).__name__}.")
    if action not in query_builders:
        raise ValueError(f"Unknown SQL action '{action}'. Available actions: {sorted(query_builders)}.")

    query, parameters = query_builders[action](database, table, **kwargs)

    with _ConnectionManager(database) as connection:
        if action in ("CREATE", "INSERT", "UPDATE", "DELETE"):
            connection.execute("BEGIN IMMEDIATE").close()

        if action != "CREATE":
            _require_matching_schema(connection, database, table)

        if action in ("UPDATE", "DELETE") and kwargs.get("require_unique", True):
            _require_unique_filter(connection, database, table, kwargs.get("filters"))

        with closing(connection.execute(query, parameters)) as cursor:
            if action == "INSERT":
                return cursor.lastrowid
            elif action == "SELECT":
                return cursor.fetchall()
            elif action == "COUNT":
                return cursor.fetchone()[0]
            elif action in ("UPDATE", "DELETE"):
                return cursor.rowcount

        if action == "CREATE":
            _require_matching_schema(connection, database, table)

        return None


# Public operations
class Database:
    """
    Provide a public interface for managing a registered SQLite database.

    Stores the registered database name and exposes initialization,
    CRUD operations, schema inspection, comparison, and backup creation.

    Internal functions generate SQL and manage database connections.

    Args:
        name (str): Name of a registered database.

    Raises:
        TypeError: If the configuration contains invalid types.
        ValueError: If the database is unknown or incorrectly configured.

    Usage:
        db = Database("materials")
        db.initialize()
        db.insert("school", {"name": "Pencil", "quantity": 10, "price": 2.50})
    """

    def __init__(self, name):
        _validate_config(name)
        self.name = name

    def initialize(self):
        """
        Initialize the database and validate its table schemas.

        Creates missing STRICT tables and applies the configured journal mode.
        Existing tables are verified but are not modified or migrated.

        Returns:
            None: If initialization succeeds.

        Raises:
            ValueError: If a registered table has an incompatible schema.
            sqlite3.Error: If initialization fails.

        Usage:
            db.initialize()
        """
        return initialize_database(self.name)

    def create_table(self, table):
        """
        Create a registered table if it does not already exist.

        Validates the resulting table against its Python definition.

        Args:
            table (str): Name of the table.

        Returns:
            None: If creation and validation succeed.

        Raises:
            ValueError: If the table is unknown or structurally incompatible.
            sqlite3.Error: If SQLite cannot create the table.

        Usage:
            db.create_table("school")
        """
        return _execute("CREATE", self.name, table)

    def insert(self, table, values):
        """
        Insert one record into a registered SQLite table.

        Uses a dictionary of column names and values. SQLite enforces
        the constraints declared in the physical table schema.

        Args:
            table (str): Name of the target table.
            values (dict): Columns mapped to values.

        Returns:
            int: Row ID assigned to the inserted record.

        Raises:
            TypeError: If values is not a dictionary.
            ValueError: If values or the table schema are invalid.
            sqlite3.Error: If SQLite cannot insert the record.

        Usage:
            db.insert("school", {"name": "Pencil", "quantity": 10, "price": 2.50})
        """
        return _execute("INSERT", self.name, table, values=values)

    def search(self, table, columns="*", filters=None, order_by=None, descending=False, limit=20, offset=0):
        """
        Retrieve records using filters, sorting, and pagination.

        Args:
            table (str): Name of the table.
            columns (str | list | tuple): Selected columns, or "*".
            filters (dict | None): Conditions selecting records.
            order_by (str | None): Column used for sorting.
            descending (bool): Whether sorting uses descending order.
            limit (int | None): Maximum results, or None for all.
            offset (int): Number of matching records to skip.

        Returns:
            list[tuple]: Matching rows in the selected column order.

        Raises:
            TypeError: If an argument has an invalid type.
            ValueError: If a column, filter, or pagination value is invalid.
            sqlite3.Error: If SQLite cannot execute the query.

        Usage:
            db.search("school", filters={"price": {"lte": 10}}, order_by="price")
        """
        return _execute("SELECT", self.name, table, columns=columns, filters=filters,
                        order_by=order_by, descending=descending, limit=limit, offset=offset)

    def update(self, table, values, filters=None, allow_all=False, require_unique=True):
        """
        Update records matching the specified conditions.

        Requires a physically unique equality filter by default.
        Bulk updates require require_unique=False, and unfiltered
        updates additionally require allow_all=True.

        Args:
            table (str): Name of the table.
            values (dict): Columns mapped to new values.
            filters (dict | None): Conditions selecting records.
            allow_all (bool): Permit unfiltered updates.
            require_unique (bool): Require a unique equality filter.

        Returns:
            int: Number of records affected.

        Raises:
            TypeError: If an argument has an invalid type.
            ValueError: If update conditions or the schema are invalid.
            sqlite3.Error: If SQLite cannot complete the update.

        Usage:
            db.update("school", {"price": 3.50}, filters={"id": 1})
        """
        return _execute("UPDATE", self.name, table, values=values, filters=filters,
                        allow_all=allow_all, require_unique=require_unique)

    def delete(self, table, filters=None, allow_all=False, require_unique=True):
        """
        Delete records matching the specified conditions.

        Requires a physically unique equality filter by default.
        Bulk deletions require require_unique=False, and unfiltered
        deletions additionally require allow_all=True.

        Args:
            table (str): Name of the table.
            filters (dict | None): Conditions selecting records.
            allow_all (bool): Permit unfiltered deletion.
            require_unique (bool): Require a unique equality filter.

        Returns:
            int: Number of records deleted.

        Raises:
            TypeError: If an argument has an invalid type.
            ValueError: If deletion conditions or the schema are invalid.
            sqlite3.Error: If SQLite cannot complete the deletion.

        Usage:
            db.delete("school", filters={"id": 1})
        """
        return _execute("DELETE", self.name, table, filters=filters,
                        allow_all=allow_all, require_unique=require_unique)

    def count(self, table, filters=None):
        """
        Count records matching optional filter conditions.

        Returns the number of matches without retrieving complete rows.

        Args:
            table (str): Name of the table.
            filters (dict | None): Conditions selecting records.

        Returns:
            int: Number of matching records.

        Raises:
            TypeError: If filters has an invalid type.
            ValueError: If the table or filter columns are invalid.
            sqlite3.Error: If SQLite cannot execute the query.

        Usage:
            db.count("school", filters={"quantity": {"gte": 10}})
        """
        return _execute("COUNT", self.name, table, filters=filters)

    def inspect_schema(self, table):
        """
        Retrieve the physical schema of an SQLite table.

        Opens a read-only connection and retrieves column definitions,
        constraints, unique indexes, triggers, and table options.

        Args:
            table (str): Name of the table to inspect.

        Returns:
            dict: Structural information retrieved directly from SQLite.

        Raises:
            ValueError: If the table does not exist.
            sqlite3.Error: If SQLite cannot inspect its structure.

        Usage:
            schema = db.inspect_schema("school")
        """
        with _ConnectionManager(self.name, read_only=True) as connection:
            return _inspect_schema(connection, table)

    def compare_schema(self, table):
        """
        Compare the registered definition with the physical SQLite schema.

        Opens a read-only connection and reports structural differences.
        Does not modify the database or perform migrations.

        Args:
            table (str): Name of the registered table.

        Returns:
            dict: Comparison report and detected differences.

        Raises:
            ValueError: If the table is not registered.
            sqlite3.Error: If SQLite cannot inspect the database.

        Usage:
            report = db.compare_schema("school")
            print(report["matches"])
        """
        with _ConnectionManager(self.name, read_only=True) as connection:
            return _compare_schema(connection, self.name, table)

    def backup(self, overwrite=False):
        """
        Create a consistent backup of the registered SQLite database.

        Uses the configured backup_path. Refuses to replace an existing
        backup unless overwrite=True is explicitly provided.

        Args:
            overwrite (bool): Whether an existing backup may be replaced.

        Returns:
            Path: Location of the completed backup.

        Raises:
            TypeError: If overwrite is not a boolean.
            FileExistsError: If the destination exists and overwrite is False.
            sqlite3.Error: If SQLite cannot create or verify the backup.
            OSError: If a filesystem operation fails.

        Usage:
            backup_path = db.backup()
            backup_path = db.backup(overwrite=True)
        """
        return backup_database(self.name, overwrite=overwrite)
