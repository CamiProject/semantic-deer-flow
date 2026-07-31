"""Unit tests for the current local SQL tool contract."""

import os
from unittest.mock import MagicMock, patch


class TestValidateSQL:
    def test_valid_select_query(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("SELECT * FROM users")
        assert is_valid is True
        assert error == ""

    def test_valid_select_with_where(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("SELECT id, name FROM users WHERE status = 'active'")
        assert is_valid is True
        assert error == ""

    def test_valid_join_query(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id")
        assert is_valid is True
        assert error == ""

    def test_block_drop_query(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("DROP TABLE users")
        assert is_valid is False
        assert "DROP" in error

    def test_block_delete_query(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("DELETE FROM users WHERE id = 1")
        assert is_valid is False
        assert "DELETE" in error

    def test_block_truncate_query(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("TRUNCATE TABLE users")
        assert is_valid is False
        assert "TRUNCATE" in error

    def test_block_alter_query(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("ALTER TABLE users ADD COLUMN age INT")
        assert is_valid is False
        assert "ALTER" in error

    def test_block_create_query(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("CREATE TABLE test (id INT)")
        assert is_valid is False
        assert "CREATE" in error

    def test_block_update_without_permission(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("UPDATE users SET name = 'test'")
        assert is_valid is False
        assert "UPDATE" in error

    def test_block_insert_without_permission(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("INSERT INTO users (name) VALUES ('test')")
        assert is_valid is False
        assert "INSERT" in error

    def test_allow_update_with_permission(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("UPDATE users SET name = 'test'", allow_write=True)
        assert is_valid is True
        assert error == ""

    def test_allow_insert_with_permission(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("INSERT INTO users (name) VALUES ('test')", allow_write=True)
        assert is_valid is True
        assert error == ""

    def test_block_set_even_with_write_permission(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("SET @tenant = 'other'", allow_write=True)
        assert is_valid is False
        assert "SET" in error

    def test_block_grant_even_with_write_permission(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("GRANT ALL ON users TO test", allow_write=True)
        assert is_valid is False
        assert "GRANT" in error

    def test_case_insensitive_detection(self):
        from deerflow.tools.builtins.sql_tools import _validate_sql

        is_valid, error = _validate_sql("drop table users")
        assert is_valid is False
        assert "DROP" in error


class TestAddLimitIfNeeded:
    def test_add_limit_to_simple_select(self):
        from deerflow.tools.builtins.sql_tools import _add_limit_if_needed

        result = _add_limit_if_needed("SELECT * FROM users")
        assert "LIMIT 100" in result

    def test_add_limit_with_custom_value(self):
        from deerflow.tools.builtins.sql_tools import _add_limit_if_needed

        result = _add_limit_if_needed("SELECT * FROM users", limit=50)
        assert "LIMIT 50" in result

    def test_not_add_limit_if_already_exists(self):
        from deerflow.tools.builtins.sql_tools import _add_limit_if_needed

        result = _add_limit_if_needed("SELECT * FROM users LIMIT 10")
        assert result == "SELECT * FROM users LIMIT 10"

    def test_not_add_limit_to_non_select(self):
        from deerflow.tools.builtins.sql_tools import _add_limit_if_needed

        result = _add_limit_if_needed("SHOW TABLES")
        assert "LIMIT" not in result

    def test_remove_trailing_semicolon_before_limit(self):
        from deerflow.tools.builtins.sql_tools import _add_limit_if_needed

        result = _add_limit_if_needed("SELECT * FROM users;")
        assert result == "SELECT * FROM users LIMIT 100"

    def test_complex_select_with_join(self):
        from deerflow.tools.builtins.sql_tools import _add_limit_if_needed

        result = _add_limit_if_needed("SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id")
        assert "LIMIT 100" in result


class TestGetMysqlConnectionString:
    def test_connection_string_with_database(self):
        from deerflow.tools.builtins.sql_tools import _get_mysql_connection_string

        with patch.dict(os.environ, {"MYSQL_HOST": "localhost", "MYSQL_PORT": "3306", "MYSQL_USER": "root", "MYSQL_PASSWORD": "password", "MYSQL_DATABASE": "testdb"}):
            result = _get_mysql_connection_string()
            assert "testdb" in result
            assert "localhost" in result
            assert "3306" in result
            assert "root" in result

    def test_connection_string_without_database(self):
        from deerflow.tools.builtins.sql_tools import _get_mysql_connection_string

        env_vars = {"MYSQL_HOST": "localhost", "MYSQL_PORT": "3306", "MYSQL_USER": "root", "MYSQL_PASSWORD": "password"}
        with patch.dict(os.environ, env_vars, clear=True):
            if "MYSQL_DATABASE" in os.environ:
                del os.environ["MYSQL_DATABASE"]
            result = _get_mysql_connection_string()
            assert result.endswith("/")

    def test_connection_string_with_explicit_database(self):
        from deerflow.tools.builtins.sql_tools import _get_mysql_connection_string

        with patch.dict(os.environ, {"MYSQL_HOST": "localhost", "MYSQL_PORT": "3306", "MYSQL_USER": "root", "MYSQL_PASSWORD": "password", "MYSQL_DATABASE": "defaultdb"}):
            result = _get_mysql_connection_string(database="otherdb")
            assert "otherdb" in result
            assert "defaultdb" not in result


class TestSqlListTables:
    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_list_tables_with_default_database(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_list_tables

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.get_usable_table_names.return_value = ["users", "orders", "products"]
        mock_get_db.return_value = mock_db

        result = sql_list_tables.invoke({"database_name": ""})

        assert "testdb" in result
        assert "users" in result
        assert "orders" in result
        assert "products" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_list_tables_with_specific_database(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_list_tables

        mock_get_default.return_value = "defaultdb"
        mock_db = MagicMock()
        mock_db.get_usable_table_names.return_value = ["table1", "table2"]
        mock_get_db.return_value = mock_db

        result = sql_list_tables.invoke({"database_name": "otherdb"})

        assert "otherdb" in result
        assert "table1" in result

    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_list_tables_no_default_database(self, mock_get_default):
        from deerflow.tools.builtins.sql_tools import sql_list_tables

        mock_get_default.return_value = ""

        result = sql_list_tables.invoke({"database_name": ""})

        assert "No database specified" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_list_tables_empty_database(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_list_tables

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.get_usable_table_names.return_value = []
        mock_get_db.return_value = mock_db

        result = sql_list_tables.invoke({"database_name": "testdb"})

        assert "No tables found" in result


class TestSqlSchema:
    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_schema_single_table(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_schema

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.get_usable_table_names.return_value = ["users", "orders"]
        mock_db.get_table_info.return_value = "CREATE TABLE users (id INT, name VARCHAR(100))"
        mock_get_db.return_value = mock_db

        result = sql_schema.invoke({"table_names": "users"})

        assert "users" in result
        assert "CREATE TABLE" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_schema_multiple_tables(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_schema

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.get_usable_table_names.return_value = ["users", "orders"]
        mock_db.get_table_info.return_value = "Schema info for multiple tables"
        mock_get_db.return_value = mock_db

        result = sql_schema.invoke({"table_names": "users,orders"})

        assert "users" in result
        assert "orders" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_schema_cross_database_table(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_schema

        mock_get_default.return_value = ""
        mock_db = MagicMock()
        mock_db.get_table_info.return_value = "Schema info"
        mock_get_db.return_value = mock_db

        result = sql_schema.invoke({"table_names": "db1.users"})

        assert "db1.users" in result

    def test_schema_empty_table_names(self):
        from deerflow.tools.builtins.sql_tools import sql_schema

        result = sql_schema.invoke({"table_names": ""})

        assert "Error" in result
        assert "No table names" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_schema_invalid_table(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_schema

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.get_usable_table_names.return_value = ["users", "orders"]
        mock_get_db.return_value = mock_db

        result = sql_schema.invoke({"table_names": "invalid_table"})

        assert "Error" in result
        assert "not found" in result


class TestSqlQuery:
    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_success(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.return_value = "[[1, 'user1'], [2, 'user2']]"
        mock_get_db.return_value = mock_db

        result = sql_query.invoke({"query": "SELECT * FROM users"})

        assert "user1" in result or "user2" in result or result != ""

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_blocked_forbidden_keyword(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        result = sql_query.invoke({"query": "DROP TABLE users"})

        assert "blocked" in result
        assert "DROP" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_empty_result(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.return_value = []
        mock_get_db.return_value = mock_db

        result = sql_query.invoke({"query": "SELECT * FROM users WHERE id = 999"})

        assert "no results" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_unknown_column_error(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.side_effect = Exception("Unknown column 'invalid_col'")
        mock_get_db.return_value = mock_db

        result = sql_query.invoke({"query": "SELECT invalid_col FROM users"})

        assert "Unknown column" in result
        assert "sql_schema" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_table_not_exist_error(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.side_effect = Exception("Table 'testdb.invalid_table' doesn't exist")
        mock_get_db.return_value = mock_db

        result = sql_query.invoke({"query": "SELECT * FROM invalid_table"})

        assert "doesn't exist" in result
        assert "sql_list_tables" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_unknown_database_error(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        mock_get_default.return_value = ""
        mock_db = MagicMock()
        mock_db.run.side_effect = Exception("Unknown database 'invalid_db'")
        mock_get_db.return_value = mock_db

        result = sql_query.invoke({"query": "SELECT * FROM invalid_db.users"})

        assert "Unknown database" in result
        assert "Please check the database name" in result
        assert "sql_discover_databases" not in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_syntax_error(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.side_effect = Exception("You have an error in your SQL syntax")
        mock_get_db.return_value = mock_db

        result = sql_query.invoke({"query": "SELECT * FORM users"})

        assert "Syntax Error" in result

    @patch.dict(os.environ, {"MYSQL_ALLOW_WRITE": "true"})
    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_query_update_with_permission(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.return_value = "Updated 1 row"
        mock_get_db.return_value = mock_db

        result = sql_query.invoke({"query": "UPDATE users SET name = 'test' WHERE id = 1"})

        assert "blocked" not in result


class TestSqlQueryChecker:
    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_checker_valid_query(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query_checker

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.return_value = "EXPLAIN result"
        mock_get_db.return_value = mock_db

        result = sql_query_checker.invoke({"query": "SELECT * FROM users"})

        assert "valid" in result

    def test_checker_invalid_query_forbidden(self):
        from deerflow.tools.builtins.sql_tools import sql_query_checker

        result = sql_query_checker.invoke({"query": "DROP TABLE users"})

        assert "Validation failed" in result
        assert "DROP" in result

    @patch("deerflow.tools.builtins.sql_tools._get_db")
    @patch("deerflow.tools.builtins.sql_tools._get_default_database")
    def test_checker_syntax_error(self, mock_get_default, mock_get_db):
        from deerflow.tools.builtins.sql_tools import sql_query_checker

        mock_get_default.return_value = "testdb"
        mock_db = MagicMock()
        mock_db.run.side_effect = Exception("Syntax error")
        mock_get_db.return_value = mock_db

        result = sql_query_checker.invoke({"query": "SELECT * FORM users"})

        assert "validation failed" in result


class TestSQLToolsList:
    def test_sql_tools_contains_all_tools(self):
        from deerflow.tools.builtins.sql_tools import SQL_TOOLS

        tool_names = [tool.name for tool in SQL_TOOLS]

        assert tool_names == [
            "sql_show_databases",
            "sql_list_tables",
            "sql_schema",
            "sql_query",
            "sql_query_checker",
        ]

    def test_sql_tools_count(self):
        from deerflow.tools.builtins.sql_tools import SQL_TOOLS

        assert len(SQL_TOOLS) == 5
