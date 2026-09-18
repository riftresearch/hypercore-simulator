//! Evidence-driven simulator of a subset of the Hyperliquid spot HTTP API.
//!
//! Run it as the `hypercore-simulator` binary, or embed it:
//!
//! ```no_run
//! # async fn example() -> std::io::Result<()> {
//! use hypercore_simulator::{Args, Simulator};
//!
//! let simulator = Simulator::bind(Args::default()).await?;
//! let base_url = format!("http://{}", simulator.local_addr()?);
//! simulator.serve(async { /* resolve to stop */ }).await
//! # }
//! ```
//!
//! One dedicated thread owns all simulator state; HTTP handlers parse and
//! authenticate requests, then submit typed commands through a bounded mailbox.

pub mod actor;
pub mod crypto;
pub mod decimal;
pub mod engine;
pub mod error;
pub mod fees;
pub mod http;
pub mod wire;

#[cfg(test)]
mod actor_tests;

pub use engine::{MAINNET_SPOT_META, TESTNET_SPOT_META};

use actor::{Actor, Server};
use engine::Engine;
use std::{borrow::Cow, fmt, io, net::SocketAddr, path::PathBuf, thread::JoinHandle};
use tokio::net::TcpListener;

/// Launch configuration; with the `cli` feature it is also the binary's command line.
#[derive(Clone, Debug)]
#[cfg_attr(feature = "cli", derive(clap::Parser))]
#[cfg_attr(
    feature = "cli",
    command(
        name = "hypercore-simulator",
        about = "Local-only Hyperliquid spot HTTP simulator",
        long_about = "Simulator of selected Hyperliquid spot HTTP paths. The /_test/* controls are \
                      unauthenticated, so expose a non-loopback bind only on a trusted network. No \
                      upstream requests are made."
    )
)]
pub struct Args {
    /// A captured spotMeta JSON file to use instead of the network's bundled capture.
    #[cfg_attr(feature = "cli", arg(long))]
    pub meta: Option<PathBuf>,
    /// Address to listen on; the unauthenticated test controls are served there too.
    #[cfg_attr(feature = "cli", arg(long, default_value = "127.0.0.1:3000"))]
    pub bind: SocketAddr,
    /// Signature domain, dust policy and bundled metadata to emulate locally.
    /// The measured behavior and every parity suite are testnet evidence.
    #[cfg_attr(feature = "cli", arg(long, value_enum, default_value_t = Network::Mainnet))]
    pub network: Network,
    /// Admitted in-flight requests, including body buffering and delayed replies.
    #[cfg_attr(feature = "cli", arg(long, default_value_t = 1024))]
    pub max_inflight: usize,
}

impl Default for Args {
    /// Mainnet signatures and metadata on `127.0.0.1:3000`.
    fn default() -> Self {
        Self {
            meta: None,
            bind: SocketAddr::from(([127, 0, 0, 1], 3000)),
            network: Network::Mainnet,
            max_inflight: 1024,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[cfg_attr(feature = "cli", derive(clap::ValueEnum))]
#[cfg_attr(feature = "cli", value(rename_all = "lowercase"))]
pub enum Network {
    Testnet,
    Mainnet,
}

impl Network {
    /// The captured `spotMeta` served when no `--meta` override is given.
    pub fn bundled_spot_meta(self) -> &'static str {
        match self {
            Self::Testnet => TESTNET_SPOT_META,
            Self::Mainnet => MAINNET_SPOT_META,
        }
    }
}

impl fmt::Display for Network {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Testnet => "testnet",
            Self::Mainnet => "mainnet",
        })
    }
}

/// A bound, not yet serving simulator.
pub struct Simulator {
    listener: TcpListener,
    actor: Actor,
    worker: JoinHandle<()>,
    network: Network,
}

impl Simulator {
    /// Load metadata, start the state thread and bind the listener.
    pub async fn bind(args: Args) -> io::Result<Self> {
        let meta = match &args.meta {
            Some(path) => Cow::Owned(std::fs::read_to_string(path)?),
            None => Cow::Borrowed(args.network.bundled_spot_meta()),
        };
        let engine = Engine::new(&meta)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
        let server = Server::new(engine, args.network == Network::Mainnet, None);
        let listener = TcpListener::bind(args.bind).await?;
        let (actor, worker) = Actor::spawn(server, args.max_inflight)?;
        Ok(Self {
            listener,
            actor,
            worker,
            network: args.network,
        })
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }

    pub fn network(&self) -> Network {
        self.network
    }

    /// Serve until `shutdown` resolves, then drain queued work and join the
    /// state thread. State lives only in memory.
    pub async fn serve(
        self,
        shutdown: impl Future<Output = ()> + Send + 'static,
    ) -> io::Result<()> {
        let serving = axum::serve(self.listener, http::router(self.actor))
            .with_graceful_shutdown(shutdown)
            .await;
        tokio::task::spawn_blocking(move || self.worker.join())
            .await
            .map_err(io::Error::other)?
            .map_err(|_| io::Error::other("Simulator actor panicked"))?;
        serving
    }
}

/// Run the simulator until Ctrl-C, announcing the bound address on stdout.
pub async fn run_exchange(args: Args) -> io::Result<()> {
    let simulator = Simulator::bind(args).await?;
    println!(
        "Listening on http://{} ({} signatures)",
        simulator.local_addr()?,
        simulator.network()
    );
    simulator
        .serve(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
}
