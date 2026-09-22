use crate::process::{ProcessObservation, ProcessScanError, ProcessScanner};
use regex::Regex;
use std::process::Command;
use std::sync::OnceLock;

static PS_REGEX: OnceLock<Regex> = OnceLock::new();

/// Process scanner based on standard Unix `ps` utility.
#[derive(Debug, Default, Clone, Copy)]
pub struct FallbackProcessScanner;

impl FallbackProcessScanner {
    pub fn new() -> Self {
        Self
    }
}

impl ProcessScanner for FallbackProcessScanner {
    fn scan(&self) -> Result<Vec<ProcessObservation>, ProcessScanError> {
        let output = Command::new("ps")
            .args(["-eo", "pid=,ppid=,comm=,args="])
            .output();

        let output = match output {
            Ok(o) if o.status.success() => o,
            _ => {
                // Fallback to user-scoped ps
                Command::new("ps")
                    .args(["-o", "pid=,ppid=,comm=,args="])
                    .output()
                    .map_err(|e| ProcessScanError::Unavailable(format!("ps command failed: {e}")))?
            }
        };

        if !output.status.success() {
            return Err(ProcessScanError::Unavailable(
                "ps command returned non-zero exit status".into(),
            ));
        }

        let text = String::from_utf8_lossy(&output.stdout);
        parse_ps_output(&text)
    }
}

/// Parse lines from standard `ps` output into `ProcessObservation` records.
pub fn parse_ps_output(text: &str) -> Result<Vec<ProcessObservation>, ProcessScanError> {
    let re = PS_REGEX.get_or_init(|| {
        Regex::new(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$")
            .expect("PS regex compilation should not fail")
    });

    let mut observations = Vec::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Some(caps) = re.captures(trimmed) {
            let pid = caps[1].parse::<u32>().ok();
            let ppid = caps[2].parse::<u32>().ok();
            let comm = caps[3].to_string();
            let args_raw = caps[4].to_string();

            let args = if let Some(split) = shlex::split(&args_raw) {
                split
            } else {
                vec![args_raw]
            };

            observations.push(ProcessObservation::new(pid, ppid, comm, args));
        }
    }

    if observations.is_empty() {
        Err(ProcessScanError::Unavailable(
            "process enumeration returned no rows".into(),
        ))
    } else {
        Ok(observations)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_ps_output() {
        let sample = "  101     1 /bin/launchd /bin/launchd\n  500   101 /usr/local/bin/cursor /Applications/Cursor.app/Contents/MacOS/Cursor --type=renderer\n  501   500 node /usr/local/bin/claude arg1 arg2\n";
        let rows = parse_ps_output(sample).expect("parsing ps output should succeed");
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].pid, Some(101));
        assert_eq!(rows[0].ppid, Some(1));
        assert_eq!(rows[0].executable, "/bin/launchd");
        assert_eq!(rows[1].pid, Some(500));
        assert_eq!(rows[1].ppid, Some(101));
        assert_eq!(rows[1].executable, "/usr/local/bin/cursor");
        assert_eq!(rows[2].pid, Some(501));
        assert_eq!(rows[2].ppid, Some(500));
        assert_eq!(rows[2].executable, "node");
        assert_eq!(rows[2].args, vec!["/usr/local/bin/claude", "arg1", "arg2"]);
    }
}
