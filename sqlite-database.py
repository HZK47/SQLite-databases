
import logging
import sqlite3
import string
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

    The decorator stores the function in query_builders without calling it.
    Each action can be registered only once and must use uppercase A-Z.

    Args:
        action (str): Uppercase SQL action name, such as 'SELECT'.

    Returns:
        callable: A decorator that registers and returns the original function.

    Raises:
        TypeError: If action is not a string or the decorated object is not callable.
        ValueError: If action is empty, invalid, or already registered.

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
    Validate database, table, and column names registered in Python.

    Checks the configuration dictionary, including basic column definition
    types. Does not inspect or validate the physical SQLite schema.

    Args:
        database (str): Name of a registered database.
        table (str | None): Table to validate, or None to check only the database.
        columns (str | iterable[str] | None): Column names to validate.
            An empty iterable or None checks only the database and table.

    Returns:
        None: If all requested names are registered.

    Raises:
        TypeError: If a name or definition has an invalid type.
        ValueError: If a database, table, or column is unknown or empty.

    Usage:
        _check_columns("materials")
        _check_columns("materials", "school")
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
        raise ValueError(f"Table '{table}' in database '{database}' must contain at least one column.")

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
    Quote a SQL table or column name without changing its contents.

    Internal double quotes are escaped by doubling them. Leading and
    trailing spaces are preserved.

    Args:
        name (str): SQL identifier to quote.

    Returns:
        str: The identifier enclosed in double quotes.

    Raises:
        TypeError: If name is not a string.
        ValueError: If name is empty or contains only whitespace.

    Usage:
        _quote_identifier("school")  # '"school"'
        _quote_identifier('a"b')     # '"a""b"'
    """
    if not isinstance(name, str):
        raise TypeError(f"SQL identifier must be a string, received {type(name).__name__}.")
    if not name.strip():
        raise ValueError("SQL identifier cannot be empty or contain only whitespace.")

    return '"' + name.replace('"', '""') + '"'


def _validate_config(database):
    """
    Validate the configuration of a registered SQLite database.

    Checks required field names, types, and allowed values without changing
    the configuration, opening the database, or creating files.

    Args:
        database (str): Name of the registered database to validate.

    Returns:
        None: If all supported settings are valid.

    Raises:
        TypeError: If a configuration field has an invalid type.
        ValueError: If a required field is missing or contains an invalid value.

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
        raise ValueError(
            f"Invalid journal mode '{journal_mode}'. Allowed modes: {sorted(PERMITTED_JOURNAL_MODES)}."
        )

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

    Supports implicit equality, eq/ne/gt/gte/lt/lte/like, IN lists,
    and IS NULL/IS NOT NULL checks. Values use SQLite placeholders.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        filters (dict | None): Columns mapped to direct values or dictionaries
            containing one operator, such as {'price': {'gte': 10}}.

    Returns:
        tuple[str, tuple]: The WHERE clause and parameters.
            Returns ('', ()) when no filters are supplied.

    Raises:
        TypeError: If filters or an operator value has an invalid type.
        ValueError: If a column, operator, or filter value is invalid.

    Usage:
        _where("materials", "school", {"name": "Pencil"})
        _where("materials", "school", {"quantity": {"in": [5, 10]}})
        _where("materials", "school", {"description": {"is_null": True}})
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
    Validate the filters and safety settings shared by UPDATE and DELETE.

    By default, a filtered write requires equality on a declared
    PRIMARY KEY or single-column UNIQUE field. Physical constraints
    are not yet verified against the actual SQLite schema.

    Args:
        database (str): Name of the registered database.
        table (str): Table being modified.
        filters (dict | None): Conditions identifying records.
        allow_all (bool): Allow operations without filters.
        require_unique (bool): Require equality on a unique column.

    Returns:
        tuple[str, tuple]: The WHERE clause and its parameters.

    Raises:
        TypeError: If a safety option or filter has an incorrect type.
        ValueError: If required filters or unique conditions are missing.

    Usage:
        _validate_write_filters("materials", "school", {"id": 1}, False, True)
    """
    if type(allow_all) is not bool:
        raise TypeError("'allow_all' must be a boolean.")
    if type(require_unique) is not bool:
        raise TypeError("'require_unique' must be a boolean.")

    where_clause, parameters = _where(database, table, filters)

    if not where_clause:
        if not allow_all:
            raise ValueError(f"Unfiltered modifications to table '{table}' require allow_all=True.")
        if require_unique:
            raise ValueError("Set require_unique=False to allow modifying all records.")
        return where_clause, parameters

    if require_unique:
        column_definitions = databases[database]["tables"][table]
        unique_columns = {
            column for column, definition in column_definitions.items()
            if "PRIMARY KEY" in definition.upper() or "UNIQUE" in definition.upper().split()
        }

        for column, condition in filters.items():
            if column not in unique_columns:
                continue

            if isinstance(condition, dict):
                operator, value = next(iter(condition.items()))
            else:
                operator, value = "eq", condition

            if operator == "eq" and value is not None:
                break
        else:
            raise ValueError(
                f"Modification of table '{table}' requires equality on a unique column. "
                f"Available unique columns: {sorted(unique_columns)}."
            )

    return where_clause, parameters


# Connection management
class _ConnectionManager:
    """
    Open, configure, and close registered SQLite database connections.

    Supports read-write and read-only access. Uses a context manager to
    commit successful transactions, roll back failures, and close connections.

    Args:
        database (str): Registered database name.
        read_only (bool): Open an existing database without write access.

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
        Open and configure the SQLite connection.

        Returns:
            sqlite3.Connection: Open and configured connection.

        Raises:
            RuntimeError: If the manager is already open or SQLite is outdated.
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
            connection.execute(f"PRAGMA foreign_keys = {foreign_keys}")

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
            bool | None: SQLite context manager result; exceptions are not suppressed.
        """
        try:
            return self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self._connection.close()
            self._connection = None


@_handle_errors
def initialize_database(database):
    """
    Apply journal settings and create registered SQLite tables.

    Sets journal_mode and creates missing STRICT tables. Existing tables
    are not modified, compared, or migrated. Does not assign schema_version.

    Args:
        database (str): Name of the database to initialize.

    Returns:
        None: If initialization succeeds.

    Raises:
        TypeError: If the database configuration has an invalid type.
        ValueError: If the database or its configuration is invalid.
        sqlite3.Error: If journal configuration or table creation fails.

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


