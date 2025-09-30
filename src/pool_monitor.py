import asyncio
import logging
import time
from typing import Dict, List, Optional, Callable
from web3 import Web3
from web3.middleware import geth_poa_middleware

logger = logging.getLogger("PoolMonitor")


class RealTimePoolMonitor:
    """
    Real-time monitoring system that detects new pool creations
    and automatically includes them in risk analysis.
    """
    
    def __init__(self, infura_url: str, redis_service=None):
        self.w3 = Web3(Web3.HTTPProvider(infura_url))
        self.redis = redis_service
        self.is_monitoring = False
        self.new_pools_callback: Optional[Callable] = None
        
        # Uniswap V3 Factory contract address
        self.factory_address = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
        
        # PoolCreated event ABI
        self.pool_created_abi = [
            {
                "anonymous": False,
                "inputs": [
                    {"indexed": True, "internalType": "address", "name": "token0", "type": "address"},
                    {"indexed": True, "internalType": "address", "name": "token1", "type": "address"},
                    {"indexed": True, "internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"indexed": False, "internalType": "int24", "name": "tickSpacing", "type": "int24"},
                    {"indexed": False, "internalType": "address", "name": "pool", "type": "address"}
                ],
                "name": "PoolCreated",
                "type": "event"
            }
        ]
        
        # Get factory contract
        self.factory_contract = self.w3.eth.contract(
            address=self.factory_address,
            abi=self.pool_created_abi
        )
        
        # Cache for known pools
        self.known_pools = set()
        self._load_known_pools()
        
        logger.info("RealTimePoolMonitor initialized")
    
    def _load_known_pools(self):
        """Load known pools from Redis cache"""
        if not self.redis:
            return
        
        try:
            known_pools = self.redis.get("known_pools")
            if known_pools:
                self.known_pools = set(known_pools.split(","))
                logger.info(f"Loaded {len(self.known_pools)} known pools from cache")
        except Exception as e:
            logger.error(f"Error loading known pools: {e}")
    
    def _save_known_pools(self):
        """Save known pools to Redis cache"""
        if not self.redis:
            return
        
        try:
            pools_str = ",".join(self.known_pools)
            self.redis.set("known_pools", pools_str)
        except Exception as e:
            logger.error(f"Error saving known pools: {e}")
    
    def set_new_pools_callback(self, callback: Callable[[List[Dict]], None]):
        """Set callback function to be called when new pools are detected"""
        self.new_pools_callback = callback
    
    async def start_monitoring(self, poll_interval: int = 30):
        """
        Start monitoring for new pool creations
        """
        if self.is_monitoring:
            logger.warning("Pool monitoring is already running")
            return
        
        self.is_monitoring = True
        logger.info(f"Starting pool monitoring (poll interval: {poll_interval}s)")
        
        try:
            while self.is_monitoring:
                await self._check_for_new_pools()
                await asyncio.sleep(poll_interval)
        except Exception as e:
            logger.error(f"Error in pool monitoring: {e}")
        finally:
            self.is_monitoring = False
            logger.info("Pool monitoring stopped")
    
    def stop_monitoring(self):
        """Stop the monitoring process"""
        self.is_monitoring = False
        logger.info("Stopping pool monitoring...")
    
    async def _check_for_new_pools(self):
        """Check for new pools created since last check"""
        try:
            # Get the latest block number
            latest_block = self.w3.eth.block_number
            
            # Get events from the last 100 blocks (adjust based on block time)
            from_block = max(0, latest_block - 100)
            to_block = latest_block
            
            logger.debug(f"Checking blocks {from_block} to {to_block} for new pools")
            
            # Create event filter
            event_filter = self.factory_contract.events.PoolCreated.create_filter(
                fromBlock=from_block,
                toBlock=to_block
            )
            
            # Get events
            events = event_filter.get_all_entries()
            
            new_pools = []
            for event in events:
                pool_address = event['args']['pool']
                
                if pool_address not in self.known_pools:
                    # New pool detected
                    pool_info = {
                        'address': pool_address,
                        'token0': event['args']['token0'],
                        'token1': event['args']['token1'],
                        'fee': event['args']['fee'],
                        'tick_spacing': event['args']['tickSpacing'],
                        'block_number': event['blockNumber'],
                        'transaction_hash': event['transactionHash'].hex(),
                        'timestamp': time.time()
                    }
                    
                    new_pools.append(pool_info)
                    self.known_pools.add(pool_address)
                    
                    logger.info(f"New pool detected: {pool_address}")
            
            if new_pools:
                logger.info(f"Found {len(new_pools)} new pools")
                
                # Save updated known pools
                self._save_known_pools()
                
                # Call callback if set
                if self.new_pools_callback:
                    try:
                        await self.new_pools_callback(new_pools)
                    except Exception as e:
                        logger.error(f"Error in new pools callback: {e}")
            
        except Exception as e:
            logger.error(f"Error checking for new pools: {e}")
    
    def get_pool_info(self, pool_address: str) -> Optional[Dict]:
        """Get detailed information about a specific pool"""
        try:
            # This would require additional contract calls to get pool details
            # For now, return basic info
            return {
                'address': pool_address,
                'is_known': pool_address in self.known_pools
            }
        except Exception as e:
            logger.error(f"Error getting pool info for {pool_address}: {e}")
            return None
    
    def get_monitoring_stats(self) -> Dict:
        """Get monitoring statistics"""
        return {
            'is_monitoring': self.is_monitoring,
            'known_pools_count': len(self.known_pools),
            'factory_address': self.factory_address
        }


class PoolAlertSystem:
    """
    Alert system for pool-related events and risk changes
    """
    
    def __init__(self, redis_service=None):
        self.redis = redis_service
        self.alert_callbacks = []
        self.risk_thresholds = {
            'critical_risk': 0.8,
            'high_risk': 0.6,
            'medium_risk': 0.4
        }
    
    def add_alert_callback(self, callback: Callable[[Dict], None]):
        """Add alert callback function"""
        self.alert_callbacks.append(callback)
    
    async def check_pool_alerts(self, pool_data: Dict, risk_analysis: Dict):
        """Check if pool requires alerts based on risk analysis"""
        try:
            symbol = pool_data.get('symbol', 'Unknown')
            risk_score = risk_analysis.get('composite_score', 0)
            risk_level = risk_analysis.get('risk_level', 'Unknown')
            
            alerts = []
            
            # Risk level alerts
            if risk_score >= self.risk_thresholds['critical_risk']:
                alerts.append({
                    'type': 'critical_risk',
                    'message': f"🚨 CRITICAL RISK: {symbol} has extremely high risk score ({risk_score:.3f})",
                    'severity': 'critical',
                    'pool': symbol,
                    'risk_score': risk_score
                })
            elif risk_score >= self.risk_thresholds['high_risk']:
                alerts.append({
                    'type': 'high_risk',
                    'message': f"⚠️ HIGH RISK: {symbol} has elevated risk score ({risk_score:.3f})",
                    'severity': 'high',
                    'pool': symbol,
                    'risk_score': risk_score
                })
            
            # TVL volatility alerts
            tvl = pool_data.get('tvl', 0)
            if tvl > 1000000:  # Only alert for pools with >$1M TVL
                volatility = risk_analysis.get('components', {}).get('liquidity_risk', 0)
                if volatility > 0.7:  # High volatility threshold
                    alerts.append({
                        'type': 'high_volatility',
                        'message': f"📈 HIGH VOLATILITY: {symbol} shows extreme TVL volatility",
                        'severity': 'medium',
                        'pool': symbol,
                        'volatility': volatility
                    })
            
            # Volume anomaly alerts
            volume_24h = pool_data.get('volume', 0)
            if volume_24h > 0 and tvl > 0:
                volume_tvl_ratio = volume_24h / tvl
                if volume_tvl_ratio > 2.0:  # Volume > 2x TVL
                    alerts.append({
                        'type': 'volume_anomaly',
                        'message': f"💥 VOLUME ANOMALY: {symbol} has extreme volume/TVL ratio ({volume_tvl_ratio:.2f})",
                        'severity': 'medium',
                        'pool': symbol,
                        'volume_ratio': volume_tvl_ratio
                    })
            
            # Send alerts
            for alert in alerts:
                await self._send_alert(alert)
                
        except Exception as e:
            logger.error(f"Error checking pool alerts: {e}")
    
    async def _send_alert(self, alert: Dict):
        """Send alert to all registered callbacks"""
        try:
            # Store alert in Redis for persistence
            if self.redis:
                alert_key = f"alert:{int(time.time())}:{alert['pool']}"
                self.redis.set(alert_key, str(alert), ex=86400)  # Expire in 24 hours
            
            # Call all alert callbacks
            for callback in self.alert_callbacks:
                try:
                    await callback(alert)
                except Exception as e:
                    logger.error(f"Error in alert callback: {e}")
            
            logger.info(f"Alert sent: {alert['message']}")
            
        except Exception as e:
            logger.error(f"Error sending alert: {e}")
    
    def get_recent_alerts(self, hours: int = 24) -> List[Dict]:
        """Get recent alerts from Redis"""
        if not self.redis:
            return []
        
        try:
            alerts = []
            # This would require scanning Redis keys for alerts
            # Implementation depends on Redis key structure
            return alerts
        except Exception as e:
            logger.error(f"Error getting recent alerts: {e}")
            return []
