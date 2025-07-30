import asyncio
import aiohttp
import time
import json
import logging

from web3 import Web3
from config import INFURA_URL, SUBGRAPH_URL, POOLS, CHAINLINK_ETH_USD, PREDEFINED_ABIS


class UniswapDataFetcher:
    def __init__(
        self,
        infura_url=INFURA_URL,
        subgraph_url=SUBGRAPH_URL,
        pools=POOLS,
        abis=PREDEFINED_ABIS,
        chainlink_address=CHAINLINK_ETH_USD,
    ):
        self.w3 = Web3(Web3.HTTPProvider(infura_url))
        self.subgraph_url = subgraph_url
        self.abis = abis
        self.pools = pools
        self.chainlink_address = chainlink_address
        self.session = aiohttp.ClientSession()
        self.eth_price = 0
        self.logger = logging.getLogger("UniswapFetcher")
        self._contract_cache = {}

        if not self.w3.is_connected():
            raise ConnectionError("Failed to connect to ethereum node")

    def _load_abi(self, address=None):
        if address is not None:
            return self.abis[address]
        return {}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    def get_contract(self, address, abi=None):

        if address not in self._contract_cache:

            if abi is None:
                abi = self._load_abi(address)

            self._contract_cache[address] = self.w3.eth.contract(
                address=address, abi=abi
            )

        return self._contract_cache[address]

    async def fetch_pool_data(self, pool_id: str):
        """Fetch pool data from Uniswap V3 Subgraph"""

        query = f"""
        {{
            pool(id: "{pool_id}") {{
                totalValueLockedUSD
                token0 {{ symbol decimals }}
                token1 {{ symbol decimals }}
                feeTier
                liquidity
                poolDayData(first: 1, orderBy: date, orderDirection: desc) {{
                    volumeUSD
                }}
            }}
        }}
        """

        try:
            async with self.session.post(
                url=self.subgraph_url, json={"query": query}
            ) as response:

                if response.status != 200:
                    self.logger.error(f"Subgraph error: HTTP {response.status}")
                    return None

                data = await response.json()
                return data.get("data", {}).get("pool", {})
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            self.logger.error(f"Network error: {str(e)}")

    async def get_eth_price(self, retries=3):
        """Get ETH/USD price with retry logic"""
        contract = self.get_contract(
            address=self.chainlink_address, abi=self.abis[self.chainlink_address]
        )

        for attempt in range(retries):
            try:
                price = contract.functions.latestAnswer().call() / 10**8
                self.eth_price = price
                return price
            except Exception as e:
                if attempt < retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    self.logger.error(
                        f"Chainlink error after {retries} attempts: {str(e)}"
                    )
                    return self.eth_price

    async def fetch_all_pools(self):

        if not self.session:
            raise RuntimeError("Use async context manager (async with)")

        await self.get_eth_price()

        tasks = [self.fetch_pool_data(pool_id) for pool_id in self.pools.values()]
        results = await asyncio.gather(*tasks)

        processed = {}

        for symbol, result in zip(self.pools.keys(), results):

            if not result:
                self.logger.warning(f"result unfetched for pool {symbol}")
                continue

            try:
                processed[symbol] = {
                    "tvl": float(result.get("totalValueLockedUSD", 0)),
                    "liquidity": float(result.get("liquidity", 0)),
                    "volume": float(
                        result.get("poolDayData", [{}])[0].get("volumeUSD", 0)
                    ),
                    "fee_tier": int(result.get("feeTier", 0)) / 10**4,
                    "eth_price": self.eth_price,
                    "timestamp": int(time.time()),
                }
            except (TypeError, ValueError) as e:
                self.logger.error(f"Processing error for {symbol}: {str(e)}")

        return processed
