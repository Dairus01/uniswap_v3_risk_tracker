# Uniswap Pool Risk Analysis Dashboard

![Pipeline Architecture](assets/pipeline_diagram.png) <!-- Add a diagram later if possible -->

A real-time monitoring system that calculates risk metrics for Uniswap liquidity pools and visualizes insights through an interactive dashboard.

## Key Features
- **Automated Data Pipeline**  
  - Extracts on-chain data from 3 Uniswap V3 pools
  - Processes and formats raw blockchain data
  - Calculates risk metrics at user-defined intervals
- **Risk Engine**  
  - TVL volatility analysis
  - Liquidity concentration metrics
  - Impermanent loss risk indicators
  - Price impact calculations
- **Dashboard**  
  - Streamlit-based visualization interface
  - Configurable data refresh rate (seconds)
  - Comparative analysis across multiple pools
- **Tech Stack**  
  - **Redis**: Real-time TVL volatility calculations
  - **Python**: Pandas for data processing, Web3.py for blockchain interaction
  - **Streamlit**: Interactive dashboard
  - **GitHub Actions**: CI/CD pipeline (tests in progress)

## Pipeline Architecture
```mermaid
graph LR
A[Uniswap Subgraph] -->|Extract| B[Raw Data]
B --> C[Data Formatter]
C --> D[Risk Engine]
D -->|Redis Cache| E[Volatility Calc]
E --> F[Streamlit Dashboard]
