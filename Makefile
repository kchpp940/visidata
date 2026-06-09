VD_DEV ?= $(shell command -v vd-dev 2>/dev/null || echo "python3 -m visidata.dev_cli")

.PHONY: help \
       install install-dev install-test install-all \
       test test-all test-golden test-unit test-vgit test-vdsql \
       build man zsh-completion docker \
       setup-hooks setup-vscode lint \
       diff-test clean check

help:
	@echo "All targets delegate to \`vd-dev\` — run \`vd-dev --help\` for full docs."
	@echo ""
	@echo "Common shortcuts:"
	@echo "  make install-dev      →  vd-dev install dev"
	@echo "  make install-test     →  vd-dev install test"
	@echo "  make install-all      →  vd-dev install all"
	@echo "  make install          →  vd-dev install prod"
	@echo ""
	@echo "  make test             →  vd-dev test all"
	@echo "  make test-all         →  vd-dev test all"
	@echo "  make test-golden      →  vd-dev test golden"
	@echo "  make test-unit        →  vd-dev test unit"
	@echo "  make test-vgit        →  vd-dev test vgit"
	@echo "  make test-vdsql       →  vd-dev test vdsql"
	@echo ""
	@echo "  make build            →  vd-dev build all"
	@echo "  make man              →  vd-dev build man"
	@echo "  make zsh-completion   →  vd-dev build zsh"
	@echo "  make docker           →  vd-dev build docker"
	@echo ""
	@echo "  make setup-hooks      →  vd-dev setup hooks"
	@echo "  make setup-vscode     →  vd-dev setup vscode"
	@echo ""
	@echo "  make lint             →  vd-dev lint"
	@echo "  make check            →  vd-dev check"
	@echo "  make diff-test        →  vd-dev diff-test"
	@echo "  make clean            →  vd-dev clean"

install:          ; $(VD_DEV) install prod
install-dev:      ; $(VD_DEV) install dev
install-test:     ; $(VD_DEV) install test
install-all:      ; $(VD_DEV) install all
test:             ; $(VD_DEV) test all
test-all:         ; $(VD_DEV) test all
test-golden:      ; $(VD_DEV) test golden
test-unit:        ; $(VD_DEV) test unit
test-vgit:        ; $(VD_DEV) test vgit
test-vdsql:       ; $(VD_DEV) test vdsql
build:            ; $(VD_DEV) build all
man:              ; $(VD_DEV) build man
zsh-completion:   ; $(VD_DEV) build zsh
docker:           ; $(VD_DEV) build docker
setup-hooks:      ; $(VD_DEV) setup hooks
setup-vscode:     ; $(VD_DEV) setup vscode
lint:             ; $(VD_DEV) lint
check:            ; $(VD_DEV) check
diff-test:        ; $(VD_DEV) diff-test
clean:            ; $(VD_DEV) clean
