"""
app/core/logging_config.py
==========================
Structured logging configuration for Laso.

Exports:
  LOGGING_CONFIG  — dict passed to logging.config.dictConfig
  configure_logging(settings) — call once at startup
"""
import logging
import logging.config
import os


def get_logging_config(environment: str) -> dict:
    """Build the logging config dict for the given environment."""
    is_prod = environment == "production"

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "detailed": {
                "format": (
                    "%(asctime)s - %(name)s - %(levelname)s - "
                    "[%(filename)s:%(lineno)d] - %(message)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "detailed",
                "filename": "logs/app.log",
                "maxBytes": 10_485_760,  # 10 MB
                "backupCount": 10,
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "ERROR",
                "formatter": "detailed",
                "filename": "logs/error.log",
                "maxBytes": 10_485_760,  # 10 MB
                "backupCount": 10,
            },
        },
        "loggers": {
            "": {  # root logger
                "handlers": ["console", "file"] if is_prod else ["console"],
                "level": "INFO",
                "propagate": False,
            },
            "app": {
                "handlers": (
                    ["console", "file", "error_file"] if is_prod else ["console"]
                ),
                "level": "DEBUG",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["console", "file"] if is_prod else ["console"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


def configure_logging(environment: str) -> None:
    """Ensure the logs directory exists and apply the logging config."""
    os.makedirs("logs", exist_ok=True)
    config = get_logging_config(environment)
    logging.config.dictConfig(config)
