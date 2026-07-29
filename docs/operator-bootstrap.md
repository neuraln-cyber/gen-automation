# Initial owner bootstrap

Do not start the first application replica until migrations and owner bootstrap
have completed. Run the same immutable control-plane image as a one-off
interactive job, with only the PostgreSQL URL and TOTP encryption keyring
mounted through a dedicated secret identity. Override its command with:

```console
python3.12 -m gen_automation.cli bootstrap-owner
```

The command accepts the password and TOTP confirmation only through an
interactive terminal. It does not accept credential command-line flags or
environment variables. The TOTP provisioning URI and manual secret are shown
once; save the authenticator account and do not put either value in logs,
tickets, chat, or source control. The operator must also provide an attributable
individual identity and approved change-ticket reference for the audit record.

An editable or packaged installation also exposes the equivalent
`gen-automation-auth bootstrap-owner` console command. The module form above is
guaranteed to exist in the production image.

The bootstrap transaction refuses to run after any administrative user exists.
Production uses a PostgreSQL transaction-level advisory lock so two concurrent
bootstrap attempts cannot create separate owners. After the command succeeds,
wait for the six-digit code to rotate before logging in because the enrollment
code is recorded as already used.

Before starting application replicas, configure the TLS ingress and restrict
direct container access to it. Set the trusted-proxy CIDRs to only the ingress
source ranges. Configure the ingress to overwrite untrusted forwarding headers
or append its socket-observed client address, rate-limit authentication, and
bound request bodies, headers, connection concurrency, and request duration,
including chunked bodies without `Content-Length`. Set the ingress rate-limit
and request-guard assertions only after those controls have been verified.

Normal application startup fails closed when authentication is enabled without
an active owner whose password hash and encrypted TOTP seed are structurally
usable. The rollout sequence is therefore:

1. run the migration job to completion;
2. run the one-off interactive bootstrap job to completion;
3. start application replicas;
4. verify authenticated readiness before enabling ingress traffic.

The migration, bootstrap, and application rollout jobs must not race.

## Normal administrator enrollment

After the first owner is running, create every additional administrator through
the authenticated JSON API. An owner with a recent password-and-TOTP
authentication must send a same-origin, CSRF-protected `POST` to
`/api/v1/auth/admin-enrollments/invitations`. The response contains a 256-bit
invitation capability exactly once. It does not contain the TOTP setup secret.
Transfer the capability out of band to the intended administrator; never put it
in a URL, browser history, ticket, log, analytics event, or chat transcript.

The invitee sends the capability only in the JSON body of a same-origin `POST`
to `/api/v1/auth/admin-enrollments/inspect`. A valid, unexpired capability
returns the TOTP setup secret and provisioning URI. After adding it to an
authenticator, the invitee sends the capability, new password, and current
six-digit code in the JSON body of
`/api/v1/auth/admin-enrollments/complete`.

Completion is one-time and atomic: it creates the active administrator, binds
the encrypted TOTP seed to the new immutable user ID, records the used TOTP
counter, consumes the invitation, and erases the invitation-bound seed.
The invitee must wait for the six-digit code to rotate before the first login,
because the completion code is already recorded as used.
Malformed, unknown, expired, consumed, and revoked capabilities return the same
generic error. Invitations expire according to
`GEN_AUTOMATION_AUTH_ENROLLMENT_INVITE_TTL_SECONDS`, bounded from ten minutes to
seven days. Reauthenticate before creating the second-owner invitation and
verify that the second owner can log in before treating break-glass recovery as
tested.

## Offline break-glass recovery

If administrative rows already exist but every owner is inactive or has
unusable credentials, initial bootstrap deliberately remains disabled. Stop
application rollout and run a separate one-off interactive job:

```console
python3.12 -m gen_automation.cli recover-owner
```

Recovery requires typing an explicit `RECOVER OWNER <normalized-username>`
confirmation, an attributable individual operator identity, and an approved
incident/change ticket. It refuses to run while any active owner has a usable
password hash and decryptable TOTP seed. When allowed, it resets or creates only
the named owner, enrolls a new TOTP seed, increments the credential version,
revokes that identity's active sessions, and writes before/after state to a
break-glass audit event. Use it only through restricted operator access, then
rotate away the temporary job identity and investigate why normal owner
recovery was unavailable.

If credentials remain structurally valid but the sole owner has lost the
password or authenticator, default recovery cannot prove that fact and refuses
to override the owner. The stronger command:

```console
python3.12 -m gen_automation.cli recover-owner --force
```

requires the `FORCE RECOVER OWNER <normalized-username>` phrase, two distinct
operator identities, and separate approval/change-ticket references. Its audit
event is marked as forced. This is a two-person emergency procedure, not a
substitute for creating a second owner through the authenticated management
workflow.
