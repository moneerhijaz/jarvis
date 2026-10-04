# Security

JARVIS is experimental software intended for a trusted local machine. Its API
has no authentication and exposes tools that can run commands, modify files,
and control the desktop. Keep the default loopback bind address. TLS alone does
not add access control. Tool confirmations, redaction, and rollback do not make
this a sandbox or guarantee that an action can be undone.

Keep model credentials, local configuration, conversation databases, memory
vaults, logs, and screenshots private. Redaction is best effort. If a credential
is published, revoke or rotate it immediately; deleting Git history cannot
invalidate credentials or erase other people's copies.

Do not post vulnerabilities or private data in public issues. If the repository
shows a Security → Report a vulnerability button, use it for private reporting.
Otherwise, open an issue requesting a private contact channel without including
vulnerability details or sensitive information. Include affected versions,
reproduction steps using synthetic data, and impact only in the private report.

Security fixes target the latest main branch; no older release is maintained.
