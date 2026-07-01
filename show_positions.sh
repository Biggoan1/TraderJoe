#!/bin/bash
sqlite3 trades.db 'select created_at,symbol,qty,market_value,unrealized_pl from portfolio_snapshots order by id desc limit 20;'
