#!/bin/bash
sqlite3 trades.db 'select id,created_at,symbol,signal,user_decision,order_status from recommendations;'
