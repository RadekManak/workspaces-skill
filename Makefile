SKILL_NAME := workspaces
SKILL_DEST ?= $(HOME)/.agents/skills/$(SKILL_NAME)

.PHONY: install self-check

install:
	mkdir -p "$(SKILL_DEST)"
	rsync -a --delete \
		--exclude '.git/' \
		--exclude '__pycache__/' \
		--exclude '.pytest_cache/' \
		./ "$(SKILL_DEST)/"

self-check:
	python3 scripts/self_check.py
