# Contributing to prbot

Thanks for taking a look! Issues, ideas, and pull requests are all welcome.

## Getting set up

```bash
git clone https://github.com/ayanag5076-commits/prbot.git
cd prbot
python -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -e ".[dev]"
cp .env.example .env                                 # only needed for live runs
```

The offline test suite needs no API keys, Docker, or network:

```bash
pytest
```

To exercise the sandboxed tester locally, build the image first:

```bash
docker build -t prbot-tester:py3.12 -f docker/tester.Dockerfile .
```

## Making a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Branch from `main` and keep each PR focused on one change.
3. Add or update tests in `tests/` for any behavior you change. `pytest` must pass, and CI must be green.
4. Keep the sandbox guarantees intact: the tester runs patches with `--network none`, a read-only filesystem,
   dropped capabilities, resource limits, and a hard timeout. PRs that loosen these need a clear justification.
5. Update the README if you change the CLI, configuration, or graph behavior.

## Secrets

Never commit API keys, tokens, GitHub App private keys, or `.env` files. CI runs a secret guard and will fail
the build if it finds one. If you accidentally push a secret, revoke it immediately, then open an issue.

## Reporting bugs

Please include your Python version, OS, the command you ran (with `--dry-run` if possible), and the full error
output with any keys redacted.

## Security issues

Please don't open a public issue for a vulnerability. Use GitHub's private
[security advisory](https://github.com/ayanag5076-commits/prbot/security/advisories/new) form instead.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
