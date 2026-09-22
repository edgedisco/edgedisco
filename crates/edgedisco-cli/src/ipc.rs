use edgedisco_core::store::Store;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::ffi::CString;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::Duration;
use thiserror::Error;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{mpsc, oneshot, watch, Semaphore};
use tokio::task::JoinSet;
use tokio::time::timeout;

pub const PROTOCOL_VERSION: u16 = 1;
pub const DEFAULT_MAX_REQUEST_BYTES: usize = 16 * 1024;
pub const DEFAULT_MAX_RESPONSE_BYTES: usize = 256 * 1024;
pub const DEFAULT_MAX_CONNECTIONS: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerIdentity {
    pub uid: u32,
    pub gid: u32,
    pub pid: Option<i32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PeerPolicy {
    User { daemon_uid: u32 },
    System { allowed_uids: BTreeSet<u32> },
}

impl PeerPolicy {
    pub fn user(daemon_uid: u32) -> Self {
        Self::User { daemon_uid }
    }

    pub fn system(allowed_uids: BTreeSet<u32>) -> Self {
        Self::System { allowed_uids }
    }

    pub fn authorize(&self, peer: PeerIdentity) -> bool {
        if peer.uid == 0 {
            return true;
        }
        match self {
            Self::User { daemon_uid } => peer.uid == *daemon_uid,
            Self::System { allowed_uids } => allowed_uids.contains(&peer.uid),
        }
    }
}

#[derive(Debug, Clone)]
pub struct IpcLimits {
    pub max_request_bytes: usize,
    pub max_response_bytes: usize,
    pub max_connections: usize,
    pub read_timeout: Duration,
    pub write_timeout: Duration,
    pub scan_timeout: Duration,
    pub drain_timeout: Duration,
}

impl Default for IpcLimits {
    fn default() -> Self {
        Self {
            max_request_bytes: DEFAULT_MAX_REQUEST_BYTES,
            max_response_bytes: DEFAULT_MAX_RESPONSE_BYTES,
            max_connections: DEFAULT_MAX_CONNECTIONS,
            read_timeout: Duration::from_secs(2),
            write_timeout: Duration::from_secs(2),
            scan_timeout: Duration::from_secs(120),
            drain_timeout: Duration::from_secs(5),
        }
    }
}

#[derive(Debug, Clone)]
pub struct IpcConfig {
    pub socket_path: PathBuf,
    pub policy: PeerPolicy,
    pub socket_mode: u32,
    pub socket_owner: Option<(u32, u32)>,
    pub private_parent: bool,
    pub limits: IpcLimits,
}

impl IpcConfig {
    pub fn user(socket_path: PathBuf, uid: u32) -> Self {
        Self {
            socket_path,
            policy: PeerPolicy::user(uid),
            socket_mode: 0o600,
            socket_owner: None,
            private_parent: true,
            limits: IpcLimits::default(),
        }
    }

    pub fn system(
        socket_path: PathBuf,
        allowed_uids: BTreeSet<u32>,
        socket_owner: Option<(u32, u32)>,
    ) -> Result<Self, IpcError> {
        if allowed_uids.is_empty() {
            return Err(IpcError::InvalidConfig(
                "system IPC mode requires at least one --ipc-allowed-uid".into(),
            ));
        }
        Ok(Self {
            socket_path,
            policy: PeerPolicy::system(allowed_uids),
            socket_mode: 0o660,
            socket_owner,
            private_parent: false,
            limits: IpcLimits::default(),
        })
    }

    fn validate(&self) -> Result<(), IpcError> {
        if self.limits.max_request_bytes == 0
            || self.limits.max_response_bytes == 0
            || self.limits.max_connections == 0
        {
            return Err(IpcError::InvalidConfig(
                "IPC byte and connection limits must be greater than zero".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Error)]
pub enum IpcError {
    #[error("invalid IPC configuration: {0}")]
    InvalidConfig(String),
    #[error("refusing unsafe socket path {path}: {reason}")]
    UnsafePath { path: PathBuf, reason: String },
    #[error("IPC I/O error: {0}")]
    Io(#[from] io::Error),
}

#[derive(Debug, Clone, Serialize)]
struct StatusSnapshot {
    started_at: String,
    last_scan_at: Option<String>,
    last_scan_asset_count: Option<usize>,
}

#[derive(Debug)]
pub struct DaemonIpcState {
    status: RwLock<StatusSnapshot>,
}

impl DaemonIpcState {
    pub fn new(started_at: impl Into<String>) -> Self {
        Self {
            status: RwLock::new(StatusSnapshot {
                started_at: started_at.into(),
                last_scan_at: None,
                last_scan_asset_count: None,
            }),
        }
    }

    pub fn record_scan(&self, observed_at: impl Into<String>, asset_count: usize) {
        let mut status = self.status.write().unwrap();
        status.last_scan_at = Some(observed_at.into());
        status.last_scan_asset_count = Some(asset_count);
    }

    fn snapshot(&self) -> StatusSnapshot {
        self.status.read().unwrap().clone()
    }
}

#[derive(Debug)]
pub struct ScanCommand {
    pub reply: oneshot::Sender<Result<usize, String>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IpcRequest {
    pub protocol_version: u16,
    pub request_id: String,
    pub method: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IpcResponse {
    pub protocol_version: u16,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ProtocolError>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProtocolError {
    pub code: String,
    pub message: String,
}

impl IpcResponse {
    fn ok(request_id: String, result: Value) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id: Some(request_id),
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    fn error(request_id: Option<String>, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id,
            ok: false,
            result: None,
            error: Some(ProtocolError {
                code: code.to_string(),
                message: message.into(),
            }),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SanitizedDetection {
    pub kind: String,
    pub name: String,
    pub vendor: String,
    pub version: Option<String>,
    pub running: bool,
    pub present: Option<bool>,
    pub last_seen: Option<String>,
}

struct SocketGuard {
    path: PathBuf,
    device: u64,
    inode: u64,
    armed: bool,
}

impl SocketGuard {
    fn cleanup(&mut self) {
        if !self.armed {
            return;
        }
        if let Ok(metadata) = std::fs::symlink_metadata(&self.path) {
            if metadata.file_type().is_socket()
                && metadata.dev() == self.device
                && metadata.ino() == self.inode
            {
                let _ = std::fs::remove_file(&self.path);
            }
        }
        self.armed = false;
    }
}

impl Drop for SocketGuard {
    fn drop(&mut self) {
        self.cleanup();
    }
}

pub struct IpcServer {
    listener: UnixListener,
    config: IpcConfig,
    store: Arc<Store>,
    state: Arc<DaemonIpcState>,
    scan_tx: mpsc::Sender<ScanCommand>,
    socket_guard: SocketGuard,
}

impl IpcServer {
    pub async fn bind(
        config: IpcConfig,
        store: Arc<Store>,
        state: Arc<DaemonIpcState>,
        scan_tx: mpsc::Sender<ScanCommand>,
    ) -> Result<Self, IpcError> {
        config.validate()?;
        prepare_socket_path(&config)?;
        let listener = UnixListener::bind(&config.socket_path)?;

        if let Err(error) = set_socket_attributes(&config) {
            let _ = std::fs::remove_file(&config.socket_path);
            return Err(error);
        }
        let metadata = std::fs::symlink_metadata(&config.socket_path)?;
        let socket_guard = SocketGuard {
            path: config.socket_path.clone(),
            device: metadata.dev(),
            inode: metadata.ino(),
            armed: true,
        };

        Ok(Self {
            listener,
            config,
            store,
            state,
            scan_tx,
            socket_guard,
        })
    }

    pub async fn serve(mut self, mut shutdown: watch::Receiver<bool>) -> Result<(), IpcError> {
        let semaphore = Arc::new(Semaphore::new(self.config.limits.max_connections));
        let mut clients = JoinSet::new();

        loop {
            tokio::select! {
                changed = shutdown.changed() => {
                    if changed.is_err() || *shutdown.borrow() {
                        break;
                    }
                }
                accepted = self.listener.accept() => {
                    let (stream, _) = accepted?;
                    let Ok(permit) = Arc::clone(&semaphore).try_acquire_owned() else {
                        drop(stream);
                        continue;
                    };
                    let policy = self.config.policy.clone();
                    let limits = self.config.limits.clone();
                    let store = Arc::clone(&self.store);
                    let state = Arc::clone(&self.state);
                    let scan_tx = self.scan_tx.clone();
                    clients.spawn(async move {
                        let _permit = permit;
                        let _ = handle_client(stream, policy, limits, store, state, scan_tx).await;
                    });
                }
                Some(joined) = clients.join_next(), if !clients.is_empty() => {
                    let _ = joined;
                }
            }
        }

        drop(self.listener);
        let drain = async { while clients.join_next().await.is_some() {} };
        if timeout(self.config.limits.drain_timeout, drain)
            .await
            .is_err()
        {
            clients.abort_all();
            while clients.join_next().await.is_some() {}
        }
        self.socket_guard.cleanup();
        Ok(())
    }
}

async fn handle_client(
    stream: UnixStream,
    policy: PeerPolicy,
    limits: IpcLimits,
    store: Arc<Store>,
    state: Arc<DaemonIpcState>,
    scan_tx: mpsc::Sender<ScanCommand>,
) -> Result<(), io::Error> {
    let peer = peer_identity(&stream)?;
    if !policy.authorize(peer) {
        return Ok(());
    }

    let (reader, mut writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut frame = Vec::with_capacity(limits.max_request_bytes.min(4096));
    let read = timeout(limits.read_timeout, async {
        (&mut reader)
            .take((limits.max_request_bytes + 1) as u64)
            .read_until(b'\n', &mut frame)
            .await
    })
    .await;

    let response = match read {
        Err(_) => IpcResponse::error(None, "read_timeout", "request read timed out"),
        Ok(Err(error)) => return Err(error),
        Ok(Ok(0)) => return Ok(()),
        Ok(Ok(_)) if frame.len() > limits.max_request_bytes => IpcResponse::error(
            None,
            "frame_too_large",
            "request exceeds maximum frame size",
        ),
        Ok(Ok(_)) if frame.last() != Some(&b'\n') => {
            IpcResponse::error(None, "partial_frame", "request must end with a newline")
        }
        Ok(Ok(_)) => match serde_json::from_slice::<IpcRequest>(&frame) {
            Ok(request) => process_request(request, &store, &state, &scan_tx, &limits).await,
            Err(_) => {
                IpcResponse::error(None, "malformed_json", "request is not valid protocol JSON")
            }
        },
    };

    let mut bytes = match serde_json::to_vec(&response) {
        Ok(bytes) if bytes.len() < limits.max_response_bytes => bytes,
        Ok(_) => {
            let fallback = serde_json::to_vec(&IpcResponse::error(
                response.request_id,
                "response_too_large",
                "response exceeds maximum frame size",
            ))
            .unwrap_or_default();
            if fallback.len() >= limits.max_response_bytes {
                return Ok(());
            }
            fallback
        }
        Err(_) => return Ok(()),
    };
    bytes.push(b'\n');
    let _ = timeout(limits.write_timeout, writer.write_all(&bytes)).await;
    let _ = timeout(limits.write_timeout, writer.shutdown()).await;
    Ok(())
}

async fn process_request(
    request: IpcRequest,
    store: &Store,
    state: &DaemonIpcState,
    scan_tx: &mpsc::Sender<ScanCommand>,
    limits: &IpcLimits,
) -> IpcResponse {
    if request.request_id.is_empty() || request.request_id.len() > 128 {
        return IpcResponse::error(
            None,
            "invalid_request_id",
            "request_id must contain 1 to 128 bytes",
        );
    }
    if request.protocol_version != PROTOCOL_VERSION {
        return IpcResponse::error(
            Some(request.request_id),
            "unsupported_version",
            format!("supported protocol version is {PROTOCOL_VERSION}"),
        );
    }

    match request.method.as_str() {
        "negotiate" => IpcResponse::ok(
            request.request_id,
            json!({"protocol_version": PROTOCOL_VERSION, "supported_versions": [PROTOCOL_VERSION]}),
        ),
        "status" => match status_result(store, state) {
            Ok(result) => IpcResponse::ok(request.request_id, result),
            Err(message) => IpcResponse::error(Some(request.request_id), "store_error", message),
        },
        "detections" => match detections_result(store) {
            Ok(result) => IpcResponse::ok(request.request_id, result),
            Err(message) => IpcResponse::error(Some(request.request_id), "store_error", message),
        },
        "scan" => {
            let (reply, result) = oneshot::channel();
            match timeout(limits.write_timeout, scan_tx.send(ScanCommand { reply })).await {
                Ok(Ok(())) => {}
                _ => {
                    return IpcResponse::error(
                        Some(request.request_id),
                        "scan_unavailable",
                        "daemon scan queue is unavailable",
                    );
                }
            }
            match timeout(limits.scan_timeout, result).await {
                Ok(Ok(Ok(asset_count))) => IpcResponse::ok(
                    request.request_id,
                    json!({"accepted": true, "asset_count": asset_count}),
                ),
                Ok(Ok(Err(message))) => {
                    IpcResponse::error(Some(request.request_id), "scan_failed", message)
                }
                _ => IpcResponse::error(
                    Some(request.request_id),
                    "scan_timeout",
                    "scan did not complete within the configured timeout",
                ),
            }
        }
        _ => IpcResponse::error(
            Some(request.request_id),
            "unknown_method",
            "method is not exposed by the local IPC service",
        ),
    }
}

fn status_result(store: &Store, state: &DaemonIpcState) -> Result<Value, String> {
    let device_count = store.list_devices(10_000).map_err(|e| e.to_string())?.len();
    let detection_count = store
        .list_assets(None, None, false, 10_000)
        .map_err(|e| e.to_string())?
        .len();
    let status = state.snapshot();
    Ok(json!({
        "healthy": true,
        "started_at": status.started_at,
        "last_scan_at": status.last_scan_at,
        "last_scan_asset_count": status.last_scan_asset_count,
        "device_count": device_count,
        "detection_count": detection_count,
    }))
}

fn detections_result(store: &Store) -> Result<Value, String> {
    let detections = store
        .list_assets(None, None, false, 10_000)
        .map_err(|e| e.to_string())?
        .into_iter()
        .map(|asset| SanitizedDetection {
            kind: asset.kind,
            name: asset.name,
            vendor: asset.vendor,
            version: asset.version,
            running: asset.running,
            present: asset.present,
            last_seen: asset.last_seen,
        })
        .collect::<Vec<_>>();
    Ok(json!({"detections": detections}))
}

fn prepare_socket_path(config: &IpcConfig) -> Result<(), IpcError> {
    let parent = config
        .socket_path
        .parent()
        .ok_or_else(|| IpcError::UnsafePath {
            path: config.socket_path.clone(),
            reason: "socket path has no parent directory".into(),
        })?;
    std::fs::create_dir_all(parent)?;
    if config.private_parent {
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
    }

    match std::fs::symlink_metadata(&config.socket_path) {
        Ok(metadata) => {
            let current_uid = unsafe { libc::geteuid() };
            if !metadata.file_type().is_socket() {
                return Err(IpcError::UnsafePath {
                    path: config.socket_path.clone(),
                    reason: "existing path is not a Unix socket".into(),
                });
            }
            if metadata.uid() != current_uid {
                return Err(IpcError::UnsafePath {
                    path: config.socket_path.clone(),
                    reason: "stale socket is not owned by the current effective user".into(),
                });
            }
            if std::os::unix::net::UnixStream::connect(&config.socket_path).is_ok() {
                return Err(IpcError::UnsafePath {
                    path: config.socket_path.clone(),
                    reason: "existing socket is accepting connections".into(),
                });
            }
            std::fs::remove_file(&config.socket_path)?;
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

fn set_socket_attributes(config: &IpcConfig) -> Result<(), IpcError> {
    if let Some((uid, gid)) = config.socket_owner {
        let path = CString::new(config.socket_path.as_os_str().as_bytes()).map_err(|_| {
            IpcError::InvalidConfig("socket path contains an interior NUL byte".into())
        })?;
        if unsafe { libc::chown(path.as_ptr(), uid, gid) } != 0 {
            return Err(io::Error::last_os_error().into());
        }
    }
    std::fs::set_permissions(
        &config.socket_path,
        std::fs::Permissions::from_mode(config.socket_mode),
    )?;
    Ok(())
}

pub fn peer_identity(stream: &UnixStream) -> io::Result<PeerIdentity> {
    let fd = stream.as_raw_fd();

    #[cfg(any(
        target_os = "macos",
        target_os = "freebsd",
        target_os = "openbsd",
        target_os = "netbsd"
    ))]
    {
        let mut uid: libc::uid_t = 0;
        let mut gid: libc::gid_t = 0;
        if unsafe { libc::getpeereid(fd, &mut uid, &mut gid) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(PeerIdentity {
            uid,
            gid,
            pid: None,
        })
    }

    #[cfg(target_os = "linux")]
    {
        let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
        let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        let result = unsafe {
            libc::getsockopt(
                fd,
                libc::SOL_SOCKET,
                libc::SO_PEERCRED,
                (&mut credentials as *mut libc::ucred).cast(),
                &mut length,
            )
        };
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        return Ok(PeerIdentity {
            uid: credentials.uid,
            gid: credentials.gid,
            pid: Some(credentials.pid),
        });
    }

    #[cfg(not(any(
        target_os = "macos",
        target_os = "freebsd",
        target_os = "openbsd",
        target_os = "netbsd",
        target_os = "linux"
    )))]
    {
        let _ = fd;
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "peer credentials are unsupported on this platform",
        ))
    }
}

pub fn default_user_socket_path() -> Result<PathBuf, IpcError> {
    let home = std::env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| IpcError::InvalidConfig("HOME is required for user IPC mode".into()))?;
    Ok(Path::new(&home).join(".edgedisco").join("edgedisco.sock"))
}
