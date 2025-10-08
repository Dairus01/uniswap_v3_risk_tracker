import os
from dotenv import load_dotenv

load_dotenv()

# Blockchain connections
INFURA_URL = os.getenv("INFURA_URL")

# SUBGRAPH CONNECTIONS
subgraph_api_key = os.getenv("subgraph_api_key")
subgraph_id = os.getenv("subgraph_id")

# Working Uniswap V3 subgraph via Gateway API
# This is the current working endpoint for Uniswap V3 data
UNISWAP_V3_SUBGRAPH_URL = (
    f"https://gateway.thegraph.com/api/{subgraph_api_key}/subgraphs/id/{subgraph_id}"
    if subgraph_api_key and subgraph_id else None
)

# Use working Gateway API by default
SUBGRAPH_URL = UNISWAP_V3_SUBGRAPH_URL

# Add validation
if not SUBGRAPH_URL:
    print("WARNING: Subgraph configuration missing. Please set subgraph_api_key and subgraph_id in .env file")
    print("Dynamic pool discovery will not work without proper subgraph configuration.")

# Dynamic Pool Discovery Configuration
POOL_DISCOVERY_CONFIG = {
    "min_tvl_usd": 10000,  # Minimum TVL threshold ($10k)
    "min_tx_count": 100,   # Minimum transaction count
    "max_pools": 1000,     # Maximum pools to analyze
    "batch_size": 1000,    # Batch size for pagination
    "update_interval": 300, # Pool discovery update interval (seconds)
}

# Legacy pools removed - using dynamic discovery only

# Chainlink ETH/USD price feed
CHAINLINK_ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"

PREDEFINED_ABIS = {
    "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419": [  # Chainlink ETH/USD
        {
            "inputs": [],
            "name": "latestAnswer",
            "outputs": [{"internalType": "int256", "name": "", "type": "int256"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]
}


# database connections
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = os.getenv("REDIS_PORT")
REDIS_DB = os.getenv("REDIS_DB")


# Risk Config
RISK_CONFIG = {
    # Volatility calculation
    "volatility_decay": 0.94,  # EWMA decay factor
    # VaR parameters
    "liquidity_scale": 0.8,  # Scaling factor for liquidity adjustment
    "var_confidence_z": 1.65,  # Z-score for 95% confidence
    # CVaR adjustment
    "cvar_z_adjustment": 2.06,  # Z for 98% confidence
    # Advanced risk scoring weights
    "risk_weights": {
        "liquidity_risk": 0.25,      # TVL volatility, concentration
        "market_risk": 0.30,          # Price impact, impermanent loss
        "operational_risk": 0.20,     # Fee efficiency, transaction frequency
        "systemic_risk": 0.25,        # Token correlation, ecosystem health
    },
    # Normalization bounds
    "max_volatility": 50.0,  # 50% max volatility for normalization
    "max_var": 0.3,  # 30% of TVL max VaR
    # Liquidity depth scaling
    "volume_scale": 0.05,  # Volume-to-liquidity ratio scaling
    # Advanced risk thresholds
    "risk_thresholds": {
        "low_risk": 0.3,      # Green zone
        "medium_risk": 0.6,    # Yellow zone
        "high_risk": 0.8,      # Orange zone
        "critical_risk": 1.0,  # Red zone
    },
    # Impermanent loss calculation
    "il_calculation": {
        "price_volatility_window": 7,  # Days for price volatility
        "correlation_threshold": 0.7,  # Token correlation threshold
    },
    # Price impact calculation
    "price_impact": {
        "trade_size_threshold": 0.01,  # 1% of pool liquidity
        "impact_threshold": 0.05,      # 5% price impact threshold
    },
}