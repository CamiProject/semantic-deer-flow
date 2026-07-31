"""Built-in subagent configurations."""

from .bash_agent import BASH_AGENT_CONFIG
from .general_purpose import GENERAL_PURPOSE_CONFIG
from .mysql_query import MYSQL_QUERY_CONFIG
from .mysql_validator import MYSQL_VALIDATOR_CONFIG

__all__ = [
    "GENERAL_PURPOSE_CONFIG",
    "BASH_AGENT_CONFIG",
    "MYSQL_QUERY_CONFIG",
    "MYSQL_VALIDATOR_CONFIG",
]

# Registry of built-in subagents
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
    "mysql-query": MYSQL_QUERY_CONFIG,
    "mysql-validator": MYSQL_VALIDATOR_CONFIG,
}
