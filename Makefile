.PHONY: smoke

smoke:
	CCH_SMOKE_ENABLED=1 python3 -m pytest -vv -s

