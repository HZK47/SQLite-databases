
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

    Uses the SQLite version loaded by Python, not the Python interpreter
    version. A successful check is cached for the lifetime of the process.

    Returns:
        None: If the SQLite version meets the minimum requirement.

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
        sqlite3.Error: Re-raises any SQLite exception after logging its traceback.

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
    An action can be registered only once, and its name must use A-Z.

    Args:
        action (str): Uppercase SQL action name, such as 'SELECT'.

    Returns:
        callable: A decorator that registers and returns the original function.

    Raises:
        TypeError: If action is not a string or the decorated object is not callable.
        ValueError: If action is empty, contains invalid characters, or is already registered.

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
    Validate the database, table, and column names registered in Python.

    This checks the configuration dictionary only, including basic column
    definition types. It does not inspect or validate the physical schema.

    Args:
        database (str): Name of a database in the databases dictionary.
        table (str | None): Table to validate, or None to check only the database.
        columns (str | iterable[str] | None): Column names to check; an empty
            iterable or None validates only the database and table.

    Returns:
        None: If every requested name is registered.

    Raises:
        TypeError: If a name, table definition, or column collection has an invalid type.
        ValueError: If a database, table, or column is unknown, or a definition is empty.

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

    Double quotes inside the identifier are escaped by doubling them.
    Leading and trailing spaces, when present, are preserved.

    Args:
        name (str): Table or column name to quote.

    Returns:
        str: The SQL identifier enclosed in double quotes.

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
        None: If all supported configuration settings are valid.

    Raises:
        TypeError: If a field has an incorrect Python type.
        ValueError: If a field is missing, unknown, or has a disallowed value.

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

    Supports implicit equality, explicit eq/ne/gt/gte/lt/lte/like, IN
    lists, and IS NULL / IS NOT NULL checks. SQL values are never
    interpolated directly into the statement.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        filters (dict | None): Mapping of columns to a direct value or a
            one-operator dictionary, such as {'price': {'gte': 10}}.
            None or an empty dictionary means no filtering.

    Returns:
        tuple[str, tuple]: The SQL clause and parameters in placeholder order.
            Returns ('', ()) if no conditions are supplied.

    Raises:
        TypeError: If filters or an operator value has an invalid type.
        ValueError: If a column or operator is unknown, or a filter is invalid.

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

    By default, a filtered write must use equality on a single-column
    PRIMARY KEY or UNIQUE field declared in the Python table definition.
    These declarations are not yet verified against the physical SQLite schema.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the table being modified.
        filters (dict | None): Conditions determining which records are affected.
        allow_all (bool): Whether an unfiltered modification is permitted.
        require_unique (bool): Whether an equality filter on a unique field is required.

    Returns:
        tuple[str, tuple]: A WHERE clause and its SQL parameters.

    Raises:
        TypeError: If a safety option or filter has an incorrect type.
        ValueError: If the operation lacks a required filter or unique condition.

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
@_handle_errors
def _connect_database(database):
    """
    Open and configure a SQLite connection for a registered database.

    Creates parent directories when missing, checks the SQLite version,
    and applies foreign-key enforcement independently for this connection.
    The caller must close the returned connection after use.

    Args:
        database (str): Name of the database configuration to use.

    Returns:
        sqlite3.Connection: An open connection with foreign keys configured.

    Raises:
        TypeError: If a configuration entry has an invalid type.
        ValueError: If a registered configuration is invalid.
        sqlite3.Error: If opening or configuring SQLite fails.
        OSError: If the database directory cannot be created.

    Usage:
        with closing(_connect_database("materials")) as connection:
            ...
    """
    _check_sqlite_version()
    _validate_config(database)

    database_info = databases[database]["database_info"]
    database_path = database_info["path"]
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path, timeout=database_info["timeout"])

    try:
        foreign_keys = "ON" if database_info["foreign_keys"] else "OFF"
        connection.execute(f"PRAGMA foreign_keys = {foreign_keys}")
        result = connection.execute("PRAGMA foreign_keys").fetchone()

        if result is None or bool(result[0]) != database_info["foreign_keys"]:
            raise sqlite3.DatabaseError(
                f"Could not configure foreign keys for database '{database}'. Expected: {foreign_keys}."
            )

        return connection

    except Exception:
        connection.close()
        raise


