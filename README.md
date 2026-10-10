# SQLite database: a beginner's guide

This repository is a small Python library for working with SQLite databases.
SQLite stores tables and records in a local file; you do not need to run a
separate database server. The included example tracks school materials.

You use the library from your own Python script through the `Database` class.
It can create tables, insert records, search, update, delete, count records,
inspect table structure, and make backups.

## Repository contents

| File | Purpose |
| --- | --- |
| `sqlite-database.py` | All configuration, SQL helpers, connection handling, and the public `Database` class. |
| `README.md` | This usage guide. |

The repository has no command-line menu, dependency file, package installer,
or committed test suite. Running `python sqlite-database.py` only loads its
definitions and exits: it does not initialize a database or show a menu.

## 1. Check your Python installation

Open a terminal in this repository's folder and run:

```powershell
python --version
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

The code requires Python 3.9 or newer because it uses `functools.cache`.
It also requires the SQLite library bundled with your Python interpreter to
be **3.37.0 or newer**, because it creates `STRICT` tables. A STRICT table
enforces its declared column types, allowing compatible value conversions.
The Python version and SQLite version are separate checks.

All imports belong to Python's standard library. There is nothing to install
with `pip`. The examples were verified with Python 3.12.10 and SQLite 3.49.1.
If `python` is unavailable, try `py` and use that command throughout the guide.

## 2. Understand the included database

The `databases` dictionary near the top of the source registers a database
named `materials`. Its `school` table has these columns:

| Column | Python value to supply | Rule |
| --- | --- | --- |
| `id` | Usually omit it | SQLite generates an integer primary key automatically. |
| `name` | A string, such as `"Pencil"` | Required; `None` is rejected. Empty strings are not explicitly forbidden. |
| `description` | A string or `None` | Optional; omitting it stores SQL NULL, meaning no value. |
| `quantity` | An integer, such as `10` | Required and must be zero or positive. |
| `price` | A number, such as `2.50` | Required and must be zero or positive. Stored as SQLite REAL. |

A **table** is a collection of records, a **row** is one record, and a
**column** is one field in that record. A **schema** describes the columns,
types, and rules of a table. The primary key identifies a row uniquely.

In the examples, Python strings use quotes, `None` means no value, and
`True` / `False` are Boolean values. Keyword arguments such as `limit=10`
name an option passed to a function. `db` is an object: calling `db.search()`
asks that object to perform a search.

With the original configuration, data goes to `materials.db` next to the
source file, and backups go to `backups/materials.db` next to it. These files
are created when you use the library; they are not included in the repo.

## 3. Run a complete first example

Save the following as `demo.py` **in the same folder as
`sqlite-database.py`**. You do not need to rename the existing source file.
Its hyphen prevents an ordinary statement such as
`from sqlite-database import Database`; `runpy` loads it by file path instead.

This demo redirects storage to a `demo-data` folder so you have a separate
place to experiment. The path changes affect this Python process only.

```python
from pathlib import Path
import runpy

# Find the library relative to this script, even from another working folder.
repo_dir = Path(__file__).resolve().parent
library = runpy.run_path(str(repo_dir / "sqlite-database.py"))
Database = library["Database"]

# A dictionary maps a key (such as "path") to a value (a file location).
info = library["databases"]["materials"]["database_info"]
info["path"] = repo_dir / "demo-data" / "materials.db"
info["backup_path"] = repo_dir / "demo-data" / "backups" / "materials.db"

# Constructing Database validates settings; initialize creates/checks tables.
db = Database("materials")
db.initialize()

# Insert one row. Keep its generated ID for later updates and deletion.
item_id = db.insert("school", {
    "name": "Pencil",
    "description": "HB pencil",
    "quantity": 10,
    "price": 2.50,
})
print("Inserted ID:", item_id)

rows = db.search(
    "school",
    columns=["id", "name", "quantity", "price"],
    filters={"id": item_id},
)
print("Found:", rows)

changed = db.update("school", {"price": 3.00}, filters={"id": item_id})
print("Updated rows:", changed)
print("Total rows:", db.count("school"))
print("Schema matches:", db.compare_schema("school")["matches"])

# Replaces only this demo's previous backup when the demo is run again.
print("Backup:", db.backup(overwrite=True))

