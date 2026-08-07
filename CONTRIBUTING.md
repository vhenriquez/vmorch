# Contributing

## Running the tests

```bash
git clone https://github.com/vhenriquez/vmorch && cd vmorch
./run-tests
```

That is the whole setup. **No libvirt, no network, no dependencies, no fixtures
to build** — just Python 3.11 or newer. It takes a couple of seconds.

```
test_alloc.py                allocation is correct
test_apply_network.py        apply reconciles the guest network
...
all checks pass
```

A single check on its own:

```bash
python3 tests/test_nets.py
```

Each test file is a plain script with a `main()` that returns an exit code.
There is no test framework, because adding one would mean adding a dependency
(see below). `./run-tests` just runs them all and collects the results.

### Keep it that way

**The suite must keep running on a fresh clone with nothing installed.** This is
not a nicety; it regressed once and stayed broken for a while. Three checks used
to shell out to `virsh` to look up the NAT gateway and bridge name, so they
could only pass on a machine with libvirt configured — on anyone else's clone
the suite failed before telling them anything useful.

If a test needs a value that normally comes from the host, **pass it in**.
`network._wan_filter_xml()` and `audit.nft_ruleset()` both take the gateway and
bridge as optional arguments for exactly this reason: discovered at runtime,
injected in tests.

CI enforces this by asserting `virsh` is *absent* from the runner, so the job
cannot start passing for the wrong reason.

### What the tests cannot tell you

Some of this project's claims are about the behaviour of a real hypervisor, and
no unit test settles them. If you change any of:

- the nwfilter rules or when filters are defined,
- the domain XML's interfaces, `<readonly/>`, or memory backing,
- the cloud-init seed,

then say so in the pull request, and say whether you verified it on a real box.
"I read the generated XML and it looks right" is not the same claim, and the
difference has mattered here before — a box was measurably unfiltered for ~100
seconds after every start while its filter was defined, correct, and bound the
whole time.

## Conventions that will surprise you

These are enforced or load-bearing, and none of them are obvious from the code.

### No dependencies

`dependencies = []` in `pyproject.toml`, and every import is standard library.
Installing vmorch pulls in nothing and cannot break anyone's environment.

That is a promise the README makes, so **a pull request that adds a third-party
import is a pull request that changes what this project is**. It might still be
the right call — but argue for it explicitly rather than as a side effect of
something else. CI fails if the built wheel gains a `Requires-Dist`.

### The TUI is not a subset of the CLI

**Every option `vmorch` exposes must be settable from `vmorch-tui`, and every
box option must be visible in the box panel with a description saying what it
does.** `tests/test_tui_menu.py` checks this and will fail your change if you
add a CLI flag and stop there.

- `OPTION_HELP` in `vmorch/tui/app.py` is the single source of those
  descriptions, used by both the creation form and the detail panel. An option
  cannot be explained in one place and left bare in the other.
- Options that widen what a box can reach or do (`lan`, `nested`) go in
  `RISKY_WHEN_ON` so they render in warning colour. **The risky setting must
  never be the quiet one.**
- A name that differs between the two goes in `FLAG_FIELDS` with the reason.

This rule exists because `--nested` shipped CLI-only: nothing checked options,
only commands.

### Comments explain the failure, not the code

The house style is that a non-obvious line says what went wrong to make it
necessary. Not `# set the mask` but *why* a literal `24` was wrong there. This
is most of what makes the codebase navigable, and it is the first thing to erode.

If you fix something subtle, leave the finding behind. Dates and measurements
are welcome ("Measured 2026-08-05: 0% loss for over two minutes").

### Grants fail closed

A shared folder is read-only unless the spec explicitly says `rw`. A missing,
empty or malformed `mode` resolves to `ro` — never to `rw`. There are
deliberately two independent layers (`<readonly/>` in the domain XML *and* an
`ro` guest mount), so one failing is not enough to grant writes.

Anything that widens what a box can reach needs to be opt-in, visible in
`vmorch show`, and hard to set by accident.

### Never touch a domain that is not ours

Every domain this tool creates is prefixed `vmorch-`. Code that stops, destroys
or reconfigures a domain sources its names from `virsh.list_our_domains()`.
People run this on workstations with real VMs on them.

### Test isolation claims, don't assert them

"The box can't reach the LAN" is something to verify from inside a running box,
not to infer from the configuration. The verified matrix in the README exists
because each row was checked that way, and one of them (read-only shares holding
against root in the guest) is only true because of a host-side mechanism that
the guest-side configuration would not have told you about.

## Things that must not be committed

The repository history was rewritten once to remove personal paths and an email
address. CI greps for them so it does not have to happen again:

- home directory paths (`/home/<you>/...`)
- personal email addresses
- machine-specific mount points
- your LAN's real subnets

If you need a path or address in a comment or a test, use an obviously fictional
one.

## Pull requests

- One change per pull request, with the *why* in the message. The commit log
  here is deliberately readable as a design record; keep it that way.
- Run `./run-tests` first.
- Say what you verified and how, and be explicit about what you did not.
- Docs live in `docs/`. A change to what a command does, or to what a box can
  reach, is not finished until the docs match it.

## Reporting security issues

Not here — see [SECURITY.md](SECURITY.md). It also lists what is deliberately
*not* a boundary, which is worth reading before reporting.
