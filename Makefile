# JSON output consistency harness.
# Put ANTHROPIC_API_KEY=sk-ant-... in a .env file (see .env.example).

-include .env
export

VENV        := .venv
PYTHON      := $(VENV)/bin/python
CONFIG      := config.json
ITERATIONS  ?=
MODEL       ?=
CONCURRENCY ?=

FLAGS       := $(if $(ITERATIONS),--iterations $(ITERATIONS),) \
               $(if $(MODEL),--model $(MODEL),) \
               $(if $(CONCURRENCY),--concurrency $(CONCURRENCY),)

.PHONY: help install dry-run smoke run analyze check-key clean

help:
	@echo "make install   - create venv and install dependencies"
	@echo "make dry-run   - print the test matrix and request count (no API calls)"
	@echo "make smoke     - tiny live run (2 iterations per cell)"
	@echo "make run       - full run (config default iterations)"
	@echo ""
	@echo "Options (any target): MODEL=claude-haiku-4-5  ITERATIONS=50  CONCURRENCY=8"
	@echo "  e.g. make run MODEL=claude-sonnet-4-6 ITERATIONS=25 CONCURRENCY=8"
	@echo "make analyze   - open the results notebook in Jupyter Lab"
	@echo "make clean     - remove the venv"

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt

install: $(VENV)

dry-run: $(VENV)
	$(PYTHON) runner.py --config $(CONFIG) $(FLAGS) --dry-run

check-key:
	@test -n "$(ANTHROPIC_API_KEY)" || \
		{ echo "ANTHROPIC_API_KEY is not set — copy .env.example to .env and add your key"; exit 1; }

smoke: $(VENV) check-key
	$(PYTHON) runner.py --config $(CONFIG) $(if $(MODEL),--model $(MODEL),) \
		$(if $(CONCURRENCY),--concurrency $(CONCURRENCY),) --iterations 2

run: $(VENV) check-key
	$(PYTHON) runner.py --config $(CONFIG) $(FLAGS)

analyze: $(VENV)
	$(VENV)/bin/jupyter lab analyze.ipynb

clean:
	rm -rf $(VENV)
