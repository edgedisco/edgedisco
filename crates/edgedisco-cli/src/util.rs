use edgedisco_core::redaction::sha256_digest;
use std::ffi::CStr;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

/// Generate an RFC 3339 UTC timestamp string.
pub fn current_timestamp() -> String {
    let now = SystemTime::now();
    let duration = now.duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = duration.as_secs() as i64;

    #[cfg(unix)]
    unsafe {
        let mut tm: libc::tm = std::mem::zeroed();
        libc::gmtime_r(&secs as *const _, &mut tm);
        format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
            tm.tm_year + 1900,
            tm.tm_mon + 1,
            tm.tm_mday,
            tm.tm_hour,
            tm.tm_min,
            tm.tm_sec
        )
    }

    #[cfg(not(unix))]
    {
        // Fallback for non-unix
        let days = secs / 86400;
        let rem_secs = secs % 86400;
        let hours = rem_secs / 3600;
        let rem_secs = rem_secs % 3600;
        let mins = rem_secs / 60;
        let s = rem_secs % 60;

        // Simple epoch days to year/month/day conversion
        let mut y = 1970;
        let mut d = days;
        loop {
            let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
            let days_in_year = if leap { 366 } else { 365 };
            if d < days_in_year {
                break;
            }
            d -= days_in_year;
            y += 1;
        }
        let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
        let days_in_months = [
            31,
            if leap { 29 } else { 28 },
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ];
        let mut m = 1;
        for &dim in &days_in_months {
            if d < dim {
                break;
            }
            d -= dim;
            m += 1;
        }
        let day = d + 1;
        format!("{y:04}-{m:02}-{day:02}T{hours:02}:{mins:02}:{s:02}Z")
    }
}

/// Retrieve the local hostname.
pub fn get_hostname() -> String {
    #[cfg(unix)]
    unsafe {
        let mut buf = [0u8; 256];
        if libc::gethostname(buf.as_mut_ptr() as *mut libc::c_char, buf.len()) == 0 {
            if let Ok(s) = CStr::from_ptr(buf.as_ptr() as *const libc::c_char).to_str() {
                if !s.is_empty() {
                    return s.to_string();
                }
            }
        }
    }

    if let Ok(host) = std::env::var("HOSTNAME") {
        if !host.is_empty() {
            return host;
        }
    }
    if let Ok(host) = std::env::var("HOST") {
        if !host.is_empty() {
            return host;
        }
    }

    "localhost".to_string()
}

/// Retrieve OS release / version string.
pub fn get_os_version() -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        unsafe {
            let mut name: libc::utsname = std::mem::zeroed();
            if libc::uname(&mut name) == 0 {
                if let Ok(release) = CStr::from_ptr(name.release.as_ptr()).to_str() {
                    return Some(release.to_string());
                }
            }
        }
        None
    }
    #[cfg(target_os = "linux")]
    {
        unsafe {
            let mut name: libc::utsname = std::mem::zeroed();
            if libc::uname(&mut name) == 0 {
                if let Ok(release) = CStr::from_ptr(name.release.as_ptr()).to_str() {
                    return Some(release.to_string());
                }
            }
        }
        None
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        None
    }
}

/// Retrieve machine architecture.
pub fn get_machine() -> Option<String> {
    Some(std::env::consts::ARCH.to_string())
}

/// Retrieve target OS name.
pub fn get_os_name() -> String {
    if cfg!(target_os = "macos") {
        "Darwin".to_string()
    } else if cfg!(target_os = "linux") {
        "Linux".to_string()
    } else if cfg!(target_os = "windows") {
        "Windows".to_string()
    } else {
        std::env::consts::OS.to_string()
    }
}

/// Stable local device identity shared by one-shot and daemon collection paths.
pub fn local_device_id(hostname: &str, os: &str, machine: Option<&str>) -> String {
    sha256_digest(format!("local:{hostname}:{os}:{}", machine.unwrap_or("")))[..32].to_string()
}

/// Generate an unguessable stored credential hash for local-only device rows.
pub fn random_token_hash() -> String {
    sha256_digest(Uuid::new_v4().as_bytes())
}

/// Default database path, either from explicit root directory or user home `~/.edgedisco/data/inventory.db`.
pub fn default_database_path(root: Option<&Path>, explicit_db: Option<&Path>) -> PathBuf {
    if let Some(db) = explicit_db {
        return db.to_path_buf();
    }
    if let Some(root_path) = root {
        return root_path.join("data/inventory.db");
    }

    if let Ok(home) = std::env::var("HOME") {
        return PathBuf::from(home).join(".edgedisco/data/inventory.db");
    }

    PathBuf::from("data/inventory.db")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_current_timestamp_rfc3339() {
        let ts = current_timestamp();
        assert!(ts.ends_with('Z'));
        assert!(edgedisco_core::redaction::validate_timestamp(&ts).is_ok());
    }

    #[test]
    fn test_system_info_helpers() {
        assert!(!get_hostname().is_empty());
        assert!(!get_os_name().is_empty());
        assert!(get_machine().is_some());
    }

    #[test]
    fn test_default_database_path() {
        let explicit = PathBuf::from("/custom/path.db");
        assert_eq!(
            default_database_path(None, Some(&explicit)),
            PathBuf::from("/custom/path.db")
        );

        let root = PathBuf::from("/opt/edgedisco");
        assert_eq!(
            default_database_path(Some(&root), None),
            PathBuf::from("/opt/edgedisco/data/inventory.db")
        );
    }
}
