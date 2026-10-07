# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately.

**Do not open a public issue.**

Use GitHub's private vulnerability reporting:
<https://github.com/ontolith/ontolith/security/advisories/new>

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Response Timeline

- Initial response: Within 48 hours
- Status update: Within 7 days
- Fix timeline: Depends on severity

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x     | :white_check_mark: |
| 0.x     | :x:                |

## Security Best Practices

When using Ontolith:
- Keep dependencies updated
- Rotate and revoke API-key tokens proactively — the only shipped `AuthProvider` today is per-principal API keys (ADR-0014); OIDC/workload-identity support is documented as future work, not yet implemented
- Review AI-authored assertions before accepting
- Configure appropriate policy thresholds
- Structured logs are on by default (`kb.observability`, ADR-0044); correlate them with the `namespace`/`principal`/`proposal_id` fields they already carry for audit purposes in production
