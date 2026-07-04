SKILL_NAME := workspaces
SANDCASTLE_SKILL_NAME := workspaces-with-sandcastle
SKILL_DEST ?= $(HOME)/.agents/skills/$(SKILL_NAME)
SANDCASTLE_SKILL_DEST ?= $(HOME)/.agents/skills/$(SANDCASTLE_SKILL_NAME)

.PHONY: install self-check

install:
	mkdir -p "$(SKILL_DEST)"
	rsync -a --delete \
		--exclude '.git/' \
		--exclude '__pycache__/' \
		--exclude '.pytest_cache/' \
		./ "$(SKILL_DEST)/"
	rm -rf "$(SANDCASTLE_SKILL_DEST)"
	mkdir -p "$(dir $(SANDCASTLE_SKILL_DEST))"
	ln -s "$(SKILL_DEST)/$(SANDCASTLE_SKILL_NAME)" "$(SANDCASTLE_SKILL_DEST)"

self-check:
	python3 scripts/self_check.py
