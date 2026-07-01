#!/bin/bash

echo "=== Trades ==="
sqlite3 trades.db 'select id,created_at,symbol,user_decision,order_status from recommendations;'

echo
echo "=== Latest Portfolio Snapshot ==="
sqlite3 trades.db '
with latest as (
  select max(created_at) as ts from portfolio_snapshots
)
select created_at,
       printf("%.2f", cash) as cash,
       printf("%.2f", portfolio_value) as portfolio_value,
       symbol,
       qty,
       printf("%.2f", market_value) as market_value,
       printf("%.2f", unrealized_pl) as unrealized_pl
from portfolio_snapshots
where created_at >= datetime((select ts from latest), "-2 seconds")
order by symbol;
'
