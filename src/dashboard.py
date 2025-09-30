import streamlit as st
import asyncio
import time
import logging
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
import aiohttp

from pipeline import UniswapDataFetcher
from redis_service import RedisService
from risk_engine import RiskEngine
from pool_discovery import DynamicPoolDiscovery
from config import UNISWAP_V3_SUBGRAPH_URL, POOL_DISCOVERY_CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Dashboard")


def format_large_number(value):
    """
    Format large numbers with appropriate suffixes (K, M, B, T)
    """
    if value >= 1e12:  # Trillions
        return f"${value/1e12:.2f}T"
    elif value >= 1e9:  # Billions
        return f"${value/1e9:.2f}B"
    elif value >= 1e6:  # Millions
        return f"${value/1e6:.2f}M"
    elif value >= 1e3:  # Thousands
        return f"${value/1e3:.2f}K"
    else:
        return f"${value:.2f}"


def convert_to_numeric_for_sorting(value_str):
    """Convert formatted string back to numeric value for sorting"""
    if pd.isna(value_str) or value_str == "":
        return 0
    
    # Remove $ and get the numeric part
    value_str = str(value_str).replace("$", "").replace(",", "")
    
    if value_str.endswith("T"):
        return float(value_str[:-1]) * 1e12
    elif value_str.endswith("B"):
        return float(value_str[:-1]) * 1e9
    elif value_str.endswith("M"):
        return float(value_str[:-1]) * 1e6
    elif value_str.endswith("K"):
        return float(value_str[:-1]) * 1e3
    else:
        return float(value_str)


def get_risk_level_numeric(risk_level_str):
    """Convert risk level string to numeric for sorting"""
    risk_mapping = {
        "🟢 Low Risk": 1,
        "🟡 Medium Risk": 2,
        "🟠 High Risk": 3,
        "🔴 Critical Risk": 4
    }
    return risk_mapping.get(risk_level_str, 0)


