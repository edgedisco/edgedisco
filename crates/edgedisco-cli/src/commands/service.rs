use crate::cli::ServiceArgs;
use crate::service::ServiceManager;

/// Execute the `edgedisco start` command.
pub fn run_start(args: &ServiceArgs) -> Result<(), Box<dyn std::error::Error>> {
    let manager = ServiceManager::new();
    let results = manager.start(&args.services, args.root)?;
    for (service, state) in results {
        println!("{service}: {state}");
    }
    Ok(())
}

/// Execute the `edgedisco stop` command.
pub fn run_stop(args: &ServiceArgs) -> Result<(), Box<dyn std::error::Error>> {
    let manager = ServiceManager::new();
    let results = manager.stop(&args.services, args.root)?;
    for (service, state) in results {
        println!("{service}: {state}");
    }
    Ok(())
}

/// Execute the `edgedisco restart` command.
pub fn run_restart(args: &ServiceArgs) -> Result<(), Box<dyn std::error::Error>> {
    let manager = ServiceManager::new();
    let results = manager.restart(&args.services, args.root)?;
    for (service, state) in results {
        println!("{service}: {state}");
    }
    Ok(())
}
