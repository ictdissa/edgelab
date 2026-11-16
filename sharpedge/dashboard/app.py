"""SharpEdge Sports Streamlit dashboard."""

from __future__ import annotations

import argparse
import os

import pandas as pd
import streamlit as st

from sharpedge.config.settings import Settings
from sharpedge.execution.risk_manager import RiskManager
from sharpedge.research.metrics import summarize_clv
from sharpedge.research.pipeline import build_research_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mock", action="store_true")
    args, _ = parser.parse_known_args()
    return args


def signal_rows(snapshot) -> list[dict[str, object]]:
    market_by_id = {market.market_id: market for market in snapshot.markets}
    rows = []
    for signal in snapshot.signals:
        market = market_by_id.get(signal.market_id)
        rows.append(
            {
                "sport": market.sport if market else "",
                "event": market.event_id if market else signal.event_id,
                "market": market.title if market else signal.market_id,
                "outcome": signal.outcome,
                "external fair probability": signal.fair_probability,
                "Polymarket bid": signal.polymarket_bid,
                "Polymarket ask": signal.polymarket_ask,
                "raw edge": signal.raw_edge,
                "adjusted edge": signal.adjusted_edge,
                "available size": signal.available_size,
                "signal type": signal.signal_type,
                "confidence": signal.confidence,
                "action": signal.action,
                "reason": signal.reason,
                "market status": market.status if market else "",
            }
        )
    return rows


def main() -> None:
    args = parse_args()
    mock = args.mock or os.getenv("SHARPEDGE_MOCK", "").lower() == "true"
    settings = Settings()
    snapshot = build_research_snapshot(settings, mock=mock)

    st.set_page_config(page_title="SharpEdge Sports", layout="wide")
    st.title("SharpEdge Sports")
    st.caption(f"Research and paper trading mode. as_of={snapshot.as_of.isoformat()} mock={mock}")

    signals_df = pd.DataFrame(signal_rows(snapshot))
    with st.sidebar:
        st.header("Filters")
        sports = sorted(signals_df["sport"].dropna().unique()) if not signals_df.empty else []
        selected_sports = st.multiselect("sport", sports, default=sports)
        min_edge = st.slider("minimum adjusted edge", -0.10, 0.20, float(settings.min_adjusted_edge), 0.005)
        min_liquidity = st.slider("minimum liquidity", 0.0, 500.0, float(settings.min_liquidity), 5.0)
        signal_types = sorted(signals_df["signal type"].dropna().unique()) if not signals_df.empty else []
        selected_signal_types = st.multiselect("signal type", signal_types, default=signal_types)
        statuses = sorted(signals_df["market status"].dropna().unique()) if not signals_df.empty else []
        selected_statuses = st.multiselect("market status", statuses, default=statuses)

    filtered = signals_df.copy()
    if not filtered.empty:
        filtered = filtered[
            filtered["sport"].isin(selected_sports)
            & (filtered["adjusted edge"] >= min_edge)
            & (filtered["available size"] >= min_liquidity)
            & filtered["signal type"].isin(selected_signal_types)
            & filtered["market status"].isin(selected_statuses)
        ]

    top, stale, inconsistencies, orderbook, trades, clv, pnl, risk, freshness = st.tabs(
        [
            "Live edge scanner",
            "Top stale-price opportunities",
            "Cross-market inconsistencies",
            "Order-book view",
            "Paper trades",
            "CLV analytics",
            "PnL analytics",
            "Risk status",
            "Data freshness monitor",
        ]
    )

    with top:
        st.dataframe(filtered, use_container_width=True, hide_index=True)

    with stale:
        stale_df = filtered.sort_values("adjusted edge", ascending=False).head(25)
        st.dataframe(stale_df, use_container_width=True, hide_index=True)

    with inconsistencies:
        inc_df = pd.DataFrame([item.model_dump() for item in snapshot.inconsistencies])
        st.dataframe(inc_df, use_container_width=True, hide_index=True)

    with orderbook:
        ob_rows = []
        market_titles = {market.market_id: market.title for market in snapshot.markets}
        for book in snapshot.orderbooks:
            bba = book.best_bid_ask()
            ob_rows.append(
                {
                    "market": market_titles.get(book.market_id, book.market_id),
                    "token_id": book.token_id,
                    "timestamp": book.timestamp,
                    "best_bid": bba.best_bid,
                    "best_bid_size": bba.best_bid_size,
                    "best_ask": bba.best_ask,
                    "best_ask_size": bba.best_ask_size,
                    "spread": bba.spread,
                }
            )
        st.dataframe(pd.DataFrame(ob_rows), use_container_width=True, hide_index=True)

    with trades:
        st.info("Paper trade records appear here when run_paper_trader persists decisions to storage.")

    with clv:
        st.json(summarize_clv([]))

    with pnl:
        st.metric("Mock realized PnL", "0.0000")
        st.metric("Mock unrealized PnL", "0.0000")

    with risk:
        rm = RiskManager(settings)
        st.json(rm.state.model_dump())
        st.write(
            {
                "live_trading_enabled": settings.enable_live_trading,
                "max_position_per_market": settings.max_position_per_market,
                "max_position_per_event": settings.max_position_per_event,
                "max_daily_loss": settings.max_daily_loss,
                "min_adjusted_edge": settings.min_adjusted_edge,
            }
        )

    with freshness:
        now = snapshot.as_of
        freshness_rows = []
        for book in snapshot.orderbooks:
            freshness_rows.append(
                {
                    "type": "orderbook",
                    "id": f"{book.market_id}:{book.token_id}",
                    "timestamp": book.timestamp,
                    "age_seconds": (now - book.timestamp).total_seconds(),
                }
            )
        for row in snapshot.odds:
            freshness_rows.append(
                {
                    "type": "external_odds",
                    "id": f"{row.event_id}:{row.book}:{row.market_type}:{row.outcome}",
                    "timestamp": row.timestamp,
                    "age_seconds": (now - row.timestamp).total_seconds(),
                }
            )
        st.dataframe(pd.DataFrame(freshness_rows), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