# SQLite schema inspection
@_handle_errors
def _table_exists(connection, table):
    """
    Check whether a table exists in the physical SQLite database.

    Queries sqlite_schema instead of relying on the Python configuration.
    Does not create or modify tables and leaves the connection open.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        table (str): Name of the table to find.

    Returns:
        bool: True if the table exists, otherwise False.

    Raises:
        sqlite3.Error: If SQLite cannot execute the query.

    Usage:
        with _ConnectionManager("materials", read_only=True) as connection:
            exists = _table_exists(connection, "school")
    """
    query = "SELECT name FROM main.sqlite_schema WHERE type = 'table' AND name = ?"

    with closing(connection.execute(query, (table,))) as cursor:
        return cursor.fetchone() is not None


@_handle_errors
def _get_table_columns(connection, table):
    """
    Retrieve information about the columns of an existing SQLite table.

    Uses PRAGMA table_xinfo to include normal, generated, and hidden
    columns. Results are indexed by column name.

    Each SQLite row contains seven fields:
        0. cid: Column identifier or position.
        1. name: Column name.
        2. type: Declared SQL data type.
        3. notnull: Whether an explicit NOT NULL constraint exists.
        4. dflt_value: Default value expression, or None.
        5. pk: Position in the primary key, or 0.
        6. hidden: Normal, hidden, or generated column indicator.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        table (str): Name of the table to inspect.

    Returns:
        dict[str, tuple]: Column names mapped to complete metadata tuples.

    Raises:
        TypeError: If the table name has an invalid type.
        ValueError: If the table name is invalid or the table does not exist.
        sqlite3.Error: If SQLite cannot inspect the table.

    Usage:
        with _ConnectionManager("materials", read_only=True) as connection:
            columns = _get_table_columns(connection, "school")
            print(columns["price"][2])  # REAL
    """
    with closing(connection.execute(f"PRAGMA main.table_xinfo({_quote_identifier(table)})")) as cursor:
        rows = cursor.fetchall()

    if not rows and not _table_exists(connection, table):
        raise ValueError(f"Table '{table}' does not exist in the SQLite database.")

    return {row[1]: row for row in rows}


