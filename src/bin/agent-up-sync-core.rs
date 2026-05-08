use agent_up_sync_core::{run_shadow_transaction, SyncCoreRequest};
use std::io::{self, Read};

fn main() {
    let mut input = String::new();
    if let Err(exc) = io::stdin().read_to_string(&mut input) {
        eprintln!("failed to read sync-core request from stdin: {exc}");
        std::process::exit(2);
    }
    let request: SyncCoreRequest = match serde_json::from_str(&input) {
        Ok(request) => request,
        Err(exc) => {
            eprintln!("failed to parse sync-core request JSON: {exc}");
            std::process::exit(2);
        }
    };
    let response = run_shadow_transaction(request);
    match serde_json::to_string(&response) {
        Ok(json) => println!("{json}"),
        Err(exc) => {
            eprintln!("failed to render sync-core response JSON: {exc}");
            std::process::exit(2);
        }
    }
}
