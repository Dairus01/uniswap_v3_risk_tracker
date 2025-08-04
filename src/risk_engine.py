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

    def interpret_capital_efficiency(
            self, 
            symbol: str, 
            current_tvl: float, 
            volume: float
        ) -> str:
        capital_efficiency = volume / current_tvl
        
        if capital_efficiency >= 0.5:    
            return "██████████ (Elite)"
        elif capital_efficiency >= 0.1:  
            return "█████████░ (High)"
        elif capital_efficiency >= 0.03: 
            return "████████░░ (Strong)"
        elif capital_efficiency >= 0.01:
            return "██████░░░░ (Medium)"
        elif capital_efficiency >= 0.003:
            return "████░░░░░░ (Low)"
        elif capital_efficiency >= 1e-4: 
            return "██░░░░░░░░ (Very Low)"
        else:                           
            return "░░░░░░░░░░ (Idle)"
        
    def calculate_fee_income_risk(self, volume: float, tvl: float, fee_tier: float) -> dict:
        daily_fees = volume * (fee_tier / 100)
        annual_fees = daily_fees * 365

        apr = annual_fees / tvl
        if apr >= 0.10:
            return {
                "apr": f"{apr:.2%}",
                "risk_level": "🟢 Low Risk",
                "description": "💰 Strong fee generation — attractive for LPs",
                "color": "#2ecc71",  # green
            }
        elif apr >= 0.05:
            return {
                "apr": f"{apr:.2%}",
                "risk_level": "🟡 Medium Risk",
                "description": "📊 Moderate fees — sustainable but not aggressive",
                "color": "#f39c12",  # orange
            }
        elif apr >= 0.02:
            return {
                "apr": f"{apr:.2%}",
                "risk_level": "🟠 High Risk",
                "description": "⚠️ Marginal income — LPs may hesitate",
                "color": "#e67e22",  # darker orange
            }
        else:
            return {
                "apr": f"{apr:.2%}",
                "risk_level": "🔴 Severe Risk",
                "description": "❌ Barely any fees — not viable for LPs",
                "color": "#e74c3c",  # red
            }

        
