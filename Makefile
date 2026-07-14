.PHONY: smoke stability

smoke:
	CCH_SMOKE_ENABLED=1 python3 -m pytest -vv -s

stability:
	python3 scripts/run_stability.py --flow "$${FLOW:-fiber-to-lnd}" --tps "$${TPS:-5}" --duration "$${DURATION:-300}" --amount-sats "$${AMOUNT_SATS:-100}"
