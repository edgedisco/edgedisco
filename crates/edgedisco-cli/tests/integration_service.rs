use edgedisco_cli::service::{
    resolve_service_labels, ServiceAction, ServiceError, ServiceExecutor, ServiceManager,
    AGENT_LABEL, EXPORTER_LABEL, SERVER_LABEL, SERVICE_LABELS,
};
use std::sync::{Arc, Mutex};

#[derive(Default)]
struct MockServiceExecutor {
    executed_commands: Arc<Mutex<Vec<(String, String, bool)>>>, // (action, label, is_root)
}

impl ServiceExecutor for MockServiceExecutor {
    fn execute(
        &self,
        action: ServiceAction,
        label: &str,
        is_root: bool,
    ) -> Result<String, ServiceError> {
        let action_str = match action {
            ServiceAction::Start => "start",
            ServiceAction::Stop => "stop",
            ServiceAction::Restart => "restart",
        };
        self.executed_commands.lock().unwrap().push((
            action_str.to_string(),
            label.to_string(),
            is_root,
        ));
        Ok("ok".to_string())
    }

    fn check_root_privileges(&self) -> Result<(), ServiceError> {
        Ok(())
    }
}

struct NonRootPrivilegeExecutor;

impl ServiceExecutor for NonRootPrivilegeExecutor {
    fn execute(
        &self,
        _action: ServiceAction,
        _label: &str,
        _is_root: bool,
    ) -> Result<String, ServiceError> {
        Ok("ok".to_string())
    }

    fn check_root_privileges(&self) -> Result<(), ServiceError> {
        Err(ServiceError::PrivilegeRequired(
            "privileged service lifecycle requires root privileges (UID 0)".to_string(),
        ))
    }
}

#[test]
fn test_resolve_service_labels() {
    let resolved =
        resolve_service_labels(&["server", "agent", "exporter"]).expect("resolve labels");
    assert_eq!(resolved, vec![SERVER_LABEL, AGENT_LABEL, EXPORTER_LABEL]);

    let resolved_aliases =
        resolve_service_labels(&["otlp-export", "SERVER"]).expect("resolve aliases");
    assert_eq!(resolved_aliases, vec![EXPORTER_LABEL, SERVER_LABEL]);

    let all = resolve_service_labels(&[] as &[&str]).expect("resolve default all");
    assert_eq!(all, SERVICE_LABELS.to_vec());
}

#[test]
fn test_resolve_unknown_service_fails() {
    let result = resolve_service_labels(&["nonexistent-service"]);
    match result {
        Err(ServiceError::UnknownService(name)) => {
            assert_eq!(name, "nonexistent-service");
        }
        _ => panic!("expected UnknownService error"),
    }
}

#[test]
fn test_service_start_sequence_order() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let executor = MockServiceExecutor {
        executed_commands: Arc::clone(&log),
    };
    let manager = ServiceManager::with_executor(Box::new(executor));

    let results = manager
        .start(&["exporter", "agent", "server"], false)
        .expect("start services");

    assert_eq!(results.len(), 3);

    // Verify forward dependency order: server -> agent -> exporter
    let executed = log.lock().unwrap().clone();
    assert_eq!(executed.len(), 3);
    assert_eq!(executed[0].1, SERVER_LABEL);
    assert_eq!(executed[1].1, AGENT_LABEL);
    assert_eq!(executed[2].1, EXPORTER_LABEL);
}

#[test]
fn test_service_stop_reverse_sequence_order() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let executor = MockServiceExecutor {
        executed_commands: Arc::clone(&log),
    };
    let manager = ServiceManager::with_executor(Box::new(executor));

    let results = manager
        .stop(&["server", "exporter", "agent"], false)
        .expect("stop services");

    assert_eq!(results.len(), 3);

    // Verify reverse dependency order: exporter -> agent -> server
    let executed = log.lock().unwrap().clone();
    assert_eq!(executed.len(), 3);
    assert_eq!(executed[0].1, EXPORTER_LABEL);
    assert_eq!(executed[1].1, AGENT_LABEL);
    assert_eq!(executed[2].1, SERVER_LABEL);
}

#[test]
fn test_service_restart_stop_then_start_coordination() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let executor = MockServiceExecutor {
        executed_commands: Arc::clone(&log),
    };
    let manager = ServiceManager::with_executor(Box::new(executor));

    let results = manager
        .restart(&["server", "agent"], false)
        .expect("restart services");

    assert_eq!(results.len(), 2);

    let executed = log.lock().unwrap().clone();
    // Stop agent then server (reverse order)
    assert_eq!(
        executed[0],
        ("stop".to_string(), AGENT_LABEL.to_string(), false)
    );
    assert_eq!(
        executed[1],
        ("stop".to_string(), SERVER_LABEL.to_string(), false)
    );
    // Start server then agent (forward order)
    assert_eq!(
        executed[2],
        ("start".to_string(), SERVER_LABEL.to_string(), false)
    );
    assert_eq!(
        executed[3],
        ("start".to_string(), AGENT_LABEL.to_string(), false)
    );
}

#[test]
fn test_privileged_root_flag_requires_root_privileges() {
    let executor = NonRootPrivilegeExecutor;
    let manager = ServiceManager::with_executor(Box::new(executor));

    let result = manager.start(&["server"], true);
    assert!(result.is_err());
    match result {
        Err(ServiceError::PrivilegeRequired(msg)) => {
            assert!(msg.contains("root"));
        }
        _ => panic!("expected PrivilegeRequired error"),
    }
}
