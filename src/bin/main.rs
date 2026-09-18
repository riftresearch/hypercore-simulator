use clap::Parser;
use hypercore_simulator::{Args, run_exchange};

#[tokio::main]
async fn main() {
    if let Err(error) = run_exchange(Args::parse()).await {
        eprintln!("hypercore-simulator: {error}");
        std::process::exit(1);
    }
}
