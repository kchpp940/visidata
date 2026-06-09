VD_DEV ?= $(shell command -v vd-dev 2>/dev/null || echo "python3 -m visidata.dev_cli")

.PHONY: help \
       install install-dev install-test install-all \
       test test-all test-vgit test-vdsql \
       build man zsh-completion docker \
       setup-hooks setup-vscode lint \
       diff-test clean check

help:
	@echo "Install:"
	@echo "  make install           pip install visidata"
	@echo "  make install-dev       editable install with dev deps"
	@echo "  make install-test      install with test deps"
	@echo "  make install-all       install with all optional deps"
	@echo ""
	@echo "Test:"
	@echo "  make test              run all tests (same as test-all)"
	@echo "  make test-all          run all tests"
	@echo "  make test-golden       run golden/cmdlog tests"
	@echo "  make test-unit         run Python unit tests (pytest)"
	@echo ""
	@echo "Build:"
	@echo "  make man               generate man pages (requires soelim, preconv, aha)"
	@echo "  make zsh-completion    generate zsh completion script"
	@echo "  make docker            build docker images"
	@echo ""
	@echo "Setup:"
	@echo "  make setup-hooks       configure git to use dev/hooks"
	@echo "  make setup-vscode      copy devcontainer configs to .vscode/"
	@echo ""
	@echo "Utility:"
	@echo "  make lint              run ruff linter"
	@echo "  make check             comprehensive check (lint + test)"
	@echo "  make diff-test         show diffs from last test run"
	@echo "  make clean             remove generated files"

install:
	$(VD_DEV) install prod

install-dev:
	$(VD_DEV) install dev

install-test:
	$(VD_DEV) install test

install-all:
	$(VD_DEV) install all

test: test-all

test-all:
	$(VD_DEV) test all

test-golden:
	$(VD_DEV) test golden

test-unit:
	$(VD_DEV) test unit

test-vgit:
	$(VD_DEV) test vgit

test-vdsql:
	$(VD_DEV) test vdsql

build: man zsh-completion

man:
	$(VD_DEV) build man

zsh-completion:
	$(VD_DEV) build zsh

docker:
	$(VD_DEV) build docker

setup-hooks:
	$(VD_DEV) setup hooks

setup-vscode:
	$(VD_DEV) setup vscode

lint:
	$(VD_DEV) lint

diff-test:
	$(VD_DEV) diff-test

clean:
	$(VD_DEV) clean

check:
	$(VD_DEV) check