@_handle_errors
def _get_table_sql(connection, table):
    """
    Retrieve the stored CREATE TABLE statement of an existing SQLite table.

    Reads sqlite_schema to obtain the table's SQL definition, including
    constraints not completely described by PRAGMA table_xinfo. The stored
    statement may reflect modifications made through ALTER TABLE.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        table (str): Name of the table to inspect.

    Returns:
        str: CREATE TABLE statement stored by SQLite.

    Raises:
        ValueError: If the table does not exist.
        sqlite3.Error: If SQLite cannot retrieve the definition.

    Usage:
        with _ConnectionManager("materials", read_only=True) as connection:
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
    Return the names of unique indexes belonging to a SQLite table.

    Uses PRAGMA index_list and selects entries whose unique flag is 1.
    Includes composite and partial unique indexes, which do not necessarily
    guarantee the uniqueness of an individual column.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        table (str): Table whose indexes should be inspected.

    Returns:
        list[str]: Names of unique indexes, or an empty list.

    Raises:
        TypeError: If the table name is not a string.
        ValueError: If the table name is empty or whitespace.
        sqlite3.Error: If SQLite cannot retrieve the indexes.

    Usage:
        _get_unique_indexes(connection, "school")
    """
    query = f"PRAGMA main.index_list({_quote_identifier(table)})"

    with closing(connection.execute(query)) as cursor:
        return [row[1] for row in cursor.fetchall() if row[2] == 1]


@_handle_errors
def _get_index_columns(connection, index):
    """
    Retrieve all components of an SQLite index using PRAGMA index_xinfo.

    Each row contains position, column ID, column name, sort direction,
    collation, and whether the entry belongs to the index key.
    Expressions and auxiliary columns are also included.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        index (str): Name of the index to inspect.

    Returns:
        list[tuple]: Index metadata rows, or an empty list.

    Raises:
        TypeError: If the index name is not a string.
        ValueError: If the index name is empty or whitespace.
        sqlite3.Error: If SQLite cannot inspect the index.

    Usage:
        _get_index_columns(connection, "idx_school_name")
    """
    query = f"PRAGMA main.index_xinfo({_quote_identifier(index)})"

    with closing(connection.execute(query)) as cursor:
        return cursor.fetchall()


@_handle_errors
def _get_table_foreign_keys(connection, table):
    """
    Retrieve foreign key definitions associated with a SQLite table.

    Uses PRAGMA foreign_key_list. Each row identifies the foreign key,
    referenced table, source and target columns, and update/delete
    actions. Composite foreign keys may produce multiple rows.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        table (str): Table whose foreign keys should be inspected.

    Returns:
        list[tuple]: Foreign key metadata, or an empty list.

    Raises:
        TypeError: If the table name is not a string.
        ValueError: If the table name is empty or whitespace.
        sqlite3.Error: If SQLite cannot inspect the table.

    Usage:
        _get_table_foreign_keys(connection, "school")
    """
    query = f"PRAGMA main.foreign_key_list({_quote_identifier(table)})"

    with closing(connection.execute(query)) as cursor:
        return cursor.fetchall()


@_handle_errors
def _get_table_options(connection, table):
    """
    Retrieve a SQLite table's structural options.

    Uses PRAGMA table_list to obtain its type, column count,
    WITHOUT ROWID setting, and STRICT setting.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        table (str): Table whose options should be inspected.

    Returns:
        dict | None: Table options, or None if the table does not exist.

    Raises:
        TypeError: If the table name is not a string.
        ValueError: If the table name is empty or whitespace.
        sqlite3.Error: If SQLite cannot inspect the schema.

    Usage:
        _get_table_options(connection, "school")
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

    Reads sqlite_schema and orders the matching triggers by name.
    Does not execute triggers or modify the database.

    Args:
        connection (sqlite3.Connection): An open SQLite connection.
        table (str): Table whose triggers should be inspected.

    Returns:
        list[tuple]: Tuples containing trigger names and SQL definitions.

    Raises:
        sqlite3.Error: If SQLite cannot inspect the schema.

    Usage:
        _get_table_triggers(connection, "school")
    """
    query = "SELECT name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND tbl_name = ? ORDER BY name"

    with closing(connection.execute(query, (table,))) as cursor:
        return cursor.fetchall()


