import os
from dotenv import load_dotenv

load_dotenv()

# Blockchain connections
INFURA_URL = os.getenv('INFURA_URL')

#SUBGRAPH CONNECTIONS   
subgraph_api_key = os.getenv('subgraph_api_key')
subgraph_id = os.getenv('subgraph_id')
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{subgraph_api_key}/subgraphs/id/{subgraph_id}"

# Target pools (USDC/ETH, WBTC/ETH, LINK/ETH)
POOLS = {
    "USDC_ETH": "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8",
    "WBTC_ETH": "0xcbcdf9626bc03e24f779434178a73a0b4bad62ed",
    "LINK_ETH": "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8"
}

# Chainlink ETH/USD price feed
CHAINLINK_ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"

PREDEFINED_ABIS = {
    "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419": [  # Chainlink ETH/USD
        {
            "inputs": [],
            "name": "latestAnswer",
            "outputs": [{"internalType": "int256", "name": "", "type": "int256"}],
            "stateMutability": "view",
            "type": "function"
        }
    ]
}