# Delete the row created by this run. The backup still contains that row.
print("Deleted rows:", db.delete("school", filters={"id": item_id}))
```

Run it from the terminal:

```powershell
python demo.py
```

On a fresh demo database, the result begins like this:

```text
Inserted ID: 1
Found: [(1, 'Pencil', 10, 2.5)]
Updated rows: 1
Total rows: 1
Schema matches: True
```

The backup line prints your local backup path, followed by
`Deleted rows: 1`. IDs can increase on later runs even though the previous
row was deleted. Every insert creates a new record; names are not unique.

The examples below assume the same `library` and initialized `db` variables
are available. Put them in `demo.py` after `db.initialize()`, or repeat the
loading, configuration, and initialization steps in another script. To see
the Pencil in search results, place search examples after its insertion and
before the final deletion. If you append them after the complete demo, the
table is empty and searches return no rows.

## 4. Read and filter records

`search()` returns a **list of tuples**. A list holds multiple rows; each tuple
holds the values for one row, in the order of the selected columns. No matches
returns `[]`. By default, `columns="*"` selects all columns in table order
(`id`, `name`, `description`, `quantity`, `price` for `school`).

```python
rows = db.search(
    "school",
    columns=["name", "price"],
    filters={"price": {"lte": 5}, "quantity": {"gt": 0}},
    order_by="price",
    descending=True,
    limit=10,
    offset=0,
)
for name, price in rows:
    print(name, price)

print("In stock:", db.count("school", filters={"quantity": {"gt": 0}}))
```

A filter is a dictionary of column names and conditions. Direct values mean
equality, so `{"name": "Pencil"}` is equivalent to
`{"name": {"eq": "Pencil"}}`. Conditions on different columns are joined by
**AND**: all of them must match.

| Operator | Meaning | Example filter |
| --- | --- | --- |
| `eq` | Equal | `{"name": {"eq": "Pencil"}}` |
| `ne` | Not equal | `{"name": {"ne": "Pencil"}}` |
| `gt` / `gte` | Greater than / greater than or equal | `{"quantity": {"gte": 10}}` |
| `lt` / `lte` | Less than / less than or equal | `{"price": {"lte": 5}}` |
| `like` | Match a text pattern | `{"name": {"like": "Pen%"}}` |
| `in` | Match a value from a list or tuple | `{"id": {"in": [1, 2]}}` |
| `is_null` | Check whether a value is absent | `{"description": {"is_null": True}}` |

For `like`, `%` matches any number of characters and `_` matches one character.
For `is_null`, `False` means the column must contain a non-NULL value.
`{"description": None}` also checks for NULL; `ne: None` checks for non-NULL.
The `in` list must be nonempty and cannot contain `None`.

Each column accepts exactly one operator per filter. There is no OR or
nested-condition interface, and you cannot put both `gte` and `lte` in one
column's operator dictionary.

Search returns at most **20 rows by default**. Use `limit=None` for all matches,
or `limit=10, offset=10` to skip ten matches and retrieve the next ten.
Use `order_by="id"` for predictable pagination order. Without `order_by`, row
order is unspecified. `descending=True` requires an `order_by` column.
`count()` counts all matches and does not inherit the search limit.

## 5. Update and delete deliberately

The normal pattern is to use the ID returned by `insert()`, as in the demo.
Both `update()` and `delete()` return the number of affected rows. Zero means
nothing matched; it is not an exception.

By default, these methods require equality on a column that SQLite actually
marks as individually unique. In `school`, that column is `id`. A filter on
`name` is rejected even if it currently matches only one row. A filter such
as `{"id": {"gt": 1}}` does not satisfy the unique-equality requirement either.
Composite unique keys do not qualify as individually unique columns here.

For an intentional update of multiple matching rows, disable that requirement:

```python
changed = db.update(
    "school",
    {"price": 1.00},
    filters={"quantity": {"eq": 0}},
    require_unique=False,
)
print("Updated out-of-stock rows:", changed)
```

Filtered bulk deletion uses the same `require_unique=False` option.
To modify **every row**, both safeguards must be explicitly disabled:
`allow_all=True` and `require_unique=False`. For example,
`db.delete("school", allow_all=True, require_unique=False)` empties the table.
Use that only when you intend to delete all its records.

## 6. Inspect schemas and create backups

```python
schema = db.inspect_schema("school")
print("Columns:", list(schema["columns"]))
print("Strict table:", schema["options"]["strict"])

