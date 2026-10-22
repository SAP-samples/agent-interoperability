"""DynamoDB-backed current configuration with a short in-process TTL cache."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from typing import Any

import boto3

from runtime.config import RuntimeConfig, default_config

logger = logging.getLogger(__name__)


class ConfigStore:
    def __init__(self, table: Any | None = None) -> None:
        self._table = table
        self._lock = threading.Lock()
        self._cached: RuntimeConfig | None = None
        self._cached_at = 0.0

    @property
    def table_name(self) -> str:
        return os.environ.get("AGENT_CONFIG_TABLE", "aws-agentcore-ias-config")

    @property
    def key(self) -> str:
        return os.environ.get("AGENT_CONFIG_KEY", "current")

    def _ttl(self) -> float:
        try:
            return max(0.0, float(os.environ.get("CONFIG_CACHE_TTL_SECONDS", "30")))
        except ValueError:
            return 30.0

    def _fallback(self) -> RuntimeConfig:
        return default_config()

    def _handle(self) -> Any:
        if self._table is None:
            self._table = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "eu-central-1")
            ).Table(self.table_name)
        return self._table

    def get(self) -> RuntimeConfig:
        if self._cached is not None and time.time() - self._cached_at < self._ttl():
            return self._cached
        return self.force_reload()

    def force_reload(self) -> RuntimeConfig:
        with self._lock:
            try:
                item = self._handle().get_item(Key={"config_id": self.key}).get("Item")
                cfg = RuntimeConfig.parse(json.loads(item["payload"])) if item else self._fallback()
            except Exception as exc:
                logger.warning(
                    "config read failed; using environment fallback: %s", type(exc).__name__
                )
                cfg = self._fallback()
            self._cached = cfg
            self._cached_at = time.time()
            logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))
            return cfg

    # ========================================================================
    # CONFIG WRITE PATH — not used by the MCP runtime (safe to skip when reading).
    #
    # The runtime only ever READS config (via get() / force_reload()); it never
    # writes, by design, so it can't corrupt its own settings. write() is the
    # opposite side: it validates, version-stamps, and persists a new config row
    # to DynamoDB, then refreshes the cache.
    #
    # Nothing in this folder calls it right now (the admin UI that used to call
    # it lives in a separate deployment). If you need to edit config from here,
    # the clean way is to add a small admin surface — a CLI command or a UI —
    # that calls write(), rather than hand-editing the DynamoDB row.
    # ========================================================================
    def write(self, config: RuntimeConfig, *, updated_by: str) -> RuntimeConfig:
        current = self.force_reload()
        stamped = RuntimeConfig.parse(
            {
                **config.to_dict(),
                "version": current.version + 1,
                "updated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "updated_by": updated_by.strip().lower(),
            }
        )
        self._handle().put_item(
            Item={
                "config_id": self.key,
                "payload": json.dumps(stamped.to_dict(), separators=(",", ":")),
                "version": stamped.version,
                "updated_at": stamped.updated_at,
                "updated_by": stamped.updated_by,
            }
        )
        with self._lock:
            self._cached = stamped
            self._cached_at = time.time()
        return stamped
    # ========================================================================
    # END CONFIG WRITE PATH
    # ========================================================================


_STORE: ConfigStore | None = None
_STORE_LOCK = threading.Lock()


def get_config_store() -> ConfigStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = ConfigStore()
    return _STORE
