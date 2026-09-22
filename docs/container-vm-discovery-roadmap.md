# Container and VM discovery roadmap

EdgeDisco does not currently inspect container runtimes or virtual-machine guests. This roadmap describes how that coverage can be added without collecting prompts, source code, credentials, raw command lines, environment values, container logs, or guest files.

Container and VM evidence have different trust boundaries. A local container runtime can usually provide a narrowly selected process view to the current user. A VM deliberately hides guest processes from the host, so reliable inspection inside a guest requires an explicit guest command or a collector running in that guest.

---

## Privileged enterprise mode vs. unprivileged user mode

EdgeDisco operates under a unified discovery engine that behaves according to its execution privileges:

| Capability | Unprivileged User Mode (`~/.edgedisco`) | Enterprise Root Mode (LaunchDaemon / systemd) |
| --- | --- | --- |
| **Execution Context** | Logged-in user (`gui/<uid>`) | Root / Local System (`system`) |
| **Socket Access** | User-owned or world-readable sockets only | Direct access to `/var/run/docker.sock`, `/var/run/podman/podman.sock` |
| **Per-User VM Sockets** | Can only access current user's Colima/OrbStack/Docker sockets | Attaches across all user directories without permission friction |
| **Fleet Scope** | Single interactive user session | Fleet-wide: all background services and multi-user sessions |
| **Tamper Resistance** | Low (user can stop/unload process) | High (managed by MDM, requires root to terminate or modify) |
| **Process Inspection** | Limited to container runtimes where user has group access | Full container process top (`/containers/{id}/top`) across all runtimes |
| **VM Hypervisor Lineage** | Inferred from current user's process table | Host-level Darwin `libproc` / `sysctl` correlation of all hypervisor VMs |

### Enterprise root discovery mechanics
When running as a root LaunchDaemon on macOS (or systemd service on Linux):

1. **System & VM socket discovery:** The collector detects both standard system sockets (`/var/run/docker.sock`) and user-scoped hypervisor sockets (OrbStack, Colima, Lima, Podman Machine) across `/Users/*` or `/home/*`.
2. **Container process top inspection:** Rather than requiring agents inside every container, the daemon queries the Docker Engine API's `/containers/{id}/top` endpoint. This returns running process basenames inside container namespaces directly from the host.
3. **Verified image digest matching:** Queries `/containers/json` for image SHA-256 digests and correlates them against EdgeDisco's fingerprint catalog.
4. **Host hypervisor correlation:** Correlates running Virtualization.framework / QEMU processes with their respective socket bridges to map containers to specific VM instances.

### Strict enterprise privacy guardrails
Root privileges grant powerful host and container access. EdgeDisco enforces absolute privacy boundaries:

- **Strictly NO environment variable inspection:** Developers frequently pass API keys, tokens, and database passwords in container environment variables. EdgeDisco **never** reads container `Config.Env`.
- **Strictly NO mount or volume traversal:** Containers mounting host repositories or sensitive paths (`~/.ssh`, `~/.aws`) are never traversed.
- **Strictly NO arbitrary `exec` shells:** The collector never executes arbitrary shell commands inside employee containers.
- **Strictly sanitized telemetry:** Only normalized executable basenames (e.g. `node`, `python3`, `hermes`) and cataloged image SHA-256 digests are recorded.

---

## Phase 1: baseline local-container detection

The first implementation should discover agents in running containers without a separate opt-in when the current user already has access to the local container runtime.

Initial runtime support:

- Docker Engine and Docker Desktop.
- Rootless and conventional Podman, including Podman Machine.
- Docker-compatible local contexts provided by tools such as Colima or Rancher Desktop where the same bounded interface is available.

The collector should:

1. Detect an installed or running supported container runtime.
2. Confirm that the selected endpoint is local, such as a Unix socket or Windows named pipe. SSH and TCP contexts, Kubernetes contexts, and other remote endpoints are out of scope by default.
3. Use only the permissions the current user already has. It must not invoke `sudo`, request Docker-group membership, change socket permissions, or start a stopped runtime.
4. List running container IDs through the runtime's read-only interface. Raw container IDs and container names must not be uploaded.
5. Obtain the image's content-addressed identity through a narrowly formatted field, without collecting the image configuration, environment, command, labels, mounts, ports, or registry credentials.
6. Compare the image identity locally with a versioned catalog of verified, published agent-image digests. An image digest is strong evidence only when it exactly matches a catalog entry; tags and repository names are not integrity evidence.
7. Request only executable basename, process ID, and parent process ID from the runtime process view. Process IDs are used locally for lineage and are not uploaded. Raw arguments are neither requested nor retained.
8. Apply the existing allowlisted agent signatures to executable basenames and parent relationships.
9. Hash the runtime and container identity locally to correlate observations without disclosing the raw identifier.

This produces two independent evidence paths:

