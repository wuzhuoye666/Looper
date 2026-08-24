# Local SSH credentials

Place a developer-owned private key in this directory and set
`LOOPER_DEFAULT_SSH_PRIVATE_KEY_PATH` in the local `.env` file. The default
path is `deploy/ssh/Looper.pem`.

Private keys, public keys, and other credential files in this directory are
ignored by Git and must never be committed. Use a separate key per environment
and rotate it if it has ever been exposed in a repository or log.
