import asyncio
import aiohttp
import time
import json
import logging

from web3 import Web3
from config import INFURA_URL, SUBGRAPH_URL, LEGACY_POOLS, CHAINLINK_ETH_USD, PREDEFINED_ABIS, UNISWAP_V3_SUBGRAPH_URL
from pool_discovery import DynamicPoolDiscovery


class UniswapDataFetcher:
    def __init__(
        self,
        infura_url=INFURA_URL,
        subgraph_url=SUBGRAPH_URL,
        pools=None,  # Will use dynamic discovery by default
        abis=PREDEFINED_ABIS,
        chainlink_address=CHAINLINK_ETH_USD,
        use_dynamic_discovery=True,
    ):
        # Initialize Web3 if an endpoint is provided; otherwise operate subgraph-only
        self.w3 = None
        if infura_url:
            try:
                self.w3 = Web3(Web3.HTTPProvider(infura_url))
            except Exception:
                self.w3 = None
        self.subgraph_url = subgraph_url
        self.abis = abis
        self.pools = pools or LEGACY_POOLS  # Fallback to legacy pools
        self.chainlink_address = chainlink_address
        self.session = aiohttp.ClientSession()
        self.eth_price = 0
        self.logger = logging.getLogger("UniswapFetcher")
        self._contract_cache = {}
        self.use_dynamic_discovery = use_dynamic_discovery
        
        # Only initialize pool discovery if subgraph is configured
        if use_dynamic_discovery and UNISWAP_V3_SUBGRAPH_URL:
            try:
                self.pool_discovery = DynamicPoolDiscovery()
                self.logger.info("Dynamic pool discovery initialized")
            except Exception as e:
                self.logger.warning(f"Failed to initialize dynamic discovery: {e}")
                self.pool_discovery = None
                self.use_dynamic_discovery = False
        else:
            self.pool_discovery = None
            if use_dynamic_discovery:
                self.logger.warning("Dynamic discovery disabled - subgraph not configured")

        # Web3 is optional for the current app flow; proceed if unavailable
        if self.w3 is not None and not self.w3.is_connected():
            self.logger.warning("Web3 not connected; continuing with subgraph-only data")
            self.w3 = None

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

    async def fetch_all_pools(self, use_dynamic_discovery=None):
        """
        Fetch data for all pools. If use_dynamic_discovery is True,
        automatically discover and analyze all available pools.
        """
        if not self.session:
            raise RuntimeError("Use async context manager (async with)")

        await self.get_eth_price()
        
        # Determine whether to use dynamic discovery
        use_dynamic = use_dynamic_discovery if use_dynamic_discovery is not None else self.use_dynamic_discovery
        
        if use_dynamic and self.pool_discovery:
            return await self._fetch_dynamic_pools()
        else:
            self.logger.info("Using legacy pool fetching (dynamic discovery not available)")
            return await self._fetch_legacy_pools()

    async def _fetch_dynamic_pools(self):
        """Fetch data using dynamic pool discovery"""
        try:
            # Discover all pools dynamically
            async with self.pool_discovery as discovery:
                discovered_pools = await discovery.discover_all_pools()
            
            if not discovered_pools:
                self.logger.warning("No pools discovered dynamically, falling back to legacy pools")
                return await self._fetch_legacy_pools()
            
            self.logger.info(f"Discovered {len(discovered_pools)} pools dynamically")
            
            # Process discovered pools
            processed = {}
            for symbol, pool_data in discovered_pools.items():
                try:
                    processed[symbol] = {
                        "tvl": pool_data["tvl"],
                        "liquidity": pool_data["liquidity"],
                        "volume": pool_data["volume_24h"],
                        "fee_tier": pool_data["fee_tier"],
                        "eth_price": self.eth_price,
                        "timestamp": pool_data["timestamp"],
                        "capital_efficiency": pool_data["capital_efficiency"],
                        "volume_stability": pool_data["volume_stability"],
                        "tx_count": pool_data["tx_count"],
                        "token0": pool_data["token0"],
                        "token1": pool_data["token1"],
                        "pool_id": pool_data["id"],
                    }
                except (TypeError, ValueError, KeyError) as e:
                    self.logger.error(f"Processing error for {symbol}: {str(e)}")
                    continue
            
            return processed
            
        except Exception as e:
            self.logger.error(f"Dynamic pool discovery failed: {str(e)}")
            self.logger.info("Falling back to legacy pool fetching")
            return await self._fetch_legacy_pools()

    async def _fetch_legacy_pools(self):
        """Fetch data for legacy hardcoded pools"""
        self.logger.info("Using legacy pool fetching")
        
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