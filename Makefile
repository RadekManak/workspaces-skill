SKILL_NAME := workspaces
SANDCASTLE_SKILL_NAME := workspaces-with-sandcastle
SKILL_DEST_AGENTS ?= $(HOME)/.agents/skills/$(SKILL_NAME)
SKILL_DEST_CLAUDE ?= $(HOME)/.claude/skills/$(SKILL_NAME)
SANDCASTLE_SKILL_DEST_AGENTS ?= $(HOME)/.agents/skills/$(SANDCASTLE_SKILL_NAME)
SANDCASTLE_SKILL_DEST_CLAUDE ?= $(HOME)/.claude/skills/$(SANDCASTLE_SKILL_NAME)

RSYNC_FLAGS := -a --delete \
	--exclude '.git/' \
	--exclude '__pycache__/' \
	--exclude '.pytest_cache/'

.PHONY: install self-check

install:
	mkdir -p "$(SKILL_DEST_AGENTS)" "$(dir $(SKILL_DEST_CLAUDE))"
	rsync $(RSYNC_FLAGS) ./ "$(SKILL_DEST_AGENTS)/"
	rm -rf "$(SKILL_DEST_CLAUDE)"
	ln -s "$(SKILL_DEST_AGENTS)" "$(SKILL_DEST_CLAUDE)"
	rm -rf "$(SANDCASTLE_SKILL_DEST_AGENTS)" "$(SANDCASTLE_SKILL_DEST_CLAUDE)"
	mkdir -p "$(dir $(SANDCASTLE_SKILL_DEST_AGENTS))" "$(dir $(SANDCASTLE_SKILL_DEST_CLAUDE))"
	ln -s "$(SKILL_DEST_AGENTS)/$(SANDCASTLE_SKILL_NAME)" "$(SANDCASTLE_SKILL_DEST_AGENTS)"
	ln -s "$(SKILL_DEST_AGENTS)/$(SANDCASTLE_SKILL_NAME)" "$(SANDCASTLE_SKILL_DEST_CLAUDE)"

self-check:
	python3 scripts/self_check.py
