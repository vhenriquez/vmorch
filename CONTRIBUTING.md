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

**The suite must keep running on a fresh clone with nothing installed.** If a
test needs a value that normally comes from the host, pass it in rather than
looking it up. `network._wan_filter_xml()` and `audit.nft_ruleset()` both take
the gateway and bridge address as optional arguments so tests can supply them.

CI checks this by confirming `virsh` is absent from the runner.

### What the tests cannot tell you

Some of this project's claims are about how a real hypervisor behaves, and no
unit test settles them. If you change:

- the nwfilter rules, or when filters are defined,
- the domain XML's interfaces, `<readonly/>`, or memory backing,
- the cloud-init seed,

then say in the pull request whether you verified it on a real box. Reading the
generated XML is not the same check.

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

### Comments explain why, not what

The house style is that a non-obvious line says *why* it is that way — not
`# set the mask`, but what makes a hardcoded value wrong there. If you fix
something subtle, leave that reasoning behind for the next reader.

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

"The box can't reach the LAN" is something to check from inside a running box,
not something to infer from the configuration. Guest-side settings and host-side
enforcement can disagree, and only the host-side one is binding.

## Things that must not be committed

CI rejects these, because they are painful to remove later:

- home directory paths (`/home/<you>/...`)
- email addresses
- mount points specific to one machine
- real subnets from your own network

If you need a path or an address in a comment or a test, use an obviously
fictional one.

## Pull requests

- One change per pull request, and put the *why* in the commit message.
- Run `./run-tests` first.
- Say what you verified and how, and be explicit about what you did not.
- Docs live in `docs/`. A change to what a command does, or to what a box can
  reach, is not finished until the docs match it.

## Reporting security issues

Not here — see [SECURITY.md](SECURITY.md). It also lists what is *not* a
boundary, which is worth reading before you report something.
