# Engineering Onboarding Guide

Welcome to Tessera engineering. This guide covers your first weeks.

## First day

On your first day you receive a laptop with the standard development image, and IT creates
your accounts for email, chat, the code host and the admin console. Hardware security keys
are issued in person and are required for production access. Your manager assigns you an
onboarding buddy from another team.

## Development environment

Clone the monorepo and run `make bootstrap`, which installs the pinned toolchain and starts
the local services in containers. The bootstrap takes about 20 minutes on a fresh laptop. If
it fails, ask in the #dev-env channel rather than modifying the toolchain versions yourself.

## Production access

New engineers get read-only production access after completing the security training, which
usually happens in the first week. Write access requires a further two weeks and approval
from your manager.

## Joining the on-call rotation

New engineers shadow the on-call rotation for their team after three weeks. Shadowing lasts
two full rotation cycles, during which you receive pages alongside the primary engineer but
are not responsible for responding. After shadowing, you join the rotation as a secondary
on-call engineer before becoming primary.
