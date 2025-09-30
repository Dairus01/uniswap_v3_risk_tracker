import redis
import logging
import json

from datetime import timedelta
from config import REDIS_HOST, REDIS_PORT, REDIS_DB


class RedisService:

    def __init__(self, host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB):
        # Sanitize env-derived values; Streamlit Cloud may not provide Redis
        self.host = host if host else None
        try:
            self.port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            self.port = None
        try:
            self.db = int(db) if db not in (None, "") else None
        except (TypeError, ValueError):
            self.db = None
        self.client = None
        self.logger = logging.getLogger("RedisService")
        self._fallback_cache = {}
        self.connect()

    def connect(self):
        # If Redis settings are missing, operate in in-memory fallback mode
        if not (self.host and self.port is not None and self.db is not None):
            self.logger.info("Redis not configured; using in-memory fallback cache")
            self.client = None
            return
        try:
            self.client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                socket_timeout=2,  # fast timeout-soft realtime friendly :)
                socket_connect_timeout=2,
                health_check_interval=30,
                retry_on_timeout=True,
            )

            if not self.client.ping():
                raise ConnectionError("redis pinf failed")

            self.logger.info("Redis connected successfylly")
        except (redis.ConnectionError, ConnectionError) as e:
            self.logger.error(f"Redis connection failed: {str(e)}")
            self.client = None

    def is_connected(self):
        try:
            return self.client and self.client.ping
        except redis.RedisError:
            return False

    def set(self, key: str, value, ttl: int = None):
        """
        Set value with optional TTL (seconds)
        """

        try:

            if self.is_connected():
                if ttl:
                    self.client.setex(key, time=timedelta(seconds=ttl), value=value)
                else:
                    self.client.set(key, value=value)
                return True

        except redis.RedisError as e:
            self.logger.warning(f"Redis set failed: {str(e)}")

        self._fallback_cache[key] = value
        return False

    def get(self, key: str):

        try:
            if self.is_connected:
                return self.client.get(key)
        except redis.RedisError as e:
            self.logger.warning(f"Redis get failed: {str(e)}")

        return self._fallback_cache.get(key)

    def set_json(self, key: str, data: dict, ttl: int = None):
        return self.set(key, json.dumps(data), ttl=ttl)

    def get_json(self, key: str):
        result = self.get(key)
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                self.logger.error(f"Invalid JSON data for key: {key}")
        return None

    def increment_counter(self, key: str, amount=1):
        """Atomically increment a counter"""
        try:
            if self.is_connected():
                return self.client.incrby(key, amount)
        except redis.RedisError as e:
            self.logger.warning(f"Redis increment failed: {str(e)}")

        # Fallback implementation
        current = int(self._fallback_cache.get(key, 0))
        new_value = current + amount
        self._fallback_cache[key] = new_value
        return new_value

    def get_metrics(self):
        """Get Redis service health metrics"""
        try:
            if self.is_connected():
                info = self.client.info()
                return {
                    "status": "connected",
                    "version": info.get("redis_version"),
                    "used_memory": info.get("used_memory_human"),
                    "ops_per_sec": info.get("instantaneous_ops_per_sec"),
                }
        except redis.RedisError:
            pass

        return {"status": "disconnected", "fallback_items": len(self._fallback_cache)}

    def close(self):
        """Clean up connections"""
        if self.client:
            self.client.close()


