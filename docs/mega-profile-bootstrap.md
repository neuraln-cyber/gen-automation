# One-time MEGA profile bootstrap

The staging controller uses the official, pinned MEGAcmd client and a session
cache on the encrypted integration-profile volume. The application never needs
a MEGA password, MFA code, session ID, folder key, or API token.

Run the bootstrap only from an interactive AWS Systems Manager shell on the
staging host:

```console
sudo /usr/local/sbin/gen-automation-bootstrap-mega-profile
```

The command temporarily stops the application so two MEGAcmd servers cannot
write the same cache, then opens the official MEGAcmd shell in a locked-down
container. It mounts only
`/var/lib/gen-automation/integration-profiles/mega`, uses a private bridge for
MEGA network access, receives no application environment file or AWS
credentials, and restores the application on exit.

At the `MEGA CMD>` prompt:

1. Enter `login YOUR_MEGA_EMAIL`.
2. Enter the password only at MEGAcmd's hidden `Password:` prompt.
3. Complete the MFA prompt if the account requires it.
4. Wait for `Login complete`, then enter `quit --only-shell`.

Do not put the password, MFA code, session ID, folder key, or writable-folder
authorization key on the login command line. The host shell therefore receives
no credential in an argument, environment variable, file, or history entry.
Do not run `logout`: an ordinary shell exit retains the encrypted session cache,
while `logout` removes it.

After the shell closes, the wrapper enables HTTPS transfers, verifies the
authenticated session without printing the account identity or remote
filenames, checks that the configured remote root (currently `/Future`) can be
decrypted, and rejects an unsafe
profile owner, mode, or symbolic link. A successful run ends with:

```text
MEGA profile is authenticated, private, readable, and configured for HTTPS transfers.
The persistent session was retained. Do not run mega-logout.
```

The HTTPS change can be deliberately skipped with `--skip-https`. This is not
recommended for staging or production. To check an existing session without
opening the login shell, run:

```console
sudo /usr/local/sbin/gen-automation-bootstrap-mega-profile --verify-only
```

The wrapper currently supports a normal MEGA account login. The official
writable-folder login syntax places the link key and write authorization key in
the command itself, so it is intentionally not accepted by this no-secret-args
workflow. Revoke the MEGA device session from another trusted MEGA client if the
encrypted profile volume is ever exposed.

MEGAcmd documents that an ordinary shell close retains the cached session and
that a full logout clears it:

- <https://github.com/meganz/MEGAcmd>
- <https://github.com/meganz/MEGAcmd/blob/master/UserGuide.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/login.md>
