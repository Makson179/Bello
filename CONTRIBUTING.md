# Contributing to Bello

Thank you for helping improve Bello. Bug fixes, tests, documentation, and
focused feature contributions are welcome.

## Before you start

- Search the existing [issues](https://github.com/Makson179/Bello/issues) before
  opening a new one.
- For a substantial change to behavior, configuration, or architecture, open
  an issue first so the approach can be discussed.
- Report suspected vulnerabilities privately as described in
  [SECURITY.md](./SECURITY.md), not in a public issue.

## Development setup

Bello requires Python 3.11 or newer. Clone the repository, create and activate
a virtual environment, then install the project with its test dependencies:

```bash
git clone https://github.com/Makson179/Bello.git
cd Bello
python -m venv .venv
# Activate .venv for your shell, then:
python -m pip install -e ".[test]"
python -m pytest -q
```

Run focused tests while developing and the full suite before submitting a pull
request. CI runs the suite on Python 3.11–3.14 and checks Linux, macOS, and
Windows.

## Pull requests

Keep each pull request focused and explain both the problem and the chosen
solution. Please:

- add or update tests for behavior changes and bug fixes;
- update user-facing documentation when commands, configuration, or workflows
  change;
- follow the style of the surrounding code and avoid unrelated rewrites;
- describe any effect on sandboxing, approvals, workspace isolation, recovery,
  or other trust boundaries, and cover it with regression tests;
- remove secrets and private task data from logs, fixtures, screenshots, and
  examples.

All contributions require signing the project [CLA](./CLA.md). The CLA bot will
prompt you on your first pull request; you only need to sign once.

## Bug reports and feature requests

Use [GitHub Issues](https://github.com/Makson179/Bello/issues) for non-security
bugs, feature requests, and documentation problems. Include the Bello version,
operating system, Python version, reproduction steps, expected behavior, and
sanitized logs when they are relevant.