| Evidence | Meaning |
| --- | --- |
| Verified image digest | The running container uses a published image represented in EdgeDisco's fingerprint catalog |
| Supported executable observed in a container | A known agent executable was running inside the container, including inside an otherwise unknown or generic image |

A hashed container identifier is a privacy-preserving correlation key; it does not identify the software by itself. A verified image digest identifies cataloged image content but does not prove that an agent was actively handling a request. Process evidence establishes that an executable was visible during the scan, not that it was generating a response.

Runtime access failures must be represented internally as unavailable evidence and must not be treated as an empty container inventory. Each command or API request needs a short timeout, an output-size limit, strict parsing, and failure isolation so a malformed or unresponsive runtime cannot block endpoint collection.

Docker exposes bounded container-list and process-list operations through its Engine API. Podman exposes comparable `ps`, `inspect`, and `top` operations. The implementation should prefer the API or invoke an exact executable directly with fixed arguments; it must never build a shell command from runtime data. See the [Docker Engine API](https://docs.docker.com/reference/api/engine/) and [Podman `top`](https://docs.podman.io/en/stable/markdown/podman-top.1.html).

Access to a conventional Docker daemon can be highly privileged. Baseline discovery must never broaden access merely to obtain inventory. See [Docker's daemon security guidance](https://docs.docker.com/engine/install/linux-postinstall/).

## Phase 2: container lifecycle wake-ups

After snapshot reconciliation is stable, Docker and Podman lifecycle events can wake the scanner when a container starts, stops, pauses, or resumes. Events are hints only. The periodic bounded snapshot remains authoritative for collector restarts, missed events, runtime upgrades, and race conditions.

This phase improves detection of short-lived agent containers without reading event payload fields unrelated to lifecycle state.

## Phase 3: local VM presence inventory

The host can often identify running local VMs even though it cannot see their guest processes. EdgeDisco can add presence-only adapters for local managers such as:

- Lima and compatible local VM managers.
- Multipass.
- Podman Machine.
- WSL distributions on Windows.
- libvirt, Parallels, VMware, and VirtualBox where a stable, unprivileged inventory interface is available.

Default VM inventory should report only the manager, a locally hashed VM identity, state, and a coarse guest type when safely available. It must not start, resume, mount, or enter a guest. Docker Desktop's or Podman's internal backing VM must be correlated with its container runtime to avoid double-counting it as an independently managed developer VM.

## Phase 4: opt-in guest probes

Deep discovery inside a VM crosses an isolation boundary and therefore remains explicitly opt-in. For supported managers, EdgeDisco may run a fixed, versioned probe inside an already-running guest. Lima supports guest commands through `limactl shell`, Multipass through `multipass exec`, and WSL through a selected distribution command. See the [Lima shell reference](https://lima-vm.io/docs/reference/limactl_shell/), [Multipass documentation](https://documentation.ubuntu.com/server/how-to/virtualisation/multipass/index.html), and [WSL command reference](https://learn.microsoft.com/windows/wsl/basic-commands).

An opt-in probe must:

- Never start or resume a stopped guest.
- Use a fixed executable and argument list rather than a shell-composed command.
- Return only normalized executable basenames and parent relationships.
- Apply time and output limits and tolerate missing guest tools.
- Avoid environment values, command arguments, files, package databases, shell history, source trees, and network configuration.
- Make the guest boundary visible in reported evidence.

VM managers that require credentials, elevated privileges, interactive approval, or unavailable guest tooling should remain presence-only.

## Phase 5: native guest collectors and nested environments

The durable solution for managed VMs is a normal EdgeDisco collector installed inside each guest. Guest collection observes agents under the correct user identity and can cover processes started through SSH, remote-development tools, services, or containers nested inside the VM.

The server can correlate a guest device with its host using a pseudonymous parent-device and guest identifier. Correlation must not grant the host collector access to the guest or allow one device credential to impersonate another.

Nested discovery should retain explicit boundaries—for example, host, VM, and container—so the dashboard can distinguish separate observations of the same product and avoid presenting them as duplicate agent sessions.

## Privacy and product requirements

Before Phase 1 ships, the installer notice, architecture, report schema, dashboard labels, exports, and threat model must describe container evidence explicitly. In every phase:

- No prompt, response, source code, document, tool input or output, credential, environment value, raw command line, log, shell history, mount path, or registry URL is collected.
- Unknown processes are ignored rather than uploaded.
- Raw container IDs, container names, VM names, and guest process IDs remain local.
- Remote runtime contexts and remote clusters are excluded by default.
- Installation or image presence is distinct from a running process, and both are distinct from a verified agent session.
- Loss of runtime or guest access is not reported as confirmed process termination.
- Runtime responses and guest-probe output are untrusted, bounded input.

Each phase requires focused fixtures for malformed output, timeouts, inaccessible sockets, remote-context exclusion, duplicate evidence, privacy-field rejection, and platform behavior. Live integration tests should remain opt-in because they depend on locally installed runtimes and images.