# SQL query builders
@_register_query("CREATE")
def _build_create(database, table):
    """
    Build a STRICT CREATE TABLE statement from the registered schema.

    Uses the declared column names and SQL definitions. IF NOT EXISTS
    prevents an error for an existing table but does not validate its structure.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the table to create.

    Returns:
        tuple[str, tuple]: CREATE TABLE statement and an empty tuple.

    Raises:
        TypeError: If the table definition has an invalid type.
        ValueError: If the database or table is unknown or has no columns.

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
    Build a parameterized INSERT statement for a single record.

    Uses the columns supplied in values. SQLite enforces table constraints
    when the generated statement is executed.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the target table.
        values (dict): Columns mapped to values to insert.

    Returns:
        tuple[str, tuple]: INSERT statement and its parameters.

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

    Validates selected columns and uses parameters for filter values,
    LIMIT, and OFFSET.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the target table.
        columns (str | list | tuple): Selected columns, or "*".
        filters (dict | None): Optional filter conditions.
        order_by (str | None): Column used for sorting.
        descending (bool): Whether sorting should use DESC.
        limit (int | None): Maximum returned rows, or None for all.
        offset (int): Number of matching rows to skip.

    Returns:
        tuple[str, tuple]: SELECT statement and SQL parameters.

    Raises:
        TypeError: If an argument has an invalid type.
        ValueError: If a column, filter, or pagination value is invalid.

    Usage:
        query, parameters = query_builders["SELECT"](
            "materials", "school", filters={"quantity": {"gte": 10}}, order_by="price"
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
    Build a parameterized UPDATE statement with safety checks.

    By default, requires equality on a declared unique column.
    Unfiltered updates require allow_all=True and require_unique=False.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the target table.
        values (dict): Columns mapped to their new values.
        filters (dict | None): Conditions selecting records.
        allow_all (bool): Permit updating all records.
        require_unique (bool): Require a unique equality condition.

    Returns:
        tuple[str, tuple]: UPDATE statement and its parameters.

    Raises:
        TypeError: If values or a safety option has an incorrect type.
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
    Build a parameterized DELETE statement with safety checks.

    By default, requires equality on a declared unique column.
    Unfiltered deletion requires allow_all=True and require_unique=False.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the target table.
        filters (dict | None): Conditions selecting records.
        allow_all (bool): Permit deleting all records.
        require_unique (bool): Require a unique equality condition.

    Returns:
        tuple[str, tuple]: DELETE statement and SQL parameters.

    Raises:
        TypeError: If filters or a safety option has an incorrect type.
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

    Counts records without retrieving the complete rows.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the target table.
        filters (dict | None): Conditions restricting the count.

    Returns:
        tuple[str, tuple]: COUNT statement and SQL parameters.

    Raises:
        TypeError: If filters has an invalid type.
        ValueError: If the table or filtering columns are invalid.

    Usage:
        query, parameters = query_builders["COUNT"]("materials", "school")
    """
    where_clause, parameters = _where(database, table, filters)
    return f"SELECT COUNT(*) FROM {_quote_identifier(table)}{where_clause}", parameters