async def fetch_historical_pool_data(pool_id, days=30):
    """
    Fetch real historical data for a specific pool from The Graph subgraph
    """
    if not UNISWAP_V3_SUBGRAPH_URL:
        logger.warning("Subgraph URL not configured, using simulated data")
        return None
    
    # Calculate timestamp for 30 days ago
    end_timestamp = int(time.time())
    start_timestamp = end_timestamp - (days * 24 * 60 * 60)
    
    query = f"""
    {{
        poolDayDatas(
            first: {days},
            orderBy: date,
            orderDirection: desc,
            where: {{
                pool: "{pool_id}",
                date_gte: {start_timestamp}
            }}
        ) {{
            date
            tvlUSD
            volumeUSD
            feesUSD
            txCount
            liquidity
            high
            low
            open
            close
        }}
    }}
    """
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'Uniswap-Risk-Dashboard/1.0'
            }
            
            async with session.post(
                url=UNISWAP_V3_SUBGRAPH_URL,
                json={"query": query},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                
                if response.status != 200:
                    logger.error(f"Historical data fetch failed: HTTP {response.status}")
                    return None
                
                data = await response.json()
                
                if "errors" in data:
                    logger.error(f"GraphQL errors: {data['errors']}")
                    return None
                
                historical_data = data.get("data", {}).get("poolDayDatas", [])
                
                if not historical_data:
                    logger.warning(f"No historical data found for pool {pool_id}")
                    return None
                
                # Process and sort the data (oldest first)
                processed_data = []
                for day_data in reversed(historical_data):
                    processed_data.append({
                        'date': datetime.fromtimestamp(int(day_data['date'])),
                        'tvl': float(day_data.get('tvlUSD', 0)),
                        'volume': float(day_data.get('volumeUSD', 0)),
                        'fees': float(day_data.get('feesUSD', 0)),
                        'tx_count': int(day_data.get('txCount', 0)),
                        'liquidity': float(day_data.get('liquidity', 0)),
                        'high': float(day_data.get('high', 0)),
                        'low': float(day_data.get('low', 0)),
                        'open': float(day_data.get('open', 0)),
                        'close': float(day_data.get('close', 0))
                    })
                
                logger.info(f"Fetched {len(processed_data)} days of historical data for pool {pool_id}")
                return processed_data
                
    except Exception as e:
        logger.error(f"Error fetching historical data: {e}")
        return None


async def fetch_aggregate_tvl_volatility(filtered_data, days: int) -> float:
    """Fetch historical data for all pools, aggregate daily TVL, and compute
    rolling volatility (std dev of daily returns) for the last `days` days.

    Returns percentage value (e.g., 12.34 for 12.34%).
    """
    if not filtered_data:
        return 0.0

    # Cache to avoid refetching repeatedly while navigating
    pool_ids = [pool.get('pool_id', pool.get('id', '')) for pool in filtered_data.values()]
    key = f"agg_vol_{days}_" + str(hash(tuple(sorted(pool_ids))))
    if 'cached_data' in st.session_state and key in st.session_state.cached_data:
        cached = st.session_state.cached_data[key]
        return cached.get('volatility', 0.0)

    # Concurrency limit to be gentle on the subgraph
    semaphore = asyncio.Semaphore(10)

    async def _fetch_with_limit(pid: str):
        async with semaphore:
            return await fetch_historical_pool_data(pid, days=days)

    tasks = []
    for pid in pool_ids:
        if pid:
            tasks.append(_fetch_with_limit(pid))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    from collections import defaultdict
    aggregate_by_date = defaultdict(float)

    for res in results:
        if isinstance(res, Exception) or not res:
            continue
        for d in res:
            # Use date only (drop time) to align days
            day_key = d['date'].date() if hasattr(d['date'], 'date') else d['date']
            aggregate_by_date[day_key] += float(d.get('tvl', 0.0))

    if not aggregate_by_date:
        return 0.0

    # Build time-ordered series for the most recent `days` entries
    sorted_days = sorted(aggregate_by_date.keys())[-days:]
    series = [aggregate_by_date[day] for day in sorted_days]

    # Compute daily returns
    returns = []
    for i in range(1, len(series)):
        prev = series[i-1]
        curr = series[i]
        if prev > 0:
            returns.append((curr - prev) / prev)

    if not returns:
        volatility = 0.0
    else:
        import numpy as np
        volatility = float(np.std(returns) * 100.0)

    # Store in cache
    if 'cached_data' not in st.session_state:
        st.session_state.cached_data = {}
    st.session_state.cached_data[key] = {
        'volatility': volatility,
        'timestamp': time.time()
    }

    return volatility


async def fetch_aggregate_overview(filtered_data, days: int):
    """Fetch historical data for all pools and return aggregated series and
    windowed aggregates for TVL, Volume, Fees, and TxCount.

    Returns dict with keys: dates, tvl_series, volume_series, fees_series,
    sums (volume_sum, fees_sum, tx_sum), avg_tvl, realized_fee_rate_by_tier.
    """
    if not filtered_data:
        return None

    # Cache by pool set and days
    pool_map = {symbol: data for symbol, data in filtered_data.items()}
    pool_ids_for_key = tuple(sorted([
        data.get('pool_id', data.get('id', '')) for data in pool_map.values()
    ]))
    cache_key = f"overview_{days}_{hash(pool_ids_for_key)}"
    if 'cached_data' in st.session_state and cache_key in st.session_state.cached_data:
        cached = st.session_state.cached_data[cache_key]
        return cached
    pool_ids = []
    fee_tiers = {}
    for symbol, pdata in pool_map.items():
        pid = pdata.get('pool_id', pdata.get('id', ''))
        if pid:
            pool_ids.append(pid)
            fee_tiers[pid] = pdata.get('fee_tier', 0)

    # Concurrency limit
    semaphore = asyncio.Semaphore(10)

    async def _fetch(pid: str):
        async with semaphore:
            return pid, await fetch_historical_pool_data(pid, days=days)

    tasks = [_fetch(pid) for pid in pool_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    from collections import defaultdict
    tvl_by_date = defaultdict(float)
    volume_by_date = defaultdict(float)
    fees_by_date = defaultdict(float)
    tx_by_date = defaultdict(int)
    fees_by_tier = defaultdict(float)
    volume_by_tier = defaultdict(float)

    for res in results:
        if isinstance(res, Exception) or not res:
            continue
        pid, data = res
        if not data:
            continue
        tier = fee_tiers.get(pid, 0)
        for d in data:
            day_key = d['date'].date() if hasattr(d['date'], 'date') else d['date']
            tvl_by_date[day_key] += float(d.get('tvl', 0.0))
            volume_val = float(d.get('volume', 0.0))
            fees_val = float(d.get('fees', 0.0))
            volume_by_date[day_key] += volume_val
            fees_by_date[day_key] += fees_val
            tx_by_date[day_key] += int(d.get('tx_count', 0))
            fees_by_tier[tier] += fees_val
            volume_by_tier[tier] += volume_val

    if not tvl_by_date:
        return None

    sorted_days = sorted(tvl_by_date.keys())[-days:]
    tvl_series = [tvl_by_date[d] for d in sorted_days]
    volume_series = [volume_by_date[d] for d in sorted_days]
    fees_series = [fees_by_date[d] for d in sorted_days]
    tx_series = [tx_by_date[d] for d in sorted_days]

    avg_tvl = sum(tvl_series) / len(tvl_series) if tvl_series else 0.0
    volume_sum = sum(volume_series)
    fees_sum = sum(fees_series)
    tx_sum = sum(tx_series)

    # Realized fee rate by tier (fees/volume)
    realized_fee_rate_by_tier = {}
    for tier, fsum in fees_by_tier.items():
        v = volume_by_tier.get(tier, 0.0)
        realized_fee_rate_by_tier[tier] = (fsum / v) * 100 if v > 0 else 0.0

    result = {
        'dates': sorted_days,
        'tvl_series': tvl_series,
        'volume_series': volume_series,
        'fees_series': fees_series,
        'tx_series': tx_series,
        'avg_tvl': avg_tvl,
        'volume_sum': volume_sum,
        'fees_sum': fees_sum,
        'tx_sum': tx_sum,
        'realized_fee_rate_by_tier': realized_fee_rate_by_tier,
    }

    if 'cached_data' not in st.session_state:
        st.session_state.cached_data = {}
    st.session_state.cached_data[cache_key] = result
    return result


async def fetch_per_pool_window_metrics(filtered_data, days: int):
    """Return per-pool windowed metrics computed from real subgraph data for last N days.

    For each pool: avg_tvl, avg_volume, fees_sum, tvl_vol, vol_vol, sharpe, sortino,
    max_drawdown, calmar, cvar95, tx_density, fee_yield, utilization, realized_fee_rate.
    """
    if not filtered_data:
        return {}

    # Cache using pool IDs and days
    pool_ids_for_key = tuple(sorted([
        pdata.get('pool_id', pdata.get('id', '')) for pdata in filtered_data.values()
    ]))
    cache_key = f"perpool_{days}_{hash(pool_ids_for_key)}"
    if 'cached_data' in st.session_state and cache_key in st.session_state.cached_data:
        return st.session_state.cached_data[cache_key]

    semaphore = asyncio.Semaphore(10)

    async def _fetch(symbol: str, pdata: dict):
        pid = pdata.get('pool_id', pdata.get('id', ''))
        if not pid:
            return symbol, None
        async with semaphore:
            hist = await fetch_historical_pool_data(pid, days=days)
            return symbol, hist

    tasks = [_fetch(symbol, pdata) for symbol, pdata in filtered_data.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    import numpy as np
    metrics = {}
    for res in results:
        if isinstance(res, Exception) or not res:
            continue
        symbol, hist = res
        if not hist:
            continue
        tvls = [float(d['tvl']) for d in hist]
        vols = [float(d['volume']) for d in hist]
        fees = [float(d['fees']) for d in hist]
        txs = [int(d['tx_count']) for d in hist]
        avg_tvl = float(np.mean(tvls)) if tvls else 0.0
        avg_volume = float(np.mean(vols)) if vols else 0.0
        fees_sum = float(np.sum(fees))
        tx_sum = int(np.sum(txs))
        # returns
        tvl_returns = []
        for i in range(1, len(tvls)):
            if tvls[i-1] > 0:
                tvl_returns.append((tvls[i]-tvls[i-1])/tvls[i-1])
        vol_returns = []
        for i in range(1, len(vols)):
            if vols[i-1] > 0:
                vol_returns.append((vols[i]-vols[i-1])/max(vols[i-1], 1e-9))
        tvl_vol = float(np.std(tvl_returns)) * 100 if tvl_returns else 0.0
        vol_vol = float(np.std(vol_returns)) * 100 if vol_returns else 0.0
        mean_ret = float(np.mean(tvl_returns)) if tvl_returns else 0.0
        std_ret = float(np.std(tvl_returns)) if tvl_returns else 0.0
        rf = 0.02/365
        sharpe = (mean_ret - rf) / std_ret if std_ret > 0 else 0.0
        downside = [r for r in tvl_returns if r < 0]
        down_std = float(np.std(downside)) if downside else 0.0
        sortino = (mean_ret - rf) / down_std if down_std > 0 else 0.0
        # max drawdown from cumulative returns
        if tvl_returns:
            cum = np.cumprod([1+r for r in tvl_returns])
            run_max = np.maximum.accumulate(cum)
            dd = (cum - run_max) / run_max
            max_dd = float(np.min(dd))
            annual_ret = mean_ret * 365
            calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0.0
            sorted_rets = sorted(tvl_returns)
            idx = int(0.05 * len(sorted_rets))
            cvar95 = float(np.mean(sorted_rets[:idx])) if idx > 0 else 0.0
        else:
            max_dd = 0.0
            calmar = 0.0
            cvar95 = 0.0
        fee_yield = (fees_sum / avg_tvl * 100) if avg_tvl > 0 else 0.0
        utilization = (np.sum(vols) / avg_tvl) if avg_tvl > 0 else 0.0
        tx_density = (tx_sum / avg_tvl) if avg_tvl > 0 else 0.0
        volume_sum = float(np.sum(vols))
        realized_fee_rate = (fees_sum / volume_sum * 100) if volume_sum > 0 else 0.0
        metrics[symbol] = {
            'avg_tvl': avg_tvl,
            'avg_volume': avg_volume,
            'fees_sum': fees_sum,
            'tx_sum': tx_sum,
            'tvl_vol': tvl_vol,
            'vol_vol': vol_vol,
            'sharpe': sharpe,
            'sortino': sortino,
            'max_dd': max_dd,
            'calmar': calmar,
            'cvar95': cvar95,
            'fee_yield': fee_yield,
            'utilization': utilization,
            'tx_density': tx_density,
            'realized_fee_rate': realized_fee_rate,
        }
    # store cache
    if 'cached_data' not in st.session_state:
        st.session_state.cached_data = {}
    st.session_state.cached_data[cache_key] = metrics
    return metrics

def create_pool_detail_view(selected_pool, pool_data, risk_analysis):
    """Create detailed view for selected pool"""
    st.subheader(f"Pool Details: {selected_pool}")
    
    # Pool overview metrics
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric(
            "Total Value Locked",
            format_large_number(pool_data.get('tvl', 0)),
            help="Current total value locked in the pool"
        )
    
    with col2:
        st.metric(
            "24h Volume",
            format_large_number(pool_data.get('volume', 0)),
            help="Trading volume in the last 24 hours"
        )
    
    with col3:
        st.metric(
            "Fee Tier",
            f"{pool_data.get('fee_tier', 0)*100:.2f}%",
            help="Trading fee percentage"
        )
    
    with col4:
        st.metric(
            "Risk Score",
            f"{risk_analysis['composite_score']:.3f}",
            help="Overall risk score (0-1)"
        )
    
    # Token information
    st.subheader("Token Information")
    
    token0 = pool_data.get('token0', {})
    token1 = pool_data.get('token1', {})
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown(f"**Token 0: {token0.get('symbol', 'Unknown')}**")
        st.markdown(f"- Name: {token0.get('name', 'N/A')}")
        st.markdown(f"- Address: `{token0.get('id', 'N/A')}`")
        st.markdown(f"- Decimals: {token0.get('decimals', 'N/A')}")
    
    with col2:
        st.markdown(f"**Token 1: {token1.get('symbol', 'Unknown')}**")
        st.markdown(f"- Name: {token1.get('name', 'N/A')}")
        st.markdown(f"- Address: `{token1.get('id', 'N/A')}`")
        st.markdown(f"- Decimals: {token1.get('decimals', 'N/A')}")
    
    # Risk breakdown
    st.subheader("Risk Analysis Breakdown")
    
    risk_components = risk_analysis['components']
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**Risk Components:**")
        st.markdown(f"- **Liquidity Risk**: {risk_components['liquidity_risk']:.3f}")
        st.markdown(f"- **Market Risk**: {risk_components['market_risk']:.3f}")
        st.markdown(f"- **Operational Risk**: {risk_components['operational_risk']:.3f}")
        st.markdown(f"- **Systemic Risk**: {risk_components['systemic_risk']:.3f}")
    
    with col2:
        st.markdown("**Risk Assessment:**")
        st.markdown(f"- **Risk Level**: {risk_analysis['risk_emoji']} {risk_analysis['risk_level']}")
        st.markdown(f"- **Description**: {risk_analysis['description']}")
    
    # Risk components visualization
    fig_risk = go.Figure(data=[
        go.Bar(
            x=['Liquidity', 'Market', 'Operational', 'Systemic'],
            y=[
                risk_components['liquidity_risk'],
                risk_components['market_risk'],
                risk_components['operational_risk'],
                risk_components['systemic_risk']
            ],
            marker_color=['#e74c3c', '#f39c12', '#3498db', '#9b59b6']
        )
    ])
    
    fig_risk.update_layout(
        title="Risk Components Breakdown",
        yaxis_title="Risk Score",
        yaxis=dict(range=[0, 1]),
        height=400
    )
    
    st.plotly_chart(fig_risk, use_container_width=True)
    
    # Pool metrics
    st.subheader("Pool Metrics")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric(
            "Capital Efficiency",
            f"{pool_data.get('capital_efficiency', 0):.3f}",
            help="Volume to TVL ratio - higher is better"
        )
    
    with col2:
        st.metric(
            "Volume Stability",
            f"{pool_data.get('volume_stability', 0):.3f}",
            help="Volume consistency over time - higher is better"
        )
    
    with col3:
        st.metric(
            "Transaction Count",
            f"{pool_data.get('tx_count', 0):,}",
            help="Total number of transactions"
        )
    
    # Fetch historical data once for reuse below
    with st.spinner("Fetching real historical data..."):
        pool_id = pool_data.get('pool_id', pool_data.get('id', ''))
        historical_data = asyncio.run(fetch_historical_pool_data(pool_id, days=30))

    # Advanced Risk & Performance Metrics
    st.subheader("Advanced Risk & Performance Metrics")
    
    # Calculate advanced metrics from historical data
    if historical_data and len(historical_data) > 1:
        # Extract data for calculations
        tvl_data = [day['tvl'] for day in historical_data]
        volume_data = [day['volume'] for day in historical_data]
        fees_data = [day['fees'] for day in historical_data]
        
        # Calculate daily returns
        daily_returns = []
        for i in range(1, len(tvl_data)):
            if tvl_data[i-1] > 0:
                daily_return = (tvl_data[i] - tvl_data[i-1]) / tvl_data[i-1]
                daily_returns.append(daily_return)
        
        if daily_returns:
            import numpy as np
            
            # Calculate metrics
            mean_return = np.mean(daily_returns)
            std_return = np.std(daily_returns)
            
            # Sharpe Ratio
            risk_free_rate = 0.02 / 365  # 2% annual risk-free rate
            sharpe_ratio = (mean_return - risk_free_rate) / std_return if std_return > 0 else 0
            
            # Sortino Ratio (only penalizes downside volatility)
            downside_returns = [r for r in daily_returns if r < 0]
            downside_std = np.std(downside_returns) if downside_returns else 0
            sortino_ratio = (mean_return - risk_free_rate) / downside_std if downside_std > 0 else 0
            
            # Maximum Drawdown
            cumulative_returns = np.cumprod([1 + r for r in daily_returns])
            running_max = np.maximum.accumulate(cumulative_returns)
            drawdowns = (cumulative_returns - running_max) / running_max
            max_drawdown = np.min(drawdowns)
            
            # Calmar Ratio
            annual_return = mean_return * 365
            calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0
            
            # Omega Ratio
            threshold = 0
            positive_returns = [r for r in daily_returns if r > threshold]
            negative_returns = [r for r in daily_returns if r < threshold]
            omega_ratio = sum(positive_returns) / abs(sum(negative_returns)) if negative_returns else 0
            
            # Gain to Pain Ratio
            total_gains = sum(positive_returns) if positive_returns else 0
            total_pain = abs(sum(negative_returns)) if negative_returns else 0
            gain_to_pain_ratio = total_gains / total_pain if total_pain > 0 else 0
            
            # CVaR (Conditional Value at Risk) - 95% confidence
            sorted_returns = sorted(daily_returns)
            var_95_index = int(0.05 * len(sorted_returns))
            cvar_95 = np.mean(sorted_returns[:var_95_index]) if var_95_index > 0 else 0
            
            # Display metrics in organized layout
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric("Sharpe Ratio", f"{sharpe_ratio:.3f}", help="Risk-adjusted return measure")
                st.metric("Sortino Ratio", f"{sortino_ratio:.3f}", help="Downside risk-adjusted return")
            
            with col2:
                st.metric("Max Drawdown", f"{max_drawdown:.3f}", help="Maximum peak-to-trough decline")
                st.metric("Calmar Ratio", f"{calmar_ratio:.3f}", help="Annual return / Max drawdown")
            
            with col3:
                st.metric("Omega Ratio", f"{omega_ratio:.3f}", help="Probability-weighted return ratio")
                st.metric("Gain/Pain Ratio", f"{gain_to_pain_ratio:.3f}", help="Total gains / Total losses")
            
            with col4:
                st.metric("CVaR (95%)", f"{cvar_95:.3f}", help="Conditional Value at Risk at 95% confidence")
                st.metric("Daily Volatility", f"{std_return:.3f}", help="Standard deviation of daily returns")
        
        else:
            st.warning("Insufficient data for advanced metrics calculation")
    
    else:
        st.warning("Historical data required for advanced metrics calculation")
    
    # Real Historical Trends
    st.subheader("Historical Trends")
    
    
    if historical_data:
        st.success(f"✅ Loaded {len(historical_data)} days of real historical data")
        
        # Extract data for plotting
        dates = [day['date'] for day in historical_data]
        tvl_data = [day['tvl'] for day in historical_data]
        volume_data = [day['volume'] for day in historical_data]
        
        # Create historical charts with real data
        fig_historical = go.Figure()
        
        fig_historical.add_trace(go.Scatter(
            x=dates,
            y=tvl_data,
            mode='lines+markers',
            name='TVL Trend',
            line=dict(color='#2ecc71', width=2),
            marker=dict(size=4)
        ))
        
        fig_historical.add_trace(go.Scatter(
            x=dates,
            y=volume_data,
            mode='lines+markers',
            name='Volume Trend',
            line=dict(color='#3498db', width=2),
            marker=dict(size=4),
            yaxis='y2'
        ))
        
        fig_historical.update_layout(
            title="30-Day Historical Trends (Real Data)",
            xaxis_title="Date",
            yaxis=dict(title="TVL ($)", side="left"),
            yaxis2=dict(title="Volume ($)", side="right", overlaying="y"),
            height=400,
            hovermode='x unified',
            showlegend=True
        )
        
        st.plotly_chart(fig_historical, use_container_width=True)
        
        # Historical statistics
        st.subheader("Historical Statistics")
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            avg_tvl = sum(tvl_data) / len(tvl_data) if tvl_data else 0
            st.metric("Avg TVL (30d)", format_large_number(avg_tvl))
        
        with col2:
            avg_volume = sum(volume_data) / len(volume_data) if volume_data else 0
            st.metric("Avg Volume (30d)", format_large_number(avg_volume))
        
        with col3:
            max_tvl = max(tvl_data) if tvl_data else 0
            st.metric("Max TVL (30d)", format_large_number(max_tvl))
        
        with col4:
            max_volume = max(volume_data) if volume_data else 0
            st.metric("Max Volume (30d)", format_large_number(max_volume))
        
        # Additional historical metrics
        if len(historical_data) > 1:
            st.subheader("Trend Analysis")
            
            # Calculate trends
            tvl_change = ((tvl_data[-1] - tvl_data[0]) / tvl_data[0] * 100) if tvl_data[0] > 0 else 0
            volume_change = ((volume_data[-1] - volume_data[0]) / volume_data[0] * 100) if volume_data[0] > 0 else 0
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.metric(
                    "TVL Change (30d)",
                    f"{tvl_change:+.2f}%",
                    delta=f"{tvl_change:+.2f}%"
                )
            
            with col2:
                st.metric(
                    "Volume Change (30d)",
                    f"{volume_change:+.2f}%",
                    delta=f"{volume_change:+.2f}%"
                )
    
    else:
        st.warning("⚠️ Unable to fetch historical data. Showing simulated data for demonstration.")
        
        # Fallback to simulated data
        dates = pd.date_range(start=datetime.now() - timedelta(days=30), end=datetime.now(), freq='D')
        
        # Simulate TVL trend
        base_tvl = pool_data.get('tvl', 0)
        tvl_trend = [base_tvl * (1 + 0.1 * (i/len(dates)) + 0.05 * (i % 7 - 3)/7) for i in range(len(dates))]
        
        # Simulate volume trend
        base_volume = pool_data.get('volume', 0)
        volume_trend = [base_volume * (1 + 0.2 * (i/len(dates)) + 0.1 * (i % 5 - 2)/5) for i in range(len(dates))]
        
        # Create historical charts
        fig_historical = go.Figure()
        
        fig_historical.add_trace(go.Scatter(
            x=dates,
            y=tvl_trend,
            mode='lines',
            name='TVL Trend (Simulated)',
            line=dict(color='#2ecc71', width=2)
        ))
        
        fig_historical.add_trace(go.Scatter(
            x=dates,
            y=volume_trend,
            mode='lines',
            name='Volume Trend (Simulated)',
            line=dict(color='#3498db', width=2),
            yaxis='y2'
        ))
        
        fig_historical.update_layout(
            title="30-Day Historical Trends (Simulated Data)",
            xaxis_title="Date",
            yaxis=dict(title="TVL ($)", side="left"),
            yaxis2=dict(title="Volume ($)", side="right", overlaying="y"),
            height=400,
            hovermode='x unified'
        )
        
        st.plotly_chart(fig_historical, use_container_width=True)
    
    # Pool comparison
    st.subheader("Pool Comparison")
    
    st.markdown("**Compare this pool with similar pools:**")
    
    # Simulate comparison data
    comparison_data = {
        'Metric': ['TVL', 'Volume', 'Fee Tier', 'Risk Score', 'Capital Efficiency'],
        'This Pool': [
            format_large_number(pool_data.get('tvl', 0)),
            format_large_number(pool_data.get('volume', 0)),
            f"{pool_data.get('fee_tier', 0)*100:.2f}%",
            f"{risk_analysis['composite_score']:.3f}",
            f"{pool_data.get('capital_efficiency', 0):.3f}"
        ],
        'Average (Similar Pools)': [
            format_large_number(pool_data.get('tvl', 0) * 0.8),
            format_large_number(pool_data.get('volume', 0) * 1.2),
            f"{pool_data.get('fee_tier', 0)*100:.2f}%",
            f"{risk_analysis['composite_score'] * 1.1:.3f}",
            f"{pool_data.get('capital_efficiency', 0) * 0.9:.3f}"
        ]
    }
    
    comparison_df = pd.DataFrame(comparison_data)
    st.dataframe(comparison_df, use_container_width=True)
    


redis_service = RedisService()
risk_engine = RiskEngine(redis_service=redis_service)


def create_sidebar_navigation():
    """Create the left sidebar navigation like in the reference image"""
    st.sidebar.title("DeFi Tracker")
    
    # Navigation menu
    st.sidebar.markdown("### Navigation")
    
    # Add custom styling for navigation buttons
    st.markdown("""
    <style>
    .nav-button {
        background-color: #f8fafc;
        color: #1e293b;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 0.5rem 1rem;
        margin: 0.25rem 0;
        width: 100%;
        text-align: center;
        font-weight: 500;
        transition: all 0.2s;
    }
    .nav-button:hover {
        background-color: #e2e8f0;
        border-color: #cbd5e1;
    }
    .nav-button.active {
        background-color: #3b82f6;
        color: white;
        border-color: #2563eb;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Create navigation buttons with proper symbols
    current_page = st.session_state.get('page', 'Dashboard')
    
    if st.sidebar.button("Dashboard", use_container_width=True, key="nav_dashboard"):
        st.session_state.page = "Dashboard"
    if st.sidebar.button("Pools", use_container_width=True, key="nav_pools"):
        st.session_state.page = "Pools"
    if st.sidebar.button("LPs", use_container_width=True, key="nav_lps"):
        st.session_state.page = "LPs"
    if st.sidebar.button("FAQ", use_container_width=True, key="nav_faq"):
        st.session_state.page = "FAQ"
    if st.sidebar.button("Settings", use_container_width=True, key="nav_settings"):
        st.session_state.page = "Settings"
    
    # Initialize page if not set
    if 'page' not in st.session_state:
        st.session_state.page = "Dashboard"
    
    return st.session_state.page


def get_pool_data(mode, min_tvl, max_pools, risk_filter, force_refresh=False):
    """Fetch and process pool data with caching"""
    # Check if we have cached data and don't need to refresh
    cache_key = f"pool_data_{mode}_{min_tvl}_{max_pools}_{hash(tuple(risk_filter))}"
    
    if not force_refresh and 'cached_data' in st.session_state and cache_key in st.session_state.cached_data:
        cached_data = st.session_state.cached_data[cache_key]
        return cached_data.get('filtered_data'), cached_data.get('risk_analysis')

    async def fetch_pool_data():
        async with UniswapDataFetcher(use_dynamic_discovery=(mode == "Dynamic Discovery")) as fetcher:
            return await fetcher.fetch_all_pools(use_dynamic_discovery=(mode == "Dynamic Discovery"))

    try:
        data = asyncio.run(fetch_pool_data())



        if not data:
            return None, None

        # Filter pools by TVL
        filtered_data = {
            symbol: pool_data for symbol, pool_data in data.items()
            if pool_data.get("tvl", 0) >= min_tvl
        }
        
        # Limit number of pools
        if len(filtered_data) > max_pools:
            sorted_pools = sorted(
                filtered_data.items(),
                key=lambda x: x[1].get("tvl", 0),
                reverse=True
            )
            filtered_data = dict(sorted_pools[:max_pools])

        if not filtered_data:
            return None, None

        # Calculate comprehensive risk analysis
        risk_engine = RiskEngine()
        risk_analysis = {}
        for symbol, pool_data in filtered_data.items():
            risk_analysis[symbol] = risk_engine.calculate_comprehensive_risk_score(pool_data)

        # Filter by risk level
        if risk_filter:
            filtered_data = {
                symbol: pool_data for symbol, pool_data in filtered_data.items()
                if risk_analysis[symbol]["risk_level"] in risk_filter
            }
            risk_analysis = {k: v for k, v in risk_analysis.items() if k in filtered_data}

        # Cache the data
        if 'cached_data' not in st.session_state:
            st.session_state.cached_data = {}
        
        st.session_state.cached_data[cache_key] = {
            'filtered_data': filtered_data,
            'risk_analysis': risk_analysis,
            'timestamp': time.time()
        }

        return filtered_data, risk_analysis

    except Exception as e:
        logger.error(f"Error fetching pool data: {e}")
        return None, None


def dashboard_page(filtered_data, risk_analysis):
    """Main dashboard page"""
    # Header with time filter like in the reference image
    col1, col2 = st.columns([4, 1])
    
    with col1:
        st.title("Uniswap v3 Risk Tracker")
        st.caption("Real-time protocol risk assessment")
    
    with col2:
        # Time filter dropdown like in the reference image
        time_filter = st.selectbox(
            "Time Range",
            ["7 Days", "30 Days", "90 Days"],
            index=0,
            label_visibility="collapsed"
        )
        # Store the selected timeframe in session state
        st.session_state.timeframe = time_filter
    
    if not filtered_data or not risk_analysis:
        st.warning("⚠️ No data available. Please check your configuration.")
        return

    # Key metrics cards like in the reference image
    st.subheader("Key Metrics")
    
    total_tvl = sum(pool_data.get("tvl", 0) for pool_data in filtered_data.values())
    total_volume = sum(pool_data.get("volume", 0) for pool_data in filtered_data.values())
    avg_risk = sum(analysis["composite_score"] for analysis in risk_analysis.values()) / len(risk_analysis) if risk_analysis else 0
    
    # Calculate real-time metrics
    # Active Liquidity: Calculate percentage of pools with recent activity
    active_pools = sum(1 for pool_data in filtered_data.values() 
                      if pool_data.get('volume', 0) > 0 and pool_data.get('tx_count', 0) > 0)
    active_liquidity = (active_pools / len(filtered_data) * 100) if filtered_data else 0
    
    # Fee APY: Calculate based on fee tier and volume
    # Use fee tier * volume as proxy for daily fees, then annualize
    total_daily_fees = 0
    for pool_data in filtered_data.values():
        fee_tier = pool_data.get('fee_tier', 0)  # This is in basis points (e.g., 3000 = 0.3%)
        volume = pool_data.get('volume', 0)
        daily_fees = volume * (fee_tier / 10000)  # Convert basis points to decimal
        total_daily_fees += daily_fees
    
    fee_apy = (total_daily_fees / total_tvl * 100 * 365) if total_tvl > 0 else 0  # Annualized
    
    # Rolling Volatility: std dev of aggregate TVL daily returns over N days
    # Get the selected timeframe
    timeframe = st.session_state.get('timeframe', '7 Days')
    timeframe_days = int(timeframe.split()[0])  # Extract number from "7 Days", "30 Days", etc.
    
    # Compute historical, aggregate TVL volatility for the selected window
    try:
        volatility = asyncio.run(fetch_aggregate_tvl_volatility(filtered_data, timeframe_days))
    except Exception as _e:
        logger.error(f"Failed to compute rolling volatility: {_e}")
        volatility = 0.0
    
    # Fetch aggregate historical overview for windowed metrics
    try:
        overview = asyncio.run(fetch_aggregate_overview(filtered_data, timeframe_days))
    except Exception as e:
        logger.error(f"Failed to compute overview metrics: {e}")
        overview = None

    # Windowed metrics
    if overview:
        avg_tvl_window = overview['avg_tvl']
        total_fees_window = overview['fees_sum']
        total_volume_window = overview['volume_sum']
        tx_sum_window = overview['tx_sum']
        protocol_fee_apy = total_fees_window / avg_tvl_window * 100 if avg_tvl_window > 0 else 0
        utilization_ratio = total_volume_window / avg_tvl_window if avg_tvl_window > 0 else 0
        # Changes from first to last day
        tvl_series = overview['tvl_series']
        volume_series = overview['volume_series']
        tvl_change = ((tvl_series[-1] - tvl_series[0]) / tvl_series[0] * 100) if tvl_series and tvl_series[0] > 0 else 0
        volume_change = ((volume_series[-1] - volume_series[0]) / volume_series[0] * 100) if volume_series and volume_series[0] > 0 else 0
        # Fee APY change based on daily fee_yield series
        fees_series = overview['fees_series']
        if fees_series and tvl_series and len(fees_series) == len(tvl_series):
            fee_yield_series = [
                (f / t * 100) if t > 0 else 0 for f, t in zip(fees_series, tvl_series)
            ]
            fee_apy_change = ((fee_yield_series[-1] - fee_yield_series[0]) / abs(fee_yield_series[0]) * 100) if fee_yield_series[0] != 0 else 0
        else:
            fee_apy_change = 0
        # Volatility change: compare first half vs full window
        import numpy as np
        if len(tvl_series) > 3:
            returns = []
            for i in range(1, len(tvl_series)):
                if tvl_series[i-1] > 0:
                    returns.append((tvl_series[i]-tvl_series[i-1]) / tvl_series[i-1])
            if returns:
                current_vol = float(np.std(returns) * 100)
                mid = max(2, len(returns)//2)
                initial_vol = float(np.std(returns[:mid]) * 100) if mid > 1 else 0.0
                volatility_change = ((current_vol - initial_vol) / abs(initial_vol) * 100) if initial_vol > 0 else 0
            else:
                volatility_change = 0
        else:
            volatility_change = 0
        # Active pools in window: at least one non-zero volume day
        active_pools_window = 0
        for symbol in filtered_data.keys():
            # heuristic: if pool contributes any volume in aggregate series, count active
            # We cannot attribute per-pool volumes without another pass; use tx_sum_window as proxy with current data
            pass
        # Use current active_liquidity metric for display delta context
        active_liquidity_change = volume_change
    else:
        avg_tvl_window = 0
        total_fees_window = 0
        total_volume_window = 0
        tx_sum_window = 0
        protocol_fee_apy = 0
        utilization_ratio = 0
        tvl_change = 0
        active_liquidity_change = 0
        fee_apy_change = 0
        volatility_change = 0
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
                        st.metric(
            "Total Value Locked", 
            format_large_number(total_tvl),
            delta=f"{tvl_change:+.1f}%"
                        )
    with col2:
                        st.metric(
            "Active Liquidity", 
            f"{active_liquidity:.1f}%",
            delta=f"{active_liquidity_change:+.1f}%"
        )
    with col3:
        st.metric(
            "Fee APY", 
            f"{fee_apy:.1f}%",
            delta=f"{fee_apy_change:+.1f}%"
        )
    with col4:
        st.metric(
            f"Rolling Volatility ({timeframe_days}d)", 
            f"{volatility:.1f}%",
            delta=f"{volatility_change:+.1f}%"
        )

    # Risk distribution chart
    st.subheader("Risk Distribution")
    
    risk_counts = {}
    for analysis in risk_analysis.values():
        level = analysis["risk_level"]
        risk_counts[level] = risk_counts.get(level, 0) + 1
    
    if risk_counts:
        fig_pie = px.pie(
            values=list(risk_counts.values()),
            names=list(risk_counts.keys()),
            title="Pool Risk Level Distribution",
            color_discrete_map={
                "Low Risk": "#2ecc71",
                "Medium Risk": "#f39c12", 
                "High Risk": "#e67e22",
                "Critical Risk": "#e74c3c"
            }
        )
        st.plotly_chart(fig_pie, use_container_width=True)

    # Risk heatmap
    st.subheader("Risk Heatmap")
    
    risk_matrix = []
    for symbol, analysis in risk_analysis.items():
        risk_matrix.append({
            "Pool": symbol,
            "Liquidity Risk": analysis["components"]["liquidity_risk"],
            "Market Risk": analysis["components"]["market_risk"],
            "Operational Risk": analysis["components"]["operational_risk"],
            "Systemic Risk": analysis["components"]["systemic_risk"],
        })
    
    risk_df = pd.DataFrame(risk_matrix)
    risk_df = risk_df.set_index("Pool")
    
    fig_heatmap = px.imshow(
        risk_df.T,
        title="Risk Component Heatmap",
        color_continuous_scale="RdYlGn_r",
        aspect="auto"
    )
    fig_heatmap.update_layout(height=400)
    st.plotly_chart(fig_heatmap, use_container_width=True)

    # New windowed overview metrics
    st.subheader(f"Windowed Metrics ({timeframe_days}d)")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Fees", format_large_number(total_fees_window))
    with col2:
        st.metric("Protocol Fee APY", f"{protocol_fee_apy:.2f}%")
    with col3:
        st.metric("Total Volume", format_large_number(total_volume_window))
    with col4:
        st.metric("Avg TVL", format_large_number(avg_tvl_window))

    # Aggregate TVL/Volume/Fees time series
    if overview:
        st.subheader(f"Aggregate TVL, Volume, Fees ({timeframe_days}d)")
        fig_agg = go.Figure()
        # TVL on left axis
        fig_agg.add_trace(go.Scatter(
            x=overview['dates'], y=overview['tvl_series'], mode='lines+markers',
            name='TVL', line=dict(color='#10b981', width=2), marker=dict(size=4)
        ))
        # Volume and Fees on right axis for comparable scale
        fig_agg.add_trace(go.Scatter(
            x=overview['dates'], y=overview['volume_series'], mode='lines+markers',
            name='Volume', yaxis='y2', line=dict(color='#3b82f6', width=2), marker=dict(size=3)
        ))
        fig_agg.add_trace(go.Scatter(
            x=overview['dates'], y=overview['fees_series'], mode='lines+markers',
            name='Fees', yaxis='y2', line=dict(color='#8b5cf6', width=2, dash='dot'), marker=dict(size=3)
        ))
        fig_agg.update_layout(
            height=420,
            hovermode='x unified',
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1.0),
            yaxis=dict(title='TVL ($)', side='left', showgrid=True, zeroline=False),
            yaxis2=dict(title='Volume / Fees ($)', overlaying='y', side='right', showgrid=False, zeroline=False)
        )
        st.plotly_chart(fig_agg, use_container_width=True)

        # Fee revenue by fee tier
        if overview['realized_fee_rate_by_tier']:
            st.subheader("Fee Revenue by Fee Tier (Realized Fee Rate)")
            tiers = sorted(overview['realized_fee_rate_by_tier'].keys())
            realized = [overview['realized_fee_rate_by_tier'][t] for t in tiers]
            label_map = {500: '0.05%', 3000: '0.3%', 10000: '1%'}
            labels = [label_map.get(t, f"{t/100:.2f}%") for t in tiers]
            fig_tiers = go.Figure(go.Bar(x=labels, y=realized, marker_color=['#10b981','#3b82f6','#8b5cf6']))
            fig_tiers.update_layout(yaxis_title='Realized Fee Rate (%)', height=360)
            st.plotly_chart(fig_tiers, use_container_width=True)

    # Top risky and safest pools
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Highest Risk Pools")
        risk_engine = RiskEngine()
        risky_pools = risk_engine.get_top_risky_pools(filtered_data, limit=5)
        for pool in risky_pools:
                        st.markdown(
                f"**{pool['symbol']}** - {pool['risk_emoji']} {pool['risk_level']} "
                f"(Score: {pool['risk_score']:.3f})"
            )
    
    with col2:
        st.subheader("Safest Pools")
        safe_pools = risk_engine.get_safest_pools(filtered_data, limit=5)
        for pool in safe_pools:
                        st.markdown(
                f"**{pool['symbol']}** - {pool['risk_emoji']} {pool['risk_level']} "
                f"(Score: {pool['risk_score']:.3f})"
            )


def pools_page(filtered_data, risk_analysis):
    """Detailed pools analysis page"""
    st.title("Pool Analysis")
    st.caption("Detailed analysis of all discovered pools")
    
    if not filtered_data or not risk_analysis:
        st.warning("⚠️ No data available. Please check your configuration.")
        return
    
    # Time window from dashboard timeframe
    timeframe = st.session_state.get('timeframe', '7 Days')
    timeframe_days = int(timeframe.split()[0])

    # Fetch per-pool windowed metrics (real data)
    with st.spinner(f"Computing {timeframe_days}d windowed metrics..."):
        try:
            window_metrics = asyncio.run(fetch_per_pool_window_metrics(filtered_data, timeframe_days))
        except Exception as e:
            logger.error(f"Failed to get per-pool metrics: {e}")
            window_metrics = {}

    # Create DataFrame for display
    pool_data_list = []
    for symbol, pool_data in filtered_data.items():
        analysis = risk_analysis[symbol]
        wm = window_metrics.get(symbol, {})
        pool_data_list.append({
            "Pool": symbol,
            "TVL": format_large_number(pool_data.get('tvl', 0)),
            "Volume 24h": format_large_number(pool_data.get('volume', 0)),
            "Fee Tier": f"{pool_data.get('fee_tier', 0)*100:.2f}%",
            "Risk Level": f"{analysis['risk_emoji']} {analysis['risk_level']}",
            "Risk Score": f"{analysis['composite_score']:.3f}",
            "Capital Efficiency": f"{pool_data.get('capital_efficiency', 0):.3f}",
            "Volume Stability": f"{pool_data.get('volume_stability', 0):.3f}",
            "Fee Yield (window)": f"{wm.get('fee_yield', 0):.2f}%",
            "Utilization (Vol/TVL)": f"{wm.get('utilization', 0):.2f}",
            "Tx Density": f"{wm.get('tx_density', 0):.6f}",
            "TVL Volatility": f"{wm.get('tvl_vol', 0):.2f}%",
            "Volume Volatility": f"{wm.get('vol_vol', 0):.2f}%",
            "Sharpe": f"{wm.get('sharpe', 0):.3f}",
            "Sortino": f"{wm.get('sortino', 0):.3f}",
        })
    
    df = pd.DataFrame(pool_data_list)
    
    # Single sorting dropdown
    st.markdown("**Sort Table By:**")
    sort_option = st.selectbox(
        "Choose sorting option:",
        [
            "TVL (Descending)",
            "Volume 24h (Descending)", 
            "Fee Tier (Descending)",
            "Risk Level (Descending)",
            "Risk Score (Descending)",
            "Capital Efficiency (Descending)",
            "Volume Stability (Descending)",
            "Fee Yield (window) (Descending)",
            "TVL Volatility (Descending)",
            "Volume Volatility (Descending)",
            "Sharpe (Descending)",
            "Sortino (Descending)",
            "Pool (Alphabetical)"
        ],
        index=0,
        help="Select how you want to sort the table"
    )
    
    # Apply sorting based on selected option
    if sort_option == "Pool (Alphabetical)":
        df = df.sort_values("Pool", ascending=True)
    elif sort_option == "TVL (Descending)":
        df["TVL_NUM"] = df["TVL"].apply(convert_to_numeric_for_sorting)
        df = df.sort_values("TVL_NUM", ascending=False).drop("TVL_NUM", axis=1)
    elif sort_option == "Volume 24h (Descending)":
        df["VOLUME_NUM"] = df["Volume 24h"].apply(convert_to_numeric_for_sorting)
        df = df.sort_values("VOLUME_NUM", ascending=False).drop("VOLUME_NUM", axis=1)
    elif sort_option == "Fee Tier (Descending)":
        df["FEE_NUM"] = df["Fee Tier"].str.replace("%", "").astype(float)
        df = df.sort_values("FEE_NUM", ascending=False).drop("FEE_NUM", axis=1)
    elif sort_option == "Risk Level (Descending)":
        df["RISK_NUM"] = df["Risk Level"].apply(get_risk_level_numeric)
        df = df.sort_values("RISK_NUM", ascending=False).drop("RISK_NUM", axis=1)
    elif sort_option == "Risk Score (Descending)":
        df["RISK_SCORE_NUM"] = df["Risk Score"].astype(float)
        df = df.sort_values("RISK_SCORE_NUM", ascending=False).drop("RISK_SCORE_NUM", axis=1)
    elif sort_option == "Capital Efficiency (Descending)":
        df["CAP_EFF_NUM"] = df["Capital Efficiency"].astype(float)
        df = df.sort_values("CAP_EFF_NUM", ascending=False).drop("CAP_EFF_NUM", axis=1)
    elif sort_option == "Volume Stability (Descending)":
        df["VOL_STAB_NUM"] = df["Volume Stability"].astype(float)
        df = df.sort_values("VOL_STAB_NUM", ascending=False).drop("VOL_STAB_NUM", axis=1)
    elif sort_option == "Fee Yield (window) (Descending)":
        df["FEE_YIELD_NUM"] = df["Fee Yield (window)"].str.replace('%','').astype(float)
        df = df.sort_values("FEE_YIELD_NUM", ascending=False).drop("FEE_YIELD_NUM", axis=1)
    elif sort_option == "TVL Volatility (Descending)":
        df["TVL_VOL_NUM"] = df["TVL Volatility"].str.replace('%','').astype(float)
        df = df.sort_values("TVL_VOL_NUM", ascending=False).drop("TVL_VOL_NUM", axis=1)
    elif sort_option == "Volume Volatility (Descending)":
        df["VOL_VOL_NUM"] = df["Volume Volatility"].str.replace('%','').astype(float)
        df = df.sort_values("VOL_VOL_NUM", ascending=False).drop("VOL_VOL_NUM", axis=1)
    elif sort_option == "Sharpe (Descending)":
        df["SHARPE_NUM"] = df["Sharpe"].astype(float)
        df = df.sort_values("SHARPE_NUM", ascending=False).drop("SHARPE_NUM", axis=1)
    elif sort_option == "Sortino (Descending)":
        df["SORTINO_NUM"] = df["Sortino"].astype(float)
        df = df.sort_values("SORTINO_NUM", ascending=False).drop("SORTINO_NUM", axis=1)
    
    # Add proper serial numbering (1 to N)
    df = df.reset_index(drop=True)
    df.insert(0, "#", range(1, len(df) + 1))
    
    # Display the table
    st.dataframe(df, use_container_width=True)

    # Charts
    st.subheader(f"Windowed Charts ({timeframe_days}d)")
    if window_metrics:
        charts_df = pd.DataFrame([
            {
                'Pool': sym,
                'avg_tvl': m['avg_tvl'],
                'volume_sum': m['avg_volume'] * timeframe_days,  # approx for plotting
                'fees_sum': m['fees_sum'],
                'fee_yield': m['fee_yield']
            }
            for sym, m in window_metrics.items()
        ])
        if not charts_df.empty:
            # Bar: Fees by Pool (top 15)
            topN = charts_df.sort_values('fees_sum', ascending=False).head(15)
            fig_bar = px.bar(topN, x='Pool', y='fees_sum', title='Fees by Pool (Top 15)')
            st.plotly_chart(fig_bar, use_container_width=True)

    
    # Pool Selection for Detailed View
    st.subheader("Pool Details")
    
    # Pool selection dropdown
    pool_names = list(filtered_data.keys())
    selected_pool = st.selectbox(
        "Select a pool for detailed analysis:",
        pool_names,
        help="Choose a pool to view detailed metrics and analysis"
    )
    
    if selected_pool:
        create_pool_detail_view(
            selected_pool, 
            filtered_data[selected_pool], 
            risk_analysis[selected_pool]
        )


def lps_page(filtered_data, risk_analysis):
    """Liquidity Providers page"""
    st.title("Liquidity Providers")
    st.caption("Analysis of liquidity provider behavior and performance")
    
    if not filtered_data:
        st.warning("⚠️ No data available. Please check your configuration.")
        return
    
    # Calculate comprehensive LP metrics
    total_tvl = sum(pool_data.get("tvl", 0) for pool_data in filtered_data.values())
    total_volume = sum(pool_data.get("volume", 0) for pool_data in filtered_data.values())
    
    # Calculate daily fees from volume and fee tier
    total_fees_daily = 0
    fee_tiers = []
    capital_efficiencies = []
    volume_stabilities = []
    
    for pool_data in filtered_data.values():
        fee_tier = pool_data.get('fee_tier', 0)
        volume = pool_data.get('volume', 0)
        daily_fees = volume * (fee_tier / 10000)
        total_fees_daily += daily_fees
        
        fee_tiers.append(fee_tier)
        capital_efficiencies.append(pool_data.get('capital_efficiency', 0))
        volume_stabilities.append(pool_data.get('volume_stability', 0))
    
    # Basic LP Metrics
    st.subheader("LP Overview")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Active Pools", len(filtered_data))
    with col2:
        total_positions = sum(pool_data.get('tx_count', 0) for pool_data in filtered_data.values())
        st.metric("Total Transactions", f"{total_positions:,}")
    with col3:
        avg_position_size = total_tvl / len(filtered_data) if filtered_data else 0
        st.metric("Average Pool Size", format_large_number(avg_position_size))
    with col4:
        st.metric("Total Volume (24h)", format_large_number(total_volume))
    
    # Performance Metrics
    st.subheader("Performance Metrics")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        avg_fee_rate = sum(fee_tiers) / len(fee_tiers) if fee_tiers else 0
        st.metric("Average Fee Rate", f"{avg_fee_rate:.2f}%")
    with col2:
        daily_apy = (total_fees_daily / total_tvl * 100) if total_tvl > 0 else 0
        st.metric("Daily APY", f"{daily_apy:.2f}%")
    with col3:
        annual_apy = daily_apy * 365
        st.metric("Annual APY", f"{annual_apy:.1f}%")
    with col4:
        st.metric("Total Daily Fees", format_large_number(total_fees_daily))
    
    # Efficiency Metrics
    st.subheader("Efficiency Metrics")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        avg_capital_efficiency = sum(capital_efficiencies) / len(capital_efficiencies) if capital_efficiencies else 0
        st.metric("Avg Capital Efficiency", f"{avg_capital_efficiency:.3f}")
    with col2:
        avg_volume_stability = sum(volume_stabilities) / len(volume_stabilities) if volume_stabilities else 0
        st.metric("Avg Volume Stability", f"{avg_volume_stability:.3f}")
    with col3:
        # Calculate TVL to Volume ratio
        tvl_volume_ratio = total_tvl / total_volume if total_volume > 0 else 0
        st.metric("TVL/Volume Ratio", f"{tvl_volume_ratio:.2f}")
    with col4:
        # Calculate fee efficiency (fees per TVL)
        fee_efficiency = total_fees_daily / total_tvl if total_tvl > 0 else 0
        st.metric("Fee Efficiency", f"{fee_efficiency:.4f}")
    
    # Risk Metrics
    st.subheader("Risk Metrics")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        # Count pools by risk level
        low_risk_pools = sum(1 for analysis in risk_analysis.values() if analysis.get('risk_level') == 'Low Risk')
        st.metric("Low Risk Pools", low_risk_pools)
    with col2:
        medium_risk_pools = sum(1 for analysis in risk_analysis.values() if analysis.get('risk_level') == 'Medium Risk')
        st.metric("Medium Risk Pools", medium_risk_pools)
    with col3:
        high_risk_pools = sum(1 for analysis in risk_analysis.values() if analysis.get('risk_level') == 'High Risk')
        st.metric("High Risk Pools", high_risk_pools)
    with col4:
        critical_risk_pools = sum(1 for analysis in risk_analysis.values() if analysis.get('risk_level') == 'Critical Risk')
        st.metric("Critical Risk Pools", critical_risk_pools)
    
    # Fee Tier Distribution
    st.subheader("Fee Tier Distribution")
    
    # Debug: Show unique fee tiers
    unique_fee_tiers = list(set(fee_tiers))
    st.caption(f"Debug: Found fee tiers: {unique_fee_tiers}")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        # Count pools by fee tier
        fee_005_pools = sum(1 for ft in fee_tiers if ft == 500)  # 0.05%
        st.metric("0.05% Fee Pools", fee_005_pools)
    with col2:
        fee_03_pools = sum(1 for ft in fee_tiers if ft == 3000)  # 0.3%
        st.metric("0.3% Fee Pools", fee_03_pools)
    with col3:
        fee_1_pools = sum(1 for ft in fee_tiers if ft == 10000)  # 1%
        st.metric("1% Fee Pools", fee_1_pools)




    # ===================== Windowed LP analytics (real data) =====================
    timeframe = st.session_state.get('timeframe', '7 Days')
    timeframe_days = int(timeframe.split()[0])
    st.subheader(f"Windowed LP Analytics ({timeframe_days}d)")

    # Fetch per-pool historical series concurrently
    async def _fetch_all_histories():
        semaphore = asyncio.Semaphore(10)
        async def _one(pool_id):
            async with semaphore:
                return await fetch_historical_pool_data(pool_id, days=timeframe_days)
        tasks = []
        pool_ids = {}
        pool_fee_tiers = {}
        for symbol, pdata in filtered_data.items():
            pid = pdata.get('pool_id', pdata.get('id', ''))
            if pid:
                tasks.append(_one(pid))
                pool_ids[len(tasks)-1] = symbol
                pool_fee_tiers[symbol] = pdata.get('fee_tier', 0)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # map back to symbol
        out = {}
        for idx, res in enumerate(results):
            sym = pool_ids.get(idx)
            if sym and not isinstance(res, Exception):
                out[sym] = res
        return out, pool_fee_tiers

    with st.spinner("Fetching historical data for LP analytics..."):
        try:
            pool_histories, pool_fee_tiers = asyncio.run(_fetch_all_histories())
        except Exception as e:
            logger.error(f"LP analytics history fetch failed: {e}")
            pool_histories, pool_fee_tiers = {}, {}

    # Compute per-tier fee yield and daily stacked fees
    if pool_histories:
        from collections import defaultdict
        import numpy as np

        # Per-day per-tier fees and TVL for stacked bar and yield-by-tier
        fees_by_day_tier = defaultdict(lambda: defaultdict(float))
        tvl_by_day_tier = defaultdict(lambda: defaultdict(float))
        # Per-pool aggregates
        per_pool_avg_tvl = {}
        per_pool_vol_sum = {}
        per_pool_fees_sum = {}

        for sym, series in pool_histories.items():
            if not series:
                continue
            tier = pool_fee_tiers.get(sym, 0)
            tvls = [float(d['tvl']) for d in series]
            vols = [float(d['volume']) for d in series]
            fees = [float(d['fees']) for d in series]
            dates = [d['date'].date() if hasattr(d['date'], 'date') else d['date'] for d in series]
            for i, day in enumerate(dates):
                fees_by_day_tier[day][tier] += fees[i]
                tvl_by_day_tier[day][tier] += tvls[i]
            per_pool_avg_tvl[sym] = float(np.mean(tvls)) if tvls else 0.0
            per_pool_vol_sum[sym] = float(np.sum(vols))
            per_pool_fees_sum[sym] = float(np.sum(fees))

        # Fee yield by tier and overall
        tier_yields = {}
        overall_fees = 0.0
        overall_avg_tvl_series = []
        for day, tiers in tvl_by_day_tier.items():
            overall_avg_tvl_series.append(sum(tiers.values()))
        overall_avg_tvl = (sum(overall_avg_tvl_series)/len(overall_avg_tvl_series)) if overall_avg_tvl_series else 0.0
        for tier in {t for tiers in fees_by_day_tier.values() for t in tiers.keys()}:
            fee_sum = sum(fees_by_day_tier[day].get(tier, 0.0) for day in fees_by_day_tier.keys())
            tvl_avg_series = [tvl_by_day_tier[day].get(tier, 0.0) for day in tvl_by_day_tier.keys()]
            tvl_avg = (sum(tvl_avg_series)/len(tvl_avg_series)) if tvl_avg_series else 0.0
            tier_yields[tier] = (fee_sum / tvl_avg * 100) if tvl_avg > 0 else 0.0
            overall_fees += fee_sum

        overall_yield = (overall_fees / overall_avg_tvl * 100) if overall_avg_tvl > 0 else 0.0

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Fee Yield 0.05%", f"{tier_yields.get(500, 0.0):.2f}%")
        with col2:
            st.metric("Fee Yield 0.3%", f"{tier_yields.get(3000, 0.0):.2f}%")
        with col3:
            st.metric("Fee Yield 1%", f"{tier_yields.get(10000, 0.0):.2f}%")
        with col4:
            st.metric("Fee Yield (Overall)", f"{overall_yield:.2f}%")

        # Concentration metrics using per-pool aggregates
        st.subheader("Concentration Metrics")
        tvl_values = sorted(per_pool_avg_tvl.values(), reverse=True)
        vol_values = sorted(per_pool_vol_sum.values(), reverse=True)
        top10_tvl_share = (sum(tvl_values[:10]) / sum(tvl_values) * 100) if sum(tvl_values) > 0 else 0.0
        top10_vol_share = (sum(vol_values[:10]) / sum(vol_values) * 100) if sum(vol_values) > 0 else 0.0

        def _gini(arr):
            if not arr:
                return 0.0
            arr = np.array(sorted(arr))
            n = len(arr)
            if n == 0:
                return 0.0
            cum = np.cumsum(arr)
            g = (n + 1 - 2 * np.sum(cum) / cum[-1]) / n if cum[-1] > 0 else 0.0
            return float(g)

        def _hhi(arr):
            s = sum(arr)
            if s <= 0:
                return 0.0
            shares = [x / s for x in arr]
            return float(sum(p*p for p in shares))

        gini_tvl = _gini(tvl_values)
        gini_vol = _gini(vol_values)
        hhi_tvl = _hhi(tvl_values)
        hhi_vol = _hhi(vol_values)

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Top-10 TVL Share", f"{top10_tvl_share:.1f}%")
        with col2:
            st.metric("Top-10 Volume Share", f"{top10_vol_share:.1f}%")
        with col3:
            st.metric("Gini (TVL)", f"{gini_tvl:.3f}")
        with col4:
            st.metric("HHI (TVL)", f"{hhi_tvl:.3f}")

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Gini (Volume)", f"{gini_vol:.3f}")
        with col2:
            st.metric("HHI (Volume)", f"{hhi_vol:.3f}")

        # Efficiency buckets
        st.subheader("Efficiency Buckets")
        utilizations = []
        for sym in per_pool_avg_tvl.keys():
            avg_tvl = per_pool_avg_tvl[sym]
            vol_sum = per_pool_vol_sum[sym]
            util = (vol_sum / avg_tvl) if avg_tvl > 0 else 0.0
            utilizations.append(util)
        total_pools = len(utilizations)
        b02 = sum(1 for u in utilizations if u > 0.2) / total_pools * 100 if total_pools else 0.0
        b05 = sum(1 for u in utilizations if u > 0.5) / total_pools * 100 if total_pools else 0.0
        b10 = sum(1 for u in utilizations if u > 1.0) / total_pools * 100 if total_pools else 0.0
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("> 0.2x", f"{b02:.1f}%")
        with col2:
            st.metric("> 0.5x", f"{b05:.1f}%")
        with col3:
            st.metric("> 1.0x", f"{b10:.1f}%")

        # Charts
        st.subheader("LP Charts")
        # Fee Revenue by Tier (stacked) per day
        tier_label = {500: '0.05%', 3000: '0.3%', 10000: '1%'}
        days_sorted = sorted(fees_by_day_tier.keys())
        stacked_df = pd.DataFrame([
            {
                'date': day,
                **{tier_label.get(t, str(t)): fees_by_day_tier[day].get(t, 0.0) for t in set().union(*[set(d.keys()) for d in fees_by_day_tier.values()])}
            }
            for day in days_sorted
        ])
        if not stacked_df.empty:
            fig_stack = px.bar(stacked_df, x='date', y=[c for c in stacked_df.columns if c != 'date'], title='Fee Revenue by Tier (daily)', barmode='stack')
            st.plotly_chart(fig_stack, use_container_width=True)

        # Capital Efficiency distribution (histogram)
        hist_df = pd.DataFrame({'utilization': utilizations})
        fig_hist = px.histogram(hist_df, x='utilization', nbins=30, title='Capital Efficiency Distribution (Volume/TVL)')
        st.plotly_chart(fig_hist, use_container_width=True)

        # Pareto chart of fees by pool
        fees_sorted = sorted(per_pool_fees_sum.items(), key=lambda x: x[1], reverse=True)
        if fees_sorted:
            names = [k for k, _ in fees_sorted]
            vals = [v for _, v in fees_sorted]
            cum = np.cumsum(vals) / sum(vals) * 100 if sum(vals) > 0 else np.zeros(len(vals))
            fig_pareto = go.Figure()
            fig_pareto.add_bar(x=names[:20], y=vals[:20], name='Fees')
            fig_pareto.add_scatter(x=names[:20], y=cum[:20], name='Cumulative %', yaxis='y2')
            fig_pareto.update_layout(yaxis=dict(title='Fees ($)'), yaxis2=dict(title='Cumulative %', overlaying='y', side='right'), title='Pareto: Fees by Pool (Top 20)')
            st.plotly_chart(fig_pareto, use_container_width=True)

        # Heatmap: utilization per pool per day
        heat_matrix = []
        heat_index = []
        heat_cols = [str(d) for d in days_sorted]
        for sym, series in pool_histories.items():
            row = []
            series_list = series if isinstance(series, list) else []
            for d in days_sorted:
                # find day entry
                val = next((s for s in series_list if (s.get('date').date() if hasattr(s.get('date'), 'date') else s.get('date')) == d), None)
                if val and val.get('tvl', 0) > 0:
                    row.append(val.get('volume', 0) / val.get('tvl', 1))
                else:
                    row.append(0.0)
            heat_matrix.append(row)
            heat_index.append(sym)
        if heat_matrix:
            heat_df = pd.DataFrame(heat_matrix, index=heat_index, columns=heat_cols)
            fig_heat = px.imshow(heat_df, aspect='auto', color_continuous_scale='Viridis', title='Utilization Heatmap (Volume/TVL)')
            st.plotly_chart(fig_heat, use_container_width=True)

def faq_page():
    """FAQ page with comprehensive help and explanations"""
    st.title("Frequently Asked Questions")
    st.caption("Everything you need to know about the DeFi Risk Dashboard")
    
    # General Questions
    st.subheader("General Questions")
    
    with st.expander("What is this dashboard and what does it do?"):
        st.markdown("""
        This dashboard discovers Uniswap V3 pools and produces real-time and historical risk analytics.
        
        - **Dynamic Pool Discovery**: Automatically fetches live pools from the Uniswap V3 subgraph (The Graph Gateway).
        - **Caching**: Results are cached in `st.session_state` to avoid re-fetching or re-computation across pages.
        - **Windowed Analytics**: 7/30/90-day metrics using real poolDayDatas (TVL, Volume, Fees, TxCount, Liquidity).
        - **Risk Scoring**: Composite score across Liquidity, Market, Operational, and Systemic dimensions.
        - **Dashboards**: Overview metrics, rolling volatility, and aggregate TVL/Volume/Fees charts.
        - **Pools Page**: Per‑pool windowed metrics (Fee Yield, Utilization, Tx Density, Volatility, Sharpe/Sortino) and Fees by Pool ranking.
        - **LPs Page**: Fee yield by tier, concentration (Top‑10 shares, HHI, Gini), efficiency buckets, stacked fees by tier, distributions, Pareto, and utilization heatmap.
        - **Themes & Settings**: Light/Dark/Auto themes, pool/risk filters, manual refresh.
        """)
    
    with st.expander("How does dynamic pool discovery work?"):
        st.markdown("""
        - Queries the Uniswap V3 subgraph via The Graph Gateway using your API key and subgraph ID.
        - Applies filters from Settings (Minimum TVL, Maximum Pools, Risk Levels).
        - Caches discovered pools and risk analysis; navigation does not trigger re-fetch.
        """)
    
    with st.expander("What data sources are used?"):
        st.markdown("""
        - **The Graph Gateway**: Uniswap V3 subgraph (live pools and poolDayDatas).
        - **Infura / Ethereum**: On-chain connectivity as needed.
        - **Chainlink**: Reference prices where applicable.
        - **Session Cache**: `st.session_state` for cached results and computed metrics.
        """)
    
    # Risk Analysis Questions
    st.subheader("Risk Analysis")
    
    with st.expander("How are risk scores calculated?"):
        st.markdown("""
        Risk scores are calculated using a comprehensive multi-factor model:
        
        Risk Components:
        - TVL Volatility (25%)
        - Measures how much TVL fluctuates over time
        - Higher volatility = higher risk
        
        Capital Efficiency (20%)
        - TVL vs Volume ratio
        - Higher efficiency = lower risk
        
        Fee Income Risk (20%)
        - Fee tier and volume consistency
        - Stable fees = lower risk
        
        Liquidity Risk (15%)
        - Pool depth and concentration
        - Deeper liquidity = lower risk
        
        Market Risk (10%)
        - Token price volatility
        - Stable tokens = lower risk
        
        Operational Risk (5%)
        - Pool age and transaction count
        - Established pools = lower risk
        
        Systemic Risk (5%)
        - Protocol and smart contract risks
        - Well-audited protocols = lower risk
        
        Final Score: Weighted average of all components (0-100 scale)
        """)
    
    with st.expander("What do the different risk levels mean?"):
        st.markdown("""
        Risk Level Classifications:
        - Low Risk (0-25)
        - Stable TVL and volume
        - High capital efficiency
        - Established pools with good liquidity
        - Suitable for conservative investors
        
        Medium Risk (26-50)
        - Moderate volatility
        - Decent capital efficiency
        - Some market fluctuations
        - Balanced risk-reward profile
        
        High Risk (51-75)
        - High volatility
        - Lower capital efficiency
        - Significant market risks
        - Requires active monitoring
        
        Critical Risk (76-100)
        - Extreme volatility
        - Very low capital efficiency
        - High market and operational risks
        - Not recommended for most users
        """)
    
    with st.expander("What is capital efficiency and why is it important?"):
        st.markdown("""
        Capital Efficiency measures how effectively a pool uses its locked capital:
        
        Formula: `Volume (24h) / TVL`
        
        Interpretation:
        - High Efficiency (>1.0): Pool generates more volume than its TVL
        - Medium Efficiency (0.5-1.0): Balanced volume-to-capital ratio
        - Low Efficiency (<0.5): Pool has excess capital relative to trading activity
        
        Why It Matters:
        - Higher efficiency = More fees generated per dollar locked
        - Lower efficiency = Capital sitting idle, earning fewer fees
        - Risk indicator: Inefficient pools may have liquidity issues
        
        Example: A pool with $1M TVL and $2M daily volume has 200% efficiency
        """)
    
    # Technical Questions
    st.subheader("Technical Questions")
    
    with st.expander("How often is the data updated and how does caching work?"):
        st.markdown("""
        - Live data is fetched when filters/timeframe change.
        - Heavy computations (aggregate overview and per‑pool windowed metrics) are cached by pool‑set and window length.
        - Switching pages reuses cached data; use Settings → Refresh Data Now to clear cache and re-fetch.
        - Subgraph indexing may introduce slight delays.
        """)
    
    with st.expander("What are the system requirements?"):
        st.markdown("""
        System Requirements:
        - Hardware:
        - Modern web browser (Chrome, Firefox, Safari, Edge)
        - Stable internet connection
        - No special hardware required
        
        Software:
        - Python 3.8+ (for local development)
        - Redis server (for caching and real-time features)
        - Required Python packages (see requirements.txt)
        
        Network:
        - Access to The Graph Protocol
        - Infura API connection
        - Standard web ports (80, 443)
        
        Performance:
        - Works on desktop and mobile devices
        - Optimized for modern browsers
        - Caching reduces data usage
        """)
    
    with st.expander("How do I set up the environment variables?"):
        st.markdown("""
        Required Environment Variables:
        - Infura URL
        - ```
        - INFURA_URL=https://mainnet.infura.io/v3/YOUR_PROJECT_ID
        ```
        - Get from [Infura.io](https://infura.io)
        - Provides Ethereum blockchain access
        
        Subgraph API Key
        - ```
        - subgraph_api_key=YOUR_API_KEY
        ```
        - Get from [The Graph Studio](https://thegraph.com/studio)
        - Required for accessing Uniswap V3 data
        
        Subgraph ID
        - ```
        - subgraph_id=5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV
        ```
        - Official Uniswap V3 subgraph ID
        - Used with API key for data access
        
        Redis Configuration
        - ```
        - REDIS_HOST=localhost
        REDIS_PORT=6379
        REDIS_DB=0
        ```
        - Default Redis settings
        - Change if using remote Redis
        
        Setup: Create a `.env` file in the project root with these variables.
        """)
    
    # Usage Questions
    st.subheader("Usage & Navigation")
    
    with st.expander("How do I navigate between sections and refresh data?"):
        st.markdown("""
        - Use the sidebar buttons to open **Dashboard**, **Pools**, **LPs**, **FAQ**, or **Settings**.
        - Time Range (7/30/90 Days) controls all windowed analytics.
        - Data is cached; click **Refresh Data Now** in Settings to clear cache and fetch fresh data.
        """)
    
    with st.expander("How do I filter and sort the pool data?"):
        st.markdown("""
        Filtering Options (in Settings):
        - Analysis Mode
        - Dynamic Discovery: All available pools
        - Legacy Pools: Pre-configured pools only
        
        Minimum TVL
        - Filter pools by minimum TVL amount
        - Default: $10,000
        - Range: $10 - $10,000,000
        
        Maximum Pools
        - Limit number of pools analyzed
        - Default: 100 pools
        - Range: 10 - 10,000 pools
        
        Risk Levels
        - Select which risk levels to display
        - Options: Low, Medium, High, Critical
        - Default: All risk levels
        
        Sorting Options (in Pools page):
        - TVL (Total Value Locked)
        - Volume (24h trading volume)
        - Fee Tier (0.05%, 0.3%, 1%)
        - Risk Level (Low to Critical)
        - Capital Efficiency
        - Volume Stability
        - Alphabetical (by token pair)
        """)
    
    with st.expander("What do the different metrics mean?"):
        st.markdown("""
        Key Metrics Explained:
        - TVL (Total Value Locked)
        - Total value of tokens in the pool
        - Higher TVL = more liquidity
        - Measured in USD
        
        Volume (24h)
        - Total trading volume in last 24 hours
        - Higher volume = more activity
        - Measured in USD
        
        Fee Tier
        - Trading fee percentage (0.05%, 0.3%, 1%)
        - Higher fees = more revenue for LPs
        - Set when pool is created
        
        Risk Score
        - Comprehensive risk assessment (0-100)
        - Lower score = lower risk
        - Based on multiple factors
        
        Capital Efficiency
        - Volume/TVL ratio
        - Higher efficiency = better capital utilization
        - Measured as percentage
        
        Volume Stability
        - Consistency of trading volume
        - Higher stability = more predictable
        - Measured as coefficient of variation
        
        Transaction Count
        - Number of swaps in the pool
        - Higher count = more activity
        - Indicates pool popularity
        """)
    
    # Troubleshooting
    st.subheader("Troubleshooting")
    
    with st.expander("The dashboard shows 'No data available' - what should I do?"):
        st.markdown("""
        Common Causes & Solutions:
        - Check Environment Variables
        - Verify `.env` file exists and is properly formatted
        - Ensure Infura URL is complete (not just project ID)
        - Confirm subgraph API key and ID are correct
        
        Network Issues
        - Check internet connection
        - Verify access to The Graph Protocol
        - Try refreshing the page
        
        Redis Connection
        - Ensure Redis server is running
        - Check Redis configuration in `.env`
        - Restart Redis if needed
        
        Data Refresh
        - Use "Refresh Data Now" button in Settings
        - Clear browser cache
        - Restart the application
        
        API Limits
        - Check if API rate limits are exceeded
        - Verify API key permissions
        - Contact support if issues persist
        
        Debug Steps:
        - 1. Check console for error messages
        - 2. Verify all environment variables
        3. Test subgraph connection
        4. Restart Redis server
        5. Refresh data manually
        """)
    
    with st.expander("The risk scores seem incorrect - how can I verify them?"):
        st.markdown("""
        Risk Score Verification:
        - Understanding Risk Components
        - Risk scores are calculated from multiple factors
        - Each component has different weights
        - Scores are relative to other pools
        
        Manual Verification
        - Check TVL volatility in historical data
        - Verify capital efficiency calculations
        - Compare with similar pools
        
        Adjusting Risk Parameters
        - Risk weights can be modified in `config.py`
        - Thresholds can be adjusted for different risk levels
        - Custom risk models can be implemented
        
        Data Quality
        - Ensure data is up-to-date
        - Check for missing or incomplete data
        - Verify token price accuracy
        
        Recalculation
        - Use "Refresh Data Now" to recalculate
        - Risk scores update with new data
        - Historical data affects volatility calculations
        
        Note: Risk scores are estimates based on available data and should be used as guidance, not absolute truth.
        """)
    
    with st.expander("Performance is slow - how can I optimize it?"):
        st.markdown("""
        Performance Optimization Tips:
        - Reduce Data Load
        - Lower "Maximum pools" setting
        - Increase "Minimum TVL" filter
        - Use "Legacy Pools" mode for testing
        
        Enable Caching
        - Data is automatically cached
        - Avoid unnecessary refreshes
        - Use manual refresh only when needed
        
        Network Optimization
        - Use stable internet connection
        - Close unnecessary browser tabs
        - Clear browser cache regularly
        
        Smart Refresh
        - Only refresh when making important decisions
        - Use page navigation instead of full refresh
        - Monitor data update frequency
        
        System Resources
        - Close other applications
        - Use modern browser
        - Ensure sufficient RAM
        
        Configuration
        - Adjust batch sizes in `config.py`
        - Optimize Redis settings
        - Use local Redis for better performance
        
        Expected Performance:
        - Initial load: 10-30 seconds
        - Page navigation: 1-3 seconds
        - Data refresh: 5-15 seconds
        """)
    
    # Contact & Support
    st.subheader("Support & Contact")
    
    with st.expander("Where can I get help or report issues?"):
        st.markdown("""
        Getting Help:
        - Documentation
        - Check this FAQ section first
        - Review README.md for setup instructions
        - Examine code comments for technical details
        
        Reporting Issues
        - Describe the problem clearly
        - Include error messages
        - Provide system information
        - Share relevant configuration
        
        Feature Requests
        - Suggest new features
        - Request improvements
        - Provide use case examples
        
        Technical Support
        - Check environment setup
        - Verify API connections
        - Test with minimal configuration
        - Provide logs and error details
        
        Learning Resources
        - Uniswap V3 documentation
        - The Graph Protocol guides
        - DeFi risk management best practices
        - Liquidity provision strategies
        
        Community:
        - DeFi communities and forums
        - Uniswap Discord and Telegram
        - The Graph Protocol community
        - Open source development communities
        """)
    
    # Footer
    st.markdown("---")
    st.info(" Tip: Use Ctrl+F (Cmd+F on Mac) to search for specific topics in this FAQ section!")


def settings_page():
    """Settings and configuration page"""
    st.title("Settings")
    st.caption("Configure dashboard settings and preferences")
    
    st.subheader("Pool Filtering")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Pool Discovery Settings")
        
        # Analysis Mode
        analysis_mode = st.selectbox(
            "Analysis Mode",
            ["Dynamic Discovery", "Legacy Pools"],
            index=0,
            help="Dynamic Discovery: Automatically find all pools | Legacy: Use hardcoded pools"
        )
        st.session_state.analysis_mode = analysis_mode
        
        # Minimum TVL Filter
        min_tvl = st.number_input(
            "Minimum TVL ($)",
            min_value=10,
            max_value=10000000,
            value=10000,
            step=1000,
            help="Filter pools by minimum TVL"
        )
        st.session_state.min_tvl = min_tvl
        
        # Maximum Pools Filter
        max_pools = st.number_input(
            "Maximum pools to analyze",
            min_value=10,
            max_value=10000,
            value=100,
            step=10,
            help="Limit number of pools for performance"
        )
        st.session_state.max_pools = max_pools
    
    with col2:
        st.subheader("Risk Level Filtering")
        
        # Risk level filter
        risk_filter = st.multiselect(
            "Risk Levels",
            ["Low Risk", "Medium Risk", "High Risk", "Critical Risk"],
            default=["Low Risk", "Medium Risk", "High Risk", "Critical Risk"],
            help="Filter pools by risk level"
        )
        st.session_state.risk_filter = risk_filter
        
        # Apply filters button
        if st.button("🔄 Apply Filters & Refresh", type="primary", use_container_width=True):
            # Clear cache to force refresh with new filters
            if 'cached_data' in st.session_state:
                del st.session_state.cached_data
            st.success("Filters applied! Data will refresh with new settings.")
            st.rerun()
    
    st.subheader("Data Management")
    
    # Manual refresh button
    if st.button("🔄 Refresh Data Now", type="primary", use_container_width=True):
        # Clear cache to force refresh
        if 'cached_data' in st.session_state:
            del st.session_state.cached_data
        st.success("Data refresh initiated!")
        st.rerun()
    
    # Cache status
    if 'cached_data' in st.session_state and st.session_state.cached_data:
        cache_count = len(st.session_state.cached_data)
        st.info(f"📊 {cache_count} data sets cached")
        
        # Show cache age
        latest_cache = max(st.session_state.cached_data.values(), key=lambda x: x.get('timestamp', 0))
        cache_age = time.time() - latest_cache.get('timestamp', 0)
        st.caption(f"Last updated: {int(cache_age)} seconds ago")
    else:
        st.warning("No cached data available")
    
    st.subheader("Display Settings")
    col1, col2 = st.columns(2)
    
    with col1:
        theme = st.selectbox("Theme", ["Light", "Dark", "Auto"], index=2)
        st.session_state.theme = theme
    
    with col2:
        st.checkbox("Enable Dynamic Pool Discovery", value=True)
        st.checkbox("Enable Historical Data", value=True)
        st.checkbox("Enable Real-time Alerts", value=True)


def apply_theme(theme):
    """Apply theme styling to the dashboard"""
    if theme == "Dark":
        st.markdown("""
        <style>
        .stApp {
            background-color: #0e1117;
            color: #fafafa;
        }
        .main .block-container {
            background-color: #0e1117;
            color: #fafafa;
        }
        .stSidebar {
            background-color: #1e1e1e;
            color: #fafafa;
        }
        .stSelectbox > div > div {
            background-color: #262730;
            color: #fafafa;
        }
        .stNumberInput > div > div > input {
            background-color: #262730;
            color: #fafafa;
        }
        .stMultiSelect > div > div {
            background-color: #262730;
            color: #fafafa;
        }
        .stButton > button {
            background-color: #1f2937;
            color: white;
            border: none;
        }
        .stButton > button:hover {
            background-color: #374151;
        }
        .stSuccess {
            background-color: #1e3a1e;
            color: #4ade80;
        }
        .stWarning {
            background-color: #3a1e1e;
            color: #fbbf24;
        }
        .stInfo {
            background-color: #1e3a3a;
            color: #60a5fa;
        }
        /* Metric cards */
        div[data-testid="stMetric"] {
            background-color: #262730;
            border: 1px solid #404040;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.35);
            margin-bottom: 12px;
        }
        .stDataFrame {
            background-color: #262730;
        }
        .stSelectbox label {
            color: #fafafa;
        }
        .stNumberInput label {
            color: #fafafa;
        }
        .stMultiSelect label {
            color: #fafafa;
        }
        .stCheckbox label {
            color: #fafafa;
        }
        </style>
        """, unsafe_allow_html=True)
    elif theme == "Light":
        st.markdown("""
        <style>
        .stApp {
            background-color: #ffffff;
            color: #262730;
        }
        .main .block-container {
            background-color: #ffffff;
            color: #262730;
        }
        .stSidebar {
            background-color: #f0f2f6;
            color: #262730;
        }
        .stSelectbox > div > div {
            background-color: #ffffff;
            color: #262730;
        }
        .stNumberInput > div > div > input {
            background-color: #ffffff;
            color: #262730;
        }
        .stMultiSelect > div > div {
            background-color: #ffffff;
            color: #262730;
        }
        .stButton > button {
            background-color: #1f2937;
            color: white;
            border: none;
        }
        .stButton > button:hover {
            background-color: #374151;
        }
        .stSuccess {
            background-color: #d1fae5;
            color: #065f46;
        }
        .stWarning {
            background-color: #fef3c7;
            color: #92400e;
        }
        .stInfo {
            background-color: #dbeafe;
            color: #1e40af;
        }
        /* Metric cards */
        div[data-testid="stMetric"] {
            background-color: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 2px 6px rgba(16,24,40,0.05);
            margin-bottom: 12px;
        }
        .stDataFrame {
            background-color: #ffffff;
        }
        .stSelectbox label {
            color: #262730;
        }
        .stNumberInput label {
            color: #262730;
        }
        .stMultiSelect label {
            color: #262730;
        }
        .stCheckbox label {
            color: #262730;
        }
        </style>
        """, unsafe_allow_html=True)
    # Auto theme uses default Streamlit styling, but keep metric cards consistent
    else:
        st.markdown("""
        <style>
        div[data-testid="stMetric"] {
            background-color: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 2px 6px rgba(16,24,40,0.05);
            margin-bottom: 12px;
        }
        </style>
        """, unsafe_allow_html=True)

def main():
    st.set_page_config(
        page_title="Uniswap V3 Dynamic Risk Dashboard",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    
    # Apply theme if set
    current_theme = st.session_state.get('theme', 'Auto')
    apply_theme(current_theme)

    # Create sidebar navigation like in the reference image
    page = create_sidebar_navigation()
    
    # Get settings from session state or use defaults
    mode = st.session_state.get('analysis_mode', "Dynamic Discovery")
    min_tvl = st.session_state.get('min_tvl', 10000)
    max_pools = st.session_state.get('max_pools', 100)
    risk_filter = st.session_state.get('risk_filter', ["Low Risk", "Medium Risk", "High Risk", "Critical Risk"])
    
    # Get pool data (shared across pages) with caching
    filtered_data, risk_analysis = get_pool_data(mode, min_tvl, max_pools, risk_filter, force_refresh=False)
    
    # Display status
    if filtered_data:
        st.success(f"✅ Connected - {len(filtered_data)} pools loaded")
    else:
        st.warning("⚠️ No data available - check configuration")
    
    # Route to appropriate page
    if page == "Dashboard":
        dashboard_page(filtered_data, risk_analysis)
    elif page == "Pools":
        pools_page(filtered_data, risk_analysis)
    elif page == "LPs":
        lps_page(filtered_data, risk_analysis)
    elif page == "FAQ":
        faq_page()
    elif page == "Settings":
        settings_page()
    
    # Footer
    st.markdown("---")
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    st.caption(f"Last updated: {timestamp}")
    



if __name__ == "__main__":
    main()
