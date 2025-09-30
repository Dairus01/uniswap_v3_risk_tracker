import math
import time
import logging
import json
import statistics
from typing import Dict, List, Optional

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
        self, symbol: str, current_tvl: float, volume: float
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

    def calculate_fee_income_risk(
        self, volume: float, tvl: float, fee_tier: float
    ) -> dict:
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

    def calculate_liquidity_risk(self, pool_data: Dict) -> float:
        """
        Calculate liquidity risk score (0-1, higher is riskier)
        Based on TVL volatility, liquidity concentration, and depth
        """
        try:
            tvl = pool_data.get("tvl", 0)
            volume_24h = pool_data.get("volume", 0)
            liquidity = pool_data.get("liquidity", 0)
            
            if tvl <= 0:
                return 1.0  # Maximum risk for zero TVL
            
            # TVL volatility component
            tvl_volatility = self.calculate_tvl_volatility(
                pool_data.get("symbol", "unknown"), tvl
            ) / 100  # Convert percentage to decimal
            
            # Liquidity depth component (volume vs liquidity ratio)
            depth_ratio = volume_24h / liquidity if liquidity > 0 else 1.0
            depth_risk = min(1.0, depth_ratio / 0.1)  # Normalize to 0-1
            
            # TVL size component (smaller pools are riskier)
            tvl_size_risk = max(0, 1 - (tvl / 1000000))  # Risk decreases with TVL size
            
            # Weighted combination
            liquidity_risk = (
                tvl_volatility * 0.4 +
                depth_risk * 0.3 +
                tvl_size_risk * 0.3
            )
            
            return min(1.0, max(0.0, liquidity_risk))
            
        except Exception as e:
            self.logger.error(f"Error calculating liquidity risk: {e}")
            return 0.5  # Default neutral risk

    def calculate_market_risk(self, pool_data: Dict) -> float:
        """
        Calculate market risk score (0-1, higher is riskier)
        Based on price impact, impermanent loss probability, and token correlation
        """
        try:
            tvl = pool_data.get("tvl", 0)
            volume_24h = pool_data.get("volume", 0)
            fee_tier = pool_data.get("fee_tier", 0.003)  # Default 0.3%
            
            if tvl <= 0:
                return 1.0
            
            # Price impact risk (based on volume vs TVL ratio)
            volume_tvl_ratio = volume_24h / tvl if tvl > 0 else 1.0
            price_impact_risk = min(1.0, volume_tvl_ratio / 0.5)  # Normalize
            
            # Impermanent loss risk (higher for volatile pairs)
            # Simplified calculation based on fee tier and volume
            il_risk = 0.5  # Default moderate risk
            if fee_tier >= 0.01:  # 1% fee tier
                il_risk = 0.3  # Lower risk for high fee pools
            elif fee_tier <= 0.0005:  # 0.05% fee tier
                il_risk = 0.7  # Higher risk for low fee pools
            
            # Volume stability component
            volume_stability = pool_data.get("volume_stability", 0.5)
            stability_risk = 1 - volume_stability
            
            # Weighted combination
            market_risk = (
                price_impact_risk * 0.4 +
                il_risk * 0.3 +
                stability_risk * 0.3
            )
            
            return min(1.0, max(0.0, market_risk))
            
        except Exception as e:
            self.logger.error(f"Error calculating market risk: {e}")
            return 0.5

    def calculate_operational_risk(self, pool_data: Dict) -> float:
        """
        Calculate operational risk score (0-1, higher is riskier)
        Based on fee efficiency, transaction frequency, and pool maturity
        """
        try:
            tx_count = pool_data.get("tx_count", 0)
            fee_tier = pool_data.get("fee_tier", 0.003)
            volume_24h = pool_data.get("volume", 0)
            created_at = pool_data.get("created_at", 0)
            
            # Transaction frequency risk
            tx_frequency_risk = 0.5  # Default moderate risk
            if tx_count < 100:
                tx_frequency_risk = 0.8  # High risk for low activity
            elif tx_count > 10000:
                tx_frequency_risk = 0.2  # Low risk for high activity
            
            # Fee efficiency risk
            fee_efficiency_risk = 0.5
            if fee_tier >= 0.01:  # 1% fee tier
                fee_efficiency_risk = 0.3  # Lower risk
            elif fee_tier <= 0.0005:  # 0.05% fee tier
                fee_efficiency_risk = 0.7  # Higher risk
            
            # Pool maturity risk (newer pools are riskier)
            current_time = time.time()
            pool_age_days = (current_time - created_at) / (24 * 3600) if created_at > 0 else 0
            maturity_risk = max(0, 1 - (pool_age_days / 30))  # Risk decreases over 30 days
            
            # Volume consistency risk
            volume_stability = pool_data.get("volume_stability", 0.5)
            consistency_risk = 1 - volume_stability
            
            # Weighted combination
            operational_risk = (
                tx_frequency_risk * 0.3 +
                fee_efficiency_risk * 0.3 +
                maturity_risk * 0.2 +
                consistency_risk * 0.2
            )
            
            return min(1.0, max(0.0, operational_risk))
            
        except Exception as e:
            self.logger.error(f"Error calculating operational risk: {e}")
            return 0.5

    def calculate_systemic_risk(self, pool_data: Dict) -> float:
        """
        Calculate systemic risk score (0-1, higher is riskier)
        Based on token ecosystem health and cross-pool dependencies
        """
        try:
            token0 = pool_data.get("token0", {})
            token1 = pool_data.get("token1", {})
            tvl = pool_data.get("tvl", 0)
            
            # Token ecosystem risk (simplified)
            ecosystem_risk = 0.5  # Default moderate risk
            
            # Check for major tokens (lower risk)
            major_tokens = ["WETH", "USDC", "USDT", "WBTC", "DAI"]
            token0_symbol = token0.get("symbol", "")
            token1_symbol = token1.get("symbol", "")
            
            if token0_symbol in major_tokens and token1_symbol in major_tokens:
                ecosystem_risk = 0.2  # Low risk for major token pairs
            elif token0_symbol in major_tokens or token1_symbol in major_tokens:
                ecosystem_risk = 0.4  # Medium risk for one major token
            else:
                ecosystem_risk = 0.7  # Higher risk for non-major tokens
            
            # TVL concentration risk (larger pools have lower systemic risk)
            tvl_concentration_risk = max(0, 1 - (tvl / 10000000))  # Risk decreases with TVL
            
            # Volume stability component
            volume_stability = pool_data.get("volume_stability", 0.5)
            stability_risk = 1 - volume_stability
            
            # Weighted combination
            systemic_risk = (
                ecosystem_risk * 0.5 +
                tvl_concentration_risk * 0.3 +
                stability_risk * 0.2
            )
            
            return min(1.0, max(0.0, systemic_risk))
            
        except Exception as e:
            self.logger.error(f"Error calculating systemic risk: {e}")
            return 0.5

    def calculate_comprehensive_risk_score(self, pool_data: Dict) -> Dict:
        """
        Calculate comprehensive risk score combining all risk dimensions
        Returns detailed risk analysis
        """
        try:
            # Calculate individual risk components
            liquidity_risk = self.calculate_liquidity_risk(pool_data)
            market_risk = self.calculate_market_risk(pool_data)
            operational_risk = self.calculate_operational_risk(pool_data)
            systemic_risk = self.calculate_systemic_risk(pool_data)
            
            # Get risk weights from config
            weights = self.config["risk_weights"]
            
            # Calculate weighted composite score
            composite_score = (
                liquidity_risk * weights["liquidity_risk"] +
                market_risk * weights["market_risk"] +
                operational_risk * weights["operational_risk"] +
                systemic_risk * weights["systemic_risk"]
            )
            
            # Determine risk level
            thresholds = self.config["risk_thresholds"]
            if composite_score <= thresholds["low_risk"]:
                risk_level = "Low Risk"
                risk_color = "#2ecc71"  # Green
                risk_emoji = "🟢"
            elif composite_score <= thresholds["medium_risk"]:
                risk_level = "Medium Risk"
                risk_color = "#f39c12"  # Orange
                risk_emoji = "🟡"
            elif composite_score <= thresholds["high_risk"]:
                risk_level = "High Risk"
                risk_color = "#e67e22"  # Dark orange
                risk_emoji = "🟠"
            else:
                risk_level = "Critical Risk"
                risk_color = "#e74c3c"  # Red
                risk_emoji = "🔴"
            
            return {
                "composite_score": composite_score,
                "risk_level": risk_level,
                "risk_color": risk_color,
                "risk_emoji": risk_emoji,
                "components": {
                    "liquidity_risk": liquidity_risk,
                    "market_risk": market_risk,
                    "operational_risk": operational_risk,
                    "systemic_risk": systemic_risk,
                },
                "weights": weights,
                "description": self._get_risk_description(composite_score, risk_level)
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating comprehensive risk: {e}")
            return {
                "composite_score": 0.5,
                "risk_level": "Unknown Risk",
                "risk_color": "#95a5a6",
                "risk_emoji": "⚪",
                "components": {},
                "weights": {},
                "description": "Risk calculation failed"
            }

    def _get_risk_description(self, score: float, level: str) -> str:
        """Generate human-readable risk description"""
        if score <= 0.3:
            return "Excellent risk profile with strong liquidity and stability"
        elif score <= 0.6:
            return "Moderate risk with acceptable volatility and liquidity"
        elif score <= 0.8:
            return "Elevated risk requiring careful monitoring"
        else:
            return "High risk - consider avoiding or use with extreme caution"

    def get_top_risky_pools(self, pools_data: Dict, limit: int = 10) -> List[Dict]:
        """
        Get top pools by risk score (highest risk first)
        """
        risky_pools = []
        
        for symbol, pool_data in pools_data.items():
            try:
                risk_analysis = self.calculate_comprehensive_risk_score(pool_data)
                risky_pools.append({
                    "symbol": symbol,
                    "tvl": pool_data.get("tvl", 0),
                    "volume": pool_data.get("volume", 0),
                    "risk_score": risk_analysis["composite_score"],
                    "risk_level": risk_analysis["risk_level"],
                    "risk_color": risk_analysis["risk_color"],
                    "risk_emoji": risk_analysis["risk_emoji"],
                    "components": risk_analysis["components"]
                })
            except Exception as e:
                self.logger.error(f"Error analyzing risk for {symbol}: {e}")
                continue
        
        # Sort by risk score (highest first)
        risky_pools.sort(key=lambda x: x["risk_score"], reverse=True)
        return risky_pools[:limit]

    def get_safest_pools(self, pools_data: Dict, limit: int = 10) -> List[Dict]:
        """
        Get safest pools by risk score (lowest risk first)
        """
        safe_pools = []
        
        for symbol, pool_data in pools_data.items():
            try:
                risk_analysis = self.calculate_comprehensive_risk_score(pool_data)
                safe_pools.append({
                    "symbol": symbol,
                    "tvl": pool_data.get("tvl", 0),
                    "volume": pool_data.get("volume", 0),
                    "risk_score": risk_analysis["composite_score"],
                    "risk_level": risk_analysis["risk_level"],
                    "risk_color": risk_analysis["risk_color"],
                    "risk_emoji": risk_analysis["risk_emoji"],
                    "components": risk_analysis["components"]
                })
            except Exception as e:
                self.logger.error(f"Error analyzing risk for {symbol}: {e}")
                continue
        
        # Sort by risk score (lowest first)
        safe_pools.sort(key=lambda x: x["risk_score"])
        return safe_pools[:limit]