# Statement execution
@_handle_errors
def _execute(action, database, table, **kwargs):
    """
    Build and execute a registered SQL operation.

    Retrieves the query builder, executes the statement, and returns
    its result. The connection manager commits or rolls back automatically.

    Args:
        action (str): Registered SQL action.
        database (str): Name of the registered database.
        table (str): Name of the target table.
        **kwargs: Additional arguments accepted by the query builder.

    Returns:
        int: Inserted row ID, affected row count, or COUNT result.
        list[tuple]: Rows returned by SELECT.
        None: For CREATE or operations without a result.

    Raises:
        TypeError: If an argument has an invalid type.
        ValueError: If action or another argument is invalid.
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
        with closing(connection.execute(query, parameters)) as cursor:
            if action == "INSERT":
                return cursor.lastrowid
            elif action == "SELECT":
                return cursor.fetchall()
            elif action == "COUNT":
                return cursor.fetchone()[0]
            elif action in ("UPDATE", "DELETE"):
                return cursor.rowcount

            return None


# Public operations
class Database:
    """
    Provide a simple interface for managing a registered SQLite database.

    Stores the database name and exposes initialization, table creation,
    insertion, searching, updating, deletion, and counting operations.
    Internal functions generate and execute the SQL statements.

    Args:
        name (str): Name of a database registered in 'databases'.

    Raises:
        TypeError: If the database configuration has invalid types.
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
        Initialize the database and create its registered tables.

        Applies the configured journal mode and creates missing tables.
        Existing tables are not modified or migrated.

        Returns:
            None: If initialization succeeds.

        Raises:
            sqlite3.Error: If SQLite cannot initialize the database.

        Usage:
            db.initialize()
        """
        return initialize_database(self.name)

    def create_table(self, table):
        """
        Create a registered table if it does not already exist.

        Uses the column definitions stored in the Python configuration.
        Existing table structures are not modified.

        Args:
            table (str): Name of the table to create.

        Returns:
            None: If creation succeeds.

        Raises:
            ValueError: If the table is not registered.
            sqlite3.Error: If SQLite cannot create the table.

        Usage:
            db.create_table("school")
        """
        return _execute("CREATE", self.name, table)

    def insert(self, table, values):
        """
        Insert one record into a registered SQLite table.

        Uses a dictionary containing column names and values.
        SQLite enforces the table's constraints during insertion.

        Args:
            table (str): Name of the target table.
            values (dict): Columns mapped to their values.

        Returns:
            int: Row ID assigned to the inserted record.

        Raises:
            TypeError: If values is not a dictionary.
            ValueError: If values is empty or includes invalid columns.
            sqlite3.Error: If SQLite cannot insert the record.

        Usage:
            db.insert("school", {"name": "Pencil", "quantity": 10, "price": 2.50})
        """
        return _execute("INSERT", self.name, table, values=values)

    def search(self, table, columns="*", filters=None, order_by=None, descending=False, limit=20, offset=0):
        """
        Retrieve records using optional filtering, sorting, and pagination.

        Args:
            table (str): Name of the table.
            columns (str | list | tuple): Selected columns, or "*".
            filters (dict | None): Conditions selecting records.
            order_by (str | None): Column used for sorting.
            descending (bool): Sort in descending order if True.
            limit (int | None): Maximum results, or None for all.
            offset (int): Number of matching records to skip.

        Returns:
            list[tuple]: Matching rows in selected column order.

        Raises:
            TypeError: If an option has an invalid type.
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

        By default, requires equality on a declared unique column.
        Bulk updates require require_unique=False, and updating every
        record also requires allow_all=True.

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
            ValueError: If columns or update conditions are invalid.
            sqlite3.Error: If SQLite cannot complete the update.

        Usage:
            db.update("school", {"price": 3.50}, filters={"id": 1})
        """
        return _execute("UPDATE", self.name, table, values=values, filters=filters,
                        allow_all=allow_all, require_unique=require_unique)

    def delete(self, table, filters=None, allow_all=False, require_unique=True):
        """
        Delete records matching the specified conditions.

        By default, requires equality on a declared unique column.
        Bulk deletions require require_unique=False, and deletion of
        every record also requires allow_all=True.

        Args:
            table (str): Name of the table.
            filters (dict | None): Conditions selecting records.
            allow_all (bool): Permit unfiltered deletion.
            require_unique (bool): Require a unique equality filter.

        Returns:
            int: Number of records deleted.

        Raises:
            TypeError: If an argument has an invalid type.
            ValueError: If deletion conditions are invalid.
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
        Without filters, counts every record in the table.

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