@_handle_errors
def initialize_database(database):
    """
    Apply journal settings and create the tables registered for a database.

    Configures journal_mode and creates missing STRICT tables. Existing tables
    are not modified or checked for structural compatibility. This function
    does not perform schema migrations or assign the configured schema_version.

    Args:
        database (str): Name of the registered database to initialize.

    Returns:
        None: If journal setup and table creation complete successfully.

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

    with closing(_connect_database(database)) as connection:
        result = connection.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()
        actual_mode = result[0].upper() if result else None

        if actual_mode != journal_mode:
            raise sqlite3.DatabaseError(
                f"Could not set journal mode '{journal_mode}' for database '{database}'. "
                f"SQLite reported {actual_mode!r}."
            )

        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for table in table_definitions:
                query, parameters = query_builders["CREATE"](database, table)
                connection.execute(query, parameters)


# SQL query builders
@_register_query("CREATE")
def _build_create(database, table):
    """
    Build a STRICT CREATE TABLE statement from the registered schema.

    Uses the column names and SQL definitions declared for the table.
    IF NOT EXISTS avoids errors for an existing table, but it does not
    verify whether that table has the expected structure.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table to create.

    Returns:
        tuple[str, tuple]: The CREATE TABLE statement and an empty parameter tuple.

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

    Uses only the columns supplied in values. SQLite applies NOT NULL,
    STRICT, CHECK, UNIQUE, and other constraints when the query executes.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        values (dict): Column names mapped to values to insert.

    Returns:
        tuple[str, tuple]: The INSERT statement and values in placeholder order.

    Raises:
        TypeError: If values is not a dictionary.
        ValueError: If values is empty or includes an unknown column.

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
    Build a SELECT statement with optional filtering, sorting, and pagination.

    Selected columns are validated against the registered schema. Filter values,
    LIMIT, and OFFSET are passed separately through SQLite placeholders.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        columns (str | list | tuple): '*' for all columns, one name, or an ordered collection.
        filters (dict | None): Optional conditions interpreted by _where().
        order_by (str | None): Column used for sorting; None means no explicit order.
        descending (bool): Whether order_by should use DESC rather than ASC.
        limit (int | None): Maximum records; None means no limit and 0 returns none.
        offset (int): Number of matching rows to skip, normally with ORDER BY.

    Returns:
        tuple[str, tuple]: The SELECT statement and parameters in SQL order.

    Raises:
        TypeError: If an argument has an incorrect Python type.
        ValueError: If a column, filter, limit, or offset is invalid.

    Usage:
        query, parameters = query_builders["SELECT"](
            "materials", "school", columns=["name", "price"],
            filters={"quantity": {"gte": 10}}, order_by="price", descending=True, limit=20, offset=5
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
    Build an UPDATE statement with checked conditions and parameterized values.

    Updates all matching records if require_unique is disabled. Unfiltered
    updates require both allow_all=True and require_unique=False.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        values (dict): Column names mapped to new values.
        filters (dict | None): Conditions identifying records to modify.
        allow_all (bool): Explicitly permit updating every record when no filter exists.
        require_unique (bool): Require equality on a declared unique column by default.

    Returns:
        tuple[str, tuple]: The UPDATE statement and parameters, with SET values first.

    Raises:
        TypeError: If values or a safety option has an incorrect type.
        ValueError: If values are empty or the update conditions are not allowed.

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
    Build a DELETE statement with safety checks for matching records.

    Deletes all matching records if require_unique is disabled. Unfiltered
    deletion requires both allow_all=True and require_unique=False.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        filters (dict | None): Conditions selecting records to delete.
        allow_all (bool): Explicitly permit deleting all rows when no filters exist.
        require_unique (bool): Require equality on a declared unique column by default.

    Returns:
        tuple[str, tuple]: The DELETE statement and the WHERE parameters.

    Raises:
        TypeError: If a safety option or filter has an incorrect type.
        ValueError: If the deletion conditions are not allowed.

    Usage:
        query, parameters = query_builders["DELETE"]("materials", "school", filters={"id": 1})
    """
    where_clause, parameters = _validate_write_filters(database, table, filters, allow_all, require_unique)
    return f"DELETE FROM {_quote_identifier(table)}{where_clause}", parameters


@_register_query("COUNT")
def _build_count(database, table, filters=None):
    """
    Build a SELECT COUNT(*) statement for matching rows.

    Unlike SELECT, this operation returns the total number of matches
    without retrieving every matching record.

    Args:
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        filters (dict | None): Conditions restricting which records are counted.

    Returns:
        tuple[str, tuple]: The COUNT statement and WHERE parameters.

    Raises:
        TypeError: If filters has an invalid type.
        ValueError: If the table or filter columns are not registered.

    Usage:
        query, parameters = query_builders["COUNT"]("materials", "school", filters={"quantity": {"gte": 10}})
    """
    where_clause, parameters = _where(database, table, filters)
    return f"SELECT COUNT(*) FROM {_quote_identifier(table)}{where_clause}", parameters


# Statement execution
@_handle_errors
def _execute(action, database, table, **kwargs):
    """
    Build and execute one registered SQL operation on a managed connection.

    The query builder produces SQL plus parameters. This function runs the
    query, retrieves the appropriate result, and commits or rolls back the
    transaction before closing the connection.

    Args:
        action (str): Registered query action, such as CREATE, INSERT, or SELECT.
        database (str): Name of the registered database.
        table (str): Name of the registered table.
        **kwargs: Additional options accepted by the selected query builder.

    Returns:
        int: The last inserted row ID for INSERT, affected row count for
            UPDATE/DELETE, or number of matches for COUNT.
        list[tuple]: Records returned by SELECT, with columns in requested order.
        None: For CREATE or operations without a defined result.

    Raises:
        TypeError: If an argument has an invalid Python type.
        ValueError: If action or an argument is invalid.
        sqlite3.Error: If SQLite fails to execute or commit the operation.

    Usage:
        _execute("CREATE", "materials", "school")
        _execute("INSERT", "materials", "school", values={"name": "Pencil", "quantity": 10, "price": 2.50})
    """
    if not isinstance(action, str):
        raise TypeError(f"SQL action must be a string, received {type(action).__name__}.")
    if action not in query_builders:
        raise ValueError(f"Unknown SQL action '{action}'. Available actions: {sorted(query_builders)}.")

    query, parameters = query_builders[action](database, table, **kwargs)

    with closing(_connect_database(database)) as connection:
        with connection:
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

    Stores the database name and provides methods for creating tables,
    inserting, searching, updating, deleting, and counting records.

    All SQL statements are generated and executed by the library's
    internal functions. Users do not need to write SQL directly.

    Args:
        name (str): Name of a database registered in 'databases'.

    Raises:
        TypeError: If the database configuration contains invalid types.
        ValueError: If the database is unknown or its configuration is invalid.

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
        Existing table structures are not modified or migrated.

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

        Uses the column definitions stored in the database configuration.
        Existing table structures are not modified.

        Args:
            table (str): Name of the table to create.

        Returns:
            None: If the operation succeeds.

        Raises:
            ValueError: If the table is not registered.
            sqlite3.Error: If SQLite cannot create the table.

        Usage:
            db.create_table("school")
        """
        return _execute("CREATE", self.name, table)

    def insert(self, table, values):
        """
        Insert a new record into a registered table.

        Column names and values must be supplied as a dictionary.
        SQLite validates the data against the table constraints.

        Args:
            table (str): Name of the table.
            values (dict): Column names mapped to their values.

        Returns:
            int: SQLite row ID of the inserted record.

        Raises:
            TypeError: If values is not a dictionary.
            ValueError: If values is empty or contains invalid columns.
            sqlite3.Error: If SQLite cannot insert the record.

        Usage:
            db.insert("school", {"name": "Pencil", "quantity": 10, "price": 2.50})
        """
        return _execute("INSERT", self.name, table, values=values)

    def search(self, table, columns="*", filters=None, order_by=None, descending=False, limit=20, offset=0):
        """
        Retrieve records using optional filters, sorting, and pagination.

        Supports selecting specific columns, filtering with comparison
        operators, sorting by a column, and limiting returned records.

        Args:
            table (str): Name of the table to search.
            columns (str | list | tuple): Columns to retrieve, or "*" for all.
            filters (dict | None): Conditions used to select records.
            order_by (str | None): Column used to sort the results.
            descending (bool): Whether to sort in descending order.
            limit (int | None): Maximum number of records, or None for all.
            offset (int): Number of matching records to skip.

        Returns:
            list[tuple]: Matching records in the requested column order.

        Raises:
            TypeError: If an argument has an incorrect type.
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

        By default, requires an equality filter on a registered unique
        column. Multiple-record updates require require_unique=False,
        and unfiltered updates also require allow_all=True.

        Args:
            table (str): Name of the table.
            values (dict): Column names mapped to their new values.
            filters (dict | None): Conditions identifying records to update.
            allow_all (bool): Whether updates without filters are permitted.
            require_unique (bool): Whether a unique equality filter is required.

        Returns:
            int: Number of records affected by the UPDATE operation.

        Raises:
            TypeError: If values or a safety option has an invalid type.
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

        By default, requires an equality filter on a registered unique
        column. Deleting multiple records requires require_unique=False,
        and deleting every record also requires allow_all=True.

        Args:
            table (str): Name of the table.
            filters (dict | None): Conditions identifying records to delete.
            allow_all (bool): Whether deletion without filters is permitted.
            require_unique (bool): Whether a unique equality filter is required.

        Returns:
            int: Number of records deleted.

        Raises:
            TypeError: If a filter or safety option has an invalid type.
            ValueError: If the deletion conditions are not permitted.
            sqlite3.Error: If SQLite cannot complete the deletion.

        Usage:
            db.delete("school", filters={"id": 1})
        """
        return _execute("DELETE", self.name, table, filters=filters,
                        allow_all=allow_all, require_unique=require_unique)

    def count(self, table, filters=None):
        """
        Count records matching optional filtering conditions.

        Returns the number of matching records without retrieving
        their complete data. Without filters, counts the entire table.

        Args:
            table (str): Name of the table.
            filters (dict | None): Conditions used to select records.

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
