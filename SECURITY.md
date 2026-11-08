# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report privately via
GitHub's "Report a vulnerability" (Security → Advisories) on this repository, or
contact the maintainers through a private channel listed on the repository
profile. Include reproduction steps and impact.

We aim to acknowledge reports within a few days. Please give us reasonable time
to fix the issue before public disclosure.

## Scope and expectations for self-hosting

This repository is a self-hosted template. Operators are responsible for:

- keeping `secrets/` out of source control and rotating keys (AI API key, admin
  tokens, database passwords, SSH keys);
- running PostgreSQL/Redis on private networks only and enabling backups;
- configuring TLS (the example Caddy snippets) and restricting SSH access;
- reviewing third-party data-source licences before commercial use.

The maintainers do not operate a public instance of this software.