report = db.compare_schema("school")
print("Matches configuration:", report["matches"])
print("Differences:", report["differences"])
```

`inspect_schema()` reads the actual table's columns, stored CREATE TABLE SQL,
foreign keys, triggers, table options, and unique indexes.
`compare_schema()` compares that structure to the definition in the Python
configuration. These methods use read-only connections.

Initialization and data operations also check the schema. If a table differs
from the configuration, operations raise `ValueError`. Changing the Python
column definitions **does not migrate existing tables**. Changing
`schema_version` does not migrate them either. Back up the data and plan an
explicit migration when changing an existing schema.

Comparison is conservative: it checks normalized SQL as well as metadata,
so some equivalent SQL definitions can still be rejected. Extra triggers or
unique indexes can also produce mismatches; ordinary non-unique indexes are
not included in the comparison.

To back up the configured database, call `db.backup()`. It returns a
`pathlib.Path` and refuses to replace an existing backup, raising
`FileExistsError`. Use `db.backup(overwrite=True)` only when you want to replace
that backup, as the demo does.

Backups use SQLite's backup API, write to a temporary file, and run
`PRAGMA quick_check` before publishing the result. With `overwrite=False`,
publishing uses a hard link, so the destination filesystem must support
hard links. There is no restore method in this library.

## 7. Customize the configuration

The settings for each registered database are in `database_info`.
All eight fields are required, and extra fields are rejected:

| Setting | Expected value | What it does |
| --- | --- | --- |
| `path` | `pathlib.Path` | Main database location; write connections create parent folders. |
| `version` | Numeric dotted string, such as `"1.0"` | Validated metadata; not stored in SQLite by this code. |
| `schema_version` | Nonnegative integer | Validated metadata; does not set SQLite's user_version or run migrations. |
| `timeout` | Finite, nonnegative integer or float | Seconds to wait for a database lock; default is `10.0`. |
| `foreign_keys` | `True` or `False` | Turns foreign-key enforcement on or off on each connection. |
| `journal_mode` | Allowed uppercase string | Applied by `initialize()`; default is `"WAL"`. |
| `description` | Nonblank string | Validated descriptive metadata. |
| `backup_path` | `pathlib.Path` | Backup destination; must differ from the main database path. |

Journal modes accepted by the code are `DELETE`, `TRUNCATE`, `PERSIST`,
`MEMORY`, `WAL`, and `OFF`. SQLite must accept the requested mode during
initialization. WAL is a write-ahead log: SQLite may create `-wal` and
`-shm` companion files during use. Use the backup method to capture a
consistent database rather than copying just the main file while it is active.

Paths must be `Path` objects, not plain strings. The default paths are anchored
to the source file's folder. Custom relative paths are resolved against the
process's working folder; use explicit absolute paths when that matters.

You can add a table to the loaded configuration before creating it:

```python
library["databases"]["materials"]["tables"]["suppliers"] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "name": "TEXT NOT NULL UNIQUE",
}
db.create_table("suppliers")
print(db.compare_schema("suppliers")["matches"])
```

`create_table()` creates just that registered table and checks its schema;
it does not apply the initialization journal setting. `initialize()` creates
and checks every registered table and applies that setting.

To make customization persist across runs, edit the `databases` dictionary
in `sqlite-database.py`, or put the configuration changes in your application
setup. For another database, add a separate entry containing both
`database_info` and `tables`, then pass its registered name to `Database`.

Column definitions are trusted SQL snippets. The library quotes column/table
names and passes record values as SQL parameters, but it does not sanitize
schema definition strings. Keep schema configuration under your control.

## 8. Public method reference

| Method | Result |
| --- | --- |
| `Database(name)` | A database wrapper; validates settings without opening the database. |
| `db.initialize()` | `None`; creates/checks all registered tables and applies journal mode. |
| `db.create_table(table)` | `None`; creates/checks one registered table. |
| `db.insert(table, values)` | Inserted row ID. |
| `db.search(table, columns="*", filters=None, order_by=None, descending=False, limit=20, offset=0)` | List of row tuples. |
| `db.update(table, values, filters=None, allow_all=False, require_unique=True)` | Number of affected rows. |
| `db.delete(table, filters=None, allow_all=False, require_unique=True)` | Number of deleted rows. |
| `db.count(table, filters=None)` | Number of matching rows. |
| `db.inspect_schema(table)` | Dictionary describing the actual SQLite schema. |
| `db.compare_schema(table)` | Dictionary with `database`, `table`, `matches`, and `differences`. |
| `db.backup(overwrite=False)` | Path of the completed backup. |

## 9. How the code fits together

A normal call flows through these parts of `sqlite-database.py`:

1. **Public interface:** `Database` keeps the registered name and forwards
   method arguments to the operation helpers. The standalone
   `initialize_database()` and `backup_database()` implement initialization
   and backups.
2. **Configuration checks:** `_validate_config()` checks the database settings;
   `_check_columns()` checks registered table and column names.
   `_check_sqlite_version()` checks SQLite support and caches a successful check.
3. **SQL construction:** `_register_query()` is a decorator (a function that
   registers another function) populating `query_builders` for CREATE,
   INSERT, SELECT, UPDATE, DELETE, and COUNT. The `_build_*` functions return
   SQL and a tuple of parameters. `_where()` builds filters and
   `_quote_identifier()` quotes names.
4. **Connections and transactions:** `_execute()` runs the SQL through
   `_ConnectionManager`, a context manager used with `with`. It closes the
   connection after each operation and commits successful transactions or
   rolls back failed ones. Writes start `BEGIN IMMEDIATE` before schema and
   uniqueness checks. Search and count also open read-write connections.
5. **Schema inspection:** the `_get_*` helpers and `_table_exists()` read SQLite
   metadata. `_inspect_schema()` assembles it. `_compare_schema()` builds the
   expected table in a separate in-memory database and compares structures;
   `_sql_tokens()` normalizes SQL conservatively.
   `_require_matching_schema()` rejects differences.
6. **Write safeguards:** `_validate_write_filters()` checks bulk-operation
   options. `_get_unique_columns()` and `_require_unique_filter()` inspect
   physical uniqueness and check that the filter matches at most one row.
7. **Errors and backups:** `_handle_errors()` logs SQLite errors and re-raises
   them. `backup_database()` uses a read-only source connection and publishes
   an integrity-checked backup file.

Names starting with `_` are implementation helpers. Use the `Database`
methods in your own scripts. Each method call manages its own connection;
there is no public API for grouping several calls into a single transaction.
The library also has no public join, bulk-insert, migration, or restore API.

## 10. Troubleshooting

| Symptom | Explanation and next step |
| --- | --- |
| Running the source file prints nothing | It defines the library. Create and run `demo.py` to use it. |
| Import or file-not-found error | Use the path-based loader shown above and keep `demo.py` beside the source. |
| Unsupported SQLite version | Check `sqlite3.sqlite_version` using the same Python interpreter that runs your script. Use an interpreter bundled with SQLite 3.37.0 or newer. |
| Unknown database, table, or column | Names must match the registered `databases` dictionary. |
| Missing table / schema mismatch | Call `initialize()` before data operations. If an existing table differs, inspect `compare_schema()` and plan a migration. |
| Unique equality filter required | Use the saved row ID, or explicitly choose `require_unique=False` for an intended bulk operation. |
| Only 20 search results | Set a larger `limit`, paginate, or use `limit=None`. |
| `sqlite3.IntegrityError` | Check required fields, nonnegative quantity/price, and uniqueness constraints. |
| `sqlite3.OperationalError` | Read the message: the file may be inaccessible or another writer may hold a lock beyond the configured timeout. |
| `FileExistsError` on backup | Preserve that backup with a different configured path, or intentionally use `overwrite=True`. |
| Filesystem error on backup | Check folder permissions and hard-link support; the default publishing path requires hard links. |

Errors are raised to your application. SQLite errors are also logged through
Python's `logging` module, so a traceback may appear before you handle the
exception. To handle invalid input without ending your script, catch the
specific exception around the operation:

```python
import sqlite3

try:
    db.insert("school", {"name": "Pencil", "quantity": -1, "price": 2.50})
except sqlite3.IntegrityError as error:
    print("Could not save the item:", error)
```

That example deliberately violates the quantity rule. The failed insertion
is rolled back; it does not leave an invalid row in the database. A
transaction is a group of changes that either succeeds together or is
undone (rolled back) if an error occurs. Here, each write method runs its
own transaction.
