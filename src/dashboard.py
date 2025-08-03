import streamlit as st
import asyncio
import time
import logging

from pipeline import UniswapDataFetcher
from redis_service import RedisService
from risk_engine import RiskEngine

from config import POOLS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Dashboard")


redis_service = RedisService()
risk_engine = RiskEngine(redis_service=redis_service)


def main():

    st.set_page_config(
        page_title="Uniswap V3 Risk Monitoring",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("🦄 Real-Time Uniswap V3 TVL Dashboard")
    st.caption("Monitoring Total Value Locked in Liquidity Pools")

    refresh_interval = st.sidebar.slider(
        label="Refresh interval (seconds)",
        min_value=60,
        max_value=300,
        value=15,
        key="refresh_interval",
    )

    status_bar = st.empty()
    data_placeholder = st.empty()
    last_update = st.empty()

    async def fetch_pool_data():
        async with UniswapDataFetcher() as fetcher:
            return await fetcher.fetch_all_pools()

    def update_display():
        status_bar.info("⏳ Fetching real-time on chain data...")

        try:
            data = asyncio.run(fetch_pool_data())

            if not data:
                status_bar.warning("⚠️ No data received - check connection")
                return

            volatility_data = {}
            for symbol, pool_data in data.items():
                volatility = risk_engine.calculate_tvl_volatility(
                    symbol, pool_data.get("tvl", 0)
                )
                volatility_data[symbol] = volatility

            with data_placeholder.container():
                st.subheader("Current Pool TVL")
                cols = st.columns(len(data))

                for (symbol, pool_data), col in zip(data.items(), cols):
                    tvl = pool_data.get("tvl", 0)
                    volatility = volatility_data[symbol]

                    with col:
                        st.markdown(f"**{symbol} Pool**")
                        st.metric(
                            label="Total Value Locked",
                            value=f"${tvl/1e6:,.2f}M",
                        )
                        st.metric(
                            label=f"{refresh_interval} seconds: TVL Volatility",
                            value=f"{volatility:.2f}%",
                            help="Volatility of TVL over 5-minute windows",
                        )

                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                last_update.caption(f"Last updated: {timestamp}")

            status_bar.empty()
        except Exception as e:
            logger.error(f"data fetch failed: {str(e)}")
            status_bar.error("❌ Error fetching data - check logs")

    update_display()
    last_refresh = time.time()
    while True:
        current_time = time.time()
        if current_time - last_refresh > refresh_interval:
            update_display()
            last_refresh = current_time
        time.sleep(1)


if __name__ == "__main__":
    main()
