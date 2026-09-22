#[cfg(unix)]
mod docker;

#[cfg(unix)]
pub use docker::{scan_socket, ContainerScanError};

use edgedisco_core::models::Asset;
use std::path::{Path, PathBuf};

/// Return supported local-only Docker and Podman socket locations.
pub fn socket_candidates(home: Option<&Path>, uid: Option<u32>) -> Vec<PathBuf> {
    let mut candidates = vec![
        PathBuf::from("/var/run/docker.sock"),
        PathBuf::from("/var/run/podman/podman.sock"),
    ];
    if let Some(home) = home {
        candidates.extend([
            home.join(".docker/run/docker.sock"),
            home.join(".colima/default/docker.sock"),
            home.join(".orbstack/run/docker.sock"),
        ]);
    }
    if let Some(uid) = uid {
        candidates.push(PathBuf::from(format!("/run/user/{uid}/podman/podman.sock")));
    }
    candidates
}

/// Scan every accessible supported local runtime. Failures are isolated per socket.
pub fn scan_available_containers() -> Vec<Asset> {
    #[cfg(unix)]
    {
        let home = std::env::var_os("HOME").map(PathBuf::from);
        let uid = unsafe { libc::geteuid() };
        let mut assets = Vec::new();
        for socket in socket_candidates(home.as_deref(), Some(uid)) {
            if !socket.exists() {
                continue;
            }
            if let Ok(mut found) = scan_socket(&socket) {
                assets.append(&mut found);
            }
        }
        assets
    }
    #[cfg(not(unix))]
    {
        Vec::new()
    }
}
