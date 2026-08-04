# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

Use GitHub's [private vulnerability reporting](https://github.com/HuginStudios/usd_chunks/security/advisories/new)
on this repository, or email **info@huginstudios.com**.

Include what you were running (usd_chunks version, Python version, OS, and whether under
usdview, another DCC, or the CLI) and enough detail to reproduce. We will acknowledge
receipt and let you know whether we consider it in scope, and if a fix is warranted we will
credit you in the release notes unless you would rather we didn't.

## Supported versions

usd_chunks is pre-1.0 and fixes land on the latest release only. There are no maintained
release branches yet.

## Threat model: chunks are code

This is the most important thing to understand about the security of this project.

**A chunk is arbitrary Python that the runtime imports and executes.** Every `.py` file on
`USD_CHUNK_DIRS`, and everything installed into `GLOBAL_USDCHUNKS`, runs with the full
privileges of the host application — usdview, your DCC, or the `usdchunks` CLI. A chunk can
read and write any file the user can, make network calls, and author anywhere on the stage.

This is deliberate and it is the same trust model as `pip`: installing a chunk is trusting
its author, exactly as installing a package is. It follows that:

- **Only load chunks you trust.** Treat an untrusted chunk the way you would treat an
  untrusted Python package — because that is precisely what it is.
- **A malicious chunk is not a vulnerability in usd_chunks.** Reports amounting to "a chunk
  can run arbitrary code" describe the design. Sandboxing untrusted chunks is out of scope.
- **Opening a `.usda` scene that references chunk types does not execute them.** Resolution
  is by name against what is already installed; running a network is a separate, explicit
  act.

What *is* in scope: a way to get code executed without the user installing or running
anything (for example through parsing a scene file), privilege or path escapes such as a
chunk name resolving outside the configured directories, or anything that lets one chunk
tamper with another's layers or execution.
