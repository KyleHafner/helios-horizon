# Security Policy

## Supported version

Security fixes are applied to the current `main` branch. This project has not reached a stable compatibility release.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Security → Report a vulnerability** flow. Do not open a public issue containing exploit details, credentials, private topology, player data, or backup contents.

Include the affected commit, impact, reproduction steps using synthetic data, and any proposed mitigation. You should receive an acknowledgement within seven days.

## Deployment boundary

Helios Horizon is designed for a private, authenticated operator surface. The web process must remain behind a trusted reverse proxy/SSO layer and must receive a fixed proxy credential through a protected runtime credential file. The checked-in addresses, IDs, and paths are examples.

A deployment is unsafe if it:

- exposes the FastAPI listener directly to the internet;
- trusts identity headers without validating the proxy credential;
- disables origin or CSRF validation;
- stores tokens in TOML, JSON, environment files, command lines, or Git;
- expands fixed unit/path/profile allowlists from request data;
- runs the web tier with controller privileges; or
- restores unverified backups without an approved rollback path.
