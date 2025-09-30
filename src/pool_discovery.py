import asyncio
import aiohttp
import logging
import time
from typing import List, Dict, Optional
from config import (
    UNISWAP_V3_SUBGRAPH_URL, 
    POOL_DISCOVERY_CONFIG,
    RISK_CONFIG
)

logger = logging.getLogger("PoolDiscovery")


class DynamicPoolDiscovery:
    """
    Dynamic pool discovery system that automatically detects and analyzes
    all available Uniswap V3 pools using The Graph subgraph.
    """
    
    def __init__(self):
        self.subgraph_url = UNISWAP_V3_SUBGRAPH_URL
        self.config = POOL_DISCOVERY_CONFIG
        self.session = None
        self.discovered_pools = {}
        self.last_update = 0
        
        # Validate subgraph URL
        if not self.subgraph_url:
            logger.error("Subgraph URL not configured. Check your .env file.")
            raise ValueError("Subgraph URL not configured. Please set subgraph_api_key and subgraph_id in .env file.")
        
        logger.info(f"Using subgraph URL: {self.subgraph_url}")
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()
    
    def _should_update_pools(self) -> bool:
        """Check if pool discovery should be updated based on interval"""
        current_time = time.time()
        return (current_time - self.last_update) > self.config["update_interval"]
    
    async def _fetch_pools_batch(self, skip: int, batch_size: int) -> List[Dict]:
        """Fetch a batch of pools from the subgraph with enhanced error handling"""
        query = f"""
        {{
            pools(
                first: {batch_size},
                skip: {skip},
                orderBy: totalValueLockedUSD,
                orderDirection: desc,
                where: {{
                    totalValueLockedUSD_gt: "{self.config['min_tvl_usd']}",
                    txCount_gt: "{self.config['min_tx_count']}"
                }}
            ) {{
                id
                token0 {{
                    id
                    symbol
                    name
                    decimals
                }}
                token1 {{
                    id
                    symbol
                    name
                    decimals
                }}
                feeTier
                liquidity
                sqrtPrice
                tick
                volumeUSD
                totalValueLockedUSD
                txCount
                createdAtTimestamp
                poolDayData(first: 7, orderBy: date, orderDirection: desc) {{
                    volumeUSD
                    tvlUSD
                    feesUSD
                    date
                }}
            }}
        }}
        """
        
        try:
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'Uniswap-Risk-Dashboard/1.0'
            }
            
            logger.debug(f"Fetching pools batch: skip={skip}, batch_size={batch_size}")
            
            async with self.session.post(
                url=self.subgraph_url,
                json={"query": query},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                
                logger.debug(f"Subgraph response status: {response.status}")
                
                if response.status == 401:
                    logger.error("Authentication failed. Check your subgraph API key.")
                    return []
                elif response.status == 403:
                    logger.error("Access forbidden. Check your subgraph permissions.")
                    return []
                elif response.status == 429:
                    logger.warning("Rate limited. Waiting before retry...")
                    await asyncio.sleep(1)
                    return []
                elif response.status != 200:
                    logger.error(f"Subgraph error: HTTP {response.status}")
                    return []
                
                data = await response.json()
                
                if "errors" in data:
                    logger.error(f"GraphQL errors: {data['errors']}")
                    return []
                
                pools = data.get("data", {}).get("pools", [])
                logger.info(f"Successfully fetched {len(pools)} pools from subgraph")
                return pools
                
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.error(f"Network error fetching pools batch: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching pools: {str(e)}")
            return []
    
    def _filter_pools(self, pools: List[Dict]) -> List[Dict]:
        """Apply intelligent filtering criteria to pools"""
        filtered_pools = []
        
        for pool in pools:
            try:
                # Basic validation
                if not pool.get("id") or not pool.get("token0") or not pool.get("token1"):
                    continue
                
                # TVL validation
                tvl = float(pool.get("totalValueLockedUSD", 0))
                if tvl < self.config["min_tvl_usd"]:
                    continue
                
                # Transaction count validation
                tx_count = int(pool.get("txCount", 0))
                if tx_count < self.config["min_tx_count"]:
                    continue
                
                # Token validation (avoid invalid tokens)
                token0_symbol = pool["token0"].get("symbol", "")
                token1_symbol = pool["token1"].get("symbol", "")
                
                if not token0_symbol or not token1_symbol:
                    continue
                
                # Skip pools with suspicious symbols
                suspicious_symbols = ["", "UNKNOWN", "N/A", "null"]
                if token0_symbol in suspicious_symbols or token1_symbol in suspicious_symbols:
                    continue
                
                # Add processed pool data
                processed_pool = self._process_pool_data(pool)
                if processed_pool:
                    filtered_pools.append(processed_pool)
                
            except (ValueError, TypeError) as e:
                logger.warning(f"Error processing pool {pool.get('id', 'unknown')}: {e}")
                continue
        
        return filtered_pools
    
    def _process_pool_data(self, pool: Dict) -> Dict:
        """Process and enrich pool data with additional metrics"""
        try:
            # Calculate additional metrics
            tvl = float(pool.get("totalValueLockedUSD", 0))
            volume_24h = float(pool.get("volumeUSD", 0))
            liquidity = float(pool.get("liquidity", 0))
            tx_count = int(pool.get("txCount", 0))
            
            # Calculate capital efficiency
            capital_efficiency = volume_24h / tvl if tvl > 0 else 0
            
            # Calculate volume stability from historical data
            historical_volumes = [
                float(day.get("volumeUSD", 0)) 
                for day in pool.get("poolDayData", [])
            ]
            volume_stability = self._calculate_volume_stability(historical_volumes)
            
            # Create pool symbol for identification
            token0_symbol = pool["token0"].get("symbol", "")
            token1_symbol = pool["token1"].get("symbol", "")
            pool_symbol = f"{token0_symbol}_{token1_symbol}"
            
            processed_pool = {
                "id": pool["id"],
                "symbol": pool_symbol,
                "token0": pool["token0"],
                "token1": pool["token1"],
                "fee_tier": int(pool.get("feeTier", 0)) / 10000,  # Convert to percentage
                "tvl": tvl,
                "liquidity": liquidity,
                "volume_24h": volume_24h,
                "tx_count": tx_count,
                "capital_efficiency": capital_efficiency,
                "volume_stability": volume_stability,
                "created_at": int(pool.get("createdAtTimestamp", 0)),
                "historical_data": pool.get("poolDayData", []),
                "timestamp": int(time.time())
            }
            
            return processed_pool
            
        except Exception as e:
            logger.error(f"Error processing pool data: {e}")
            return None
    
    def _calculate_volume_stability(self, volumes: List[float]) -> float:
        """Calculate volume stability coefficient (0-1, higher is more stable)"""
        if len(volumes) < 2:
            return 0.5  # Default neutral stability
        
        try:
            # Calculate coefficient of variation (CV = std/mean)
            mean_volume = sum(volumes) / len(volumes)
            if mean_volume == 0:
                return 0.0
            
            variance = sum((v - mean_volume) ** 2 for v in volumes) / len(volumes)
            std_dev = variance ** 0.5
            cv = std_dev / mean_volume
            
            # Convert CV to stability score (inverse relationship)
            # CV of 0 = perfect stability (1.0), CV of 1+ = low stability (0.0)
            stability = max(0, min(1, 1 - cv))
            return stability
            
        except Exception:
            return 0.5
    
    async def discover_all_pools(self, force_update: bool = False) -> Dict[str, Dict]:
        """
        Discover all pools above minimum threshold with intelligent filtering.
        Returns a dictionary of pool_symbol -> pool_data
        """
        if not force_update and not self._should_update_pools():
            logger.info("Using cached pool data")
            return self.discovered_pools
        
        if not self.session:
            raise RuntimeError("Use async context manager (async with)")
        
        logger.info("Starting dynamic pool discovery...")
        all_pools = []
        skip = 0
        batch_size = self.config["batch_size"]
        max_pools = self.config["max_pools"]
        
        while len(all_pools) < max_pools:
            logger.info(f"Fetching pools batch: skip={skip}, batch_size={batch_size}")
            
            pools_batch = await self._fetch_pools_batch(skip, batch_size)
            
            if not pools_batch:
                logger.info("No more pools found, stopping discovery")
                break
            
            # Filter and process pools
            filtered_pools = self._filter_pools(pools_batch)
            all_pools.extend(filtered_pools)
            
            logger.info(f"Found {len(filtered_pools)} valid pools in this batch")
            
            # Check if we've reached the limit
            if len(all_pools) >= max_pools:
                all_pools = all_pools[:max_pools]
                break
            
            skip += batch_size
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.1)
        
        # Convert to dictionary format
        self.discovered_pools = {
            pool["symbol"]: pool for pool in all_pools if pool is not None
        }
        
        self.last_update = time.time()
        
        logger.info(f"Pool discovery completed. Found {len(self.discovered_pools)} pools")
        return self.discovered_pools
    
    def get_pool_by_symbol(self, symbol: str) -> Optional[Dict]:
        """Get a specific pool by its symbol"""
        return self.discovered_pools.get(symbol)
    
    def get_top_pools_by_tvl(self, limit: int = 10) -> List[Dict]:
        """Get top pools by TVL"""
        sorted_pools = sorted(
            self.discovered_pools.values(),
            key=lambda x: x["tvl"],
            reverse=True
        )
        return sorted_pools[:limit]
    
    def get_pools_by_risk_level(self, risk_level: str) -> List[Dict]:
        """Get pools filtered by risk level (placeholder for future implementation)"""
        # This will be implemented when we add risk scoring
        return list(self.discovered_pools.values())
    
    def get_pool_statistics(self) -> Dict:
        """Get overall statistics about discovered pools"""
        if not self.discovered_pools:
            return {}
        
        pools = list(self.discovered_pools.values())
        
        total_tvl = sum(pool["tvl"] for pool in pools)
        total_volume = sum(pool["volume_24h"] for pool in pools)
        avg_capital_efficiency = sum(pool["capital_efficiency"] for pool in pools) / len(pools)
        
        return {
            "total_pools": len(pools),
            "total_tvl_usd": total_tvl,
            "total_volume_24h_usd": total_volume,
            "average_capital_efficiency": avg_capital_efficiency,
            "last_updated": self.last_update
        }