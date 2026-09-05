# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| latest main | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not open a public issue**.

Instead, use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-in-python/privately-reporting-a-security-vulnerability)
("Report a vulnerability" on the Security tab), or contact the maintainer
directly. Please include:

- A description of the vulnerability and its impact
- Steps to reproduce (proof-of-concept if possible)
- Suggested remediation, if you have one

You should receive an acknowledgement within 72 hours. We will investigate,
coordinate a fix, and credit the reporter in the advisory unless you prefer
to remain anonymous.

## Hardening Notes

- Never commit secrets, API keys, or credentials; `.env` files are git-ignored.
- Dependency updates are tracked by Dependabot; security advisories are
  addressed as a priority.
- Continuous security analysis runs via GitHub CodeQL on every push.
