import math
import time
import logging
import json

from redis_service import RedisService
from config import RISK_CONFIG


class RiskEngine:
    def __init__(self, redis_service: RedisService = None):
        self.redis = redis_service or RedisService()
        self.logger = logging.getLogger("RiskEngine")
        self.config = RISK_CONFIG
        self._init_state()

    def _init_state(self):
        self.position_states = {}

    def calculate_tvl_volatility(self, symbol: str, current_tvl: float) -> float:
        prev_key = f"risk:{symbol}:tvl_prev"
        vol_key = f"risk:{symbol}:tvl_vol"

        prev_tvl = self.redis.get(prev_key)
        prev_vol = self.redis.get(vol_key) or 0

        if prev_tvl and float(prev_tvl) > 0:
            prev_tvl = float(prev_tvl)
            returns = math.log(current_tvl / prev_tvl)

            decay = self.config["volatility_decay"]
            new_vol = math.sqrt(decay * returns**2 + (1 - decay) * float(prev_vol) ** 2)
            self.redis.set(vol_key, new_vol)
            volatility = new_vol

        else:
            volatility = 0
        self.redis.set(prev_key, current_tvl)
        return volatility * 100
