# Contributing

Thanks for your interest in improving this project! This document explains how
to set up a development environment and get your changes merged.

## Development setup

```bash
git clone https://github.com/opao-max/wiki-cross-tenant.git
cd wiki-cross-tenant
python --version   # requires Python 3.9+
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Workflow

1. Fork the repository and create a branch from `main`:
   `git checkout -b feat/short-description`
2. Make your change, keeping commits focused and using
   [Conventional Commits](https://www.conventionalcommits.org/):
   `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
3. Before pushing, run the same checks as CI:

```bash
python -m compileall -q .
flake8 --max-line-length=120 --ignore=E203,W503 .
python -m pytest -q
```

4. Push your branch and open a Pull Request, filling in the PR template.

## Code style

- Follow PEP 8; line length up to 120. Type hints are encouraged on public APIs.
- Add or update tests for behavior changes; keep fixtures small.
- Never commit secrets or large data/model files (they are git-ignored).
- Document user-facing changes in `CHANGELOG.md`.

## Reporting bugs

Please use the Bug Report issue template and include a minimal reproduction.

## License

By contributing, you agree that your contributions will be licensed under the
repository's MIT License.

