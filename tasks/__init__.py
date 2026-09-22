"""Task modules, one mini-project per file.

Every module here is imported at dispatcher startup so its ``@task`` decorator
registers. A module that fails to import is reported as a warning and skipped,
so one broken task cannot take the CLI down with it.
"""
