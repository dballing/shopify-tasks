#!/usr/bin/env python3
"""Shared foundation for the shopify-tasks mini-projects.

Task modules should import from here rather than reaching into submodules::

    from shopifylib import task, gid
    from shopifylib.errors import TaskError
"""
from __future__ import annotations

from .client import ShopifyClient, check_user_errors
from .config import AppConfig, Shop, load_config
from .errors import (
    AccessDeniedError,
    AuthError,
    ConfigError,
    GraphQLError,
    ShopifyTasksError,
    TaskError,
    TransportError,
    UserError,
    WriteBlocked,
)
from .paging import paginate
from .task import TaskContext, TaskSpec, task

__version__ = "0.1.0"

__all__ = [
    "AccessDeniedError",
    "AppConfig",
    "AuthError",
    "ConfigError",
    "GraphQLError",
    "ShopifyClient",
    "ShopifyTasksError",
    "Shop",
    "TaskContext",
    "TaskError",
    "TaskSpec",
    "TransportError",
    "UserError",
    "WriteBlocked",
    "check_user_errors",
    "load_config",
    "paginate",
    "task",
]
