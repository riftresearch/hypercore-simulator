//! One dedicated thread owns all simulator state. HTTP handlers submit typed
//! commands through a bounded FIFO mailbox and receive serialized replies.

use crate::{
    engine::{Engine, ExchangeReply},
    error::Error,
    wire::{Address, Control, Envelope, FaultKind, FaultSpec, Info, Rejection},
};
use axum::{
    body::{Body, Bytes},
    extract::FromRequestParts,
    http::{StatusCode, header, request::Parts},
    response::{IntoResponse, Response},
};
use serde::Serialize;
use std::{
    borrow::Cow,
    collections::VecDeque,
    sync::Arc,
    thread::{self, JoinHandle},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot};

#[derive(Clone)]
pub struct Actor {
    sender: mpsc::Sender<Command>,
    admissions: Arc<Semaphore>,
    is_mainnet: bool,
}

/// A held slot in the request limit, released when the reply is dropped.
pub struct Admission {
    _permit: OwnedSemaphorePermit,
}

pub enum Request {
    Info(Info),
    /// The raw body seeds the deterministic transaction hash. The signer is
    /// recovered on the HTTP worker; the state thread only sequences it after
    /// injected faults, exactly as before.
    Exchange {
        envelope: Envelope,
        raw: Bytes,
        signer: Result<Address, Error>,
    },
    Control {
        control: Control,
        raw: Bytes,
    },
}

pub struct Command {
    request: Request,
    reply: oneshot::Sender<Reply>,
    admission: Admission,
}

pub struct Reply {
    result: Result<(Bytes, Option<Fault>), Failure>,
    _admission: Admission,
}

pub enum Failure {
    Text(u16, Cow<'static, str>),
    JsonText(u16, Cow<'static, str>),
    Exchange(Cow<'static, str>),
}

impl IntoResponse for Failure {
    fn into_response(self) -> Response {
        match self {
            Self::Text(status, body) => text(status, body),
            Self::JsonText(status, body) => {
                let mut response = text(status, body);
                response
                    .headers_mut()
                    .insert(header::CONTENT_TYPE, "application/json".parse().unwrap());
                response
            }
            Self::Exchange(body) => exchange_error(body),
        }
    }
}

impl Actor {
    pub fn spawn(server: Server, max_inflight: usize) -> std::io::Result<(Self, JoinHandle<()>)> {
        if max_inflight == 0 || max_inflight > Semaphore::MAX_PERMITS {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "max-inflight is outside the supported positive range",
            ));
        }
        let (sender, receiver) = mpsc::channel::<Command>(max_inflight);
        let admissions = Arc::new(Semaphore::new(max_inflight));
        let is_mainnet = server.is_mainnet;
        let worker = thread::Builder::new()
            .name("spot-state".into())
            .spawn(move || server.run(receiver))?;
        Ok((
            Self {
                sender,
                admissions,
                is_mainnet,
            },
            worker,
        ))
    }

    #[cfg(test)]
    pub fn new(sender: mpsc::Sender<Command>, max_inflight: usize, is_mainnet: bool) -> Self {
        Self {
            sender,
            admissions: Arc::new(Semaphore::new(max_inflight)),
            is_mainnet,
        }
    }

    pub fn is_mainnet(&self) -> bool {
        self.is_mainnet
    }

    pub fn admit(&self) -> Result<Admission, Failure> {
        if self.sender.is_closed() {
            return Err(Failure::Text(503, "Simulator actor unavailable".into()));
        }
        self.admissions
            .clone()
            .try_acquire_owned()
            .map(|permit| Admission { _permit: permit })
            .map_err(|_| Failure::Text(503, "Simulator request limit reached".into()))
    }

    pub fn submit(
        &self,
        admission: Admission,
        request: Request,
    ) -> Result<oneshot::Receiver<Reply>, Failure> {
        let (reply, receiver) = oneshot::channel();
        // Every queued command owns an admission, so the mailbox cannot overflow
        // while another correctly admitted request is waiting to enqueue.
        self.sender
            .try_send(Command {
                request,
                reply,
                admission,
            })
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => {
                    Failure::Text(503, "Simulator request limit reached".into())
                }
                mpsc::error::TrySendError::Closed(_) => {
                    Failure::Text(503, "Simulator actor unavailable".into())
                }
            })?;
        Ok(receiver)
    }

    pub async fn call(&self, admission: Admission, request: Request) -> Response {
        match self.submit(admission, request) {
            Ok(receiver) => match receiver.await {
                Ok(reply) => reply.respond().await,
                Err(_) => text(503, "Simulator actor unavailable"),
            },
            Err(error) => error.into_response(),
        }
    }
}

impl FromRequestParts<Actor> for Admission {
    type Rejection = Failure;

    async fn from_request_parts(
        _parts: &mut Parts,
        actor: &Actor,
    ) -> Result<Self, Self::Rejection> {
        actor.admit()
    }
}

impl Reply {
    pub async fn respond(self) -> Response {
        let Self { result, _admission } = self;
        let (body, fault) = match result {
            Ok(result) => result,
            Err(error) => return error.into_response(),
        };
        match fault.map(|fault| fault.kind) {
            Some(FaultKind::Delay) => {
                tokio::time::sleep(Duration::from_millis(fault.map_or(0, |f| f.delay_ms))).await;
            }
            Some(FaultKind::DropAfterCommit) => {
                return Response::new(Body::from_stream(futures_util::stream::once(async {
                    Err::<Bytes, _>(std::io::Error::new(
                        std::io::ErrorKind::ConnectionReset,
                        "injected response drop",
                    ))
                })));
            }
            _ => {}
        }
        json_response(body)
    }
}

pub struct Server {
    engine: Engine,
    is_mainnet: bool,
    clock: Option<u64>,
    /// Injected faults with their message, consumed one per exchange request.
    faults: VecDeque<(Fault, String)>,
}

/// The part of an injected fault that outlives the state thread's reply.
#[derive(Clone, Copy)]
pub struct Fault {
    kind: FaultKind,
    delay_ms: u64,
}

impl Server {
    pub fn new(engine: Engine, is_mainnet: bool, clock: Option<u64>) -> Self {
        Self {
            engine,
            is_mainnet,
            clock,
            faults: VecDeque::new(),
        }
    }

    pub fn run(mut self, mut receiver: mpsc::Receiver<Command>) {
        while let Some(command) = receiver.blocking_recv() {
            let result = match command.request {
                Request::Info(query) => self.info(&query).map(|body| (body, None)),
                Request::Exchange {
                    envelope,
                    raw,
                    signer,
                } => self.exchange(&envelope, &raw, signer),
                Request::Control { control, raw } => {
                    self.control(control, &raw).map(|body| (body, None))
                }
            };
            // Enqueued work commits even if its caller no longer wants the reply.
            let _ = command.reply.send(Reply {
                result,
                _admission: command.admission,
            });
        }
    }

    fn now(&self) -> u64 {
        self.clock.unwrap_or_else(|| {
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as u64
        })
    }

    fn advance(&mut self) -> Result<u64, String> {
        let now = self.now();
        self.engine
            .advance(now, self.is_mainnet)
            .map_err(|error| error.to_string())?;
        Ok(now)
    }

    fn info(&mut self, query: &Info) -> Result<Bytes, Failure> {
        let now = self
            .advance()
            .map_err(|error| Failure::Text(500, error.into()))?;
        match self.engine.info(query, now) {
            Ok(reply) => serialize(&reply),
            Err(Rejection::Unprocessable) => Err(Failure::Text(
                422,
                "Failed to deserialize the JSON body into the target type".into(),
            )),
            Err(Rejection::Null) => Err(Failure::JsonText(500, "null".into())),
        }
    }

    fn exchange(
        &mut self,
        envelope: &Envelope,
        raw: &[u8],
        signer: Result<Address, Error>,
    ) -> Result<(Bytes, Option<Fault>), Failure> {
        let now = self
            .advance()
            .map_err(|error| Failure::Exchange(error.into()))?;
        let fault = match self.faults.pop_front() {
            Some((fault, message)) => match fault.kind {
                FaultKind::Refuse => return Err(Failure::Exchange(message.into())),
                FaultKind::RateLimit => return Err(Failure::JsonText(429, message.into())),
                _ => Some(fault),
            },
            None => None,
        };
        let signer = signer.map_err(|_| Failure::Exchange("Unable to recover signer.".into()))?;
        let reply = self.engine.exchange(envelope, raw, signer, now);
        Ok((serialize(&reply)?, fault))
    }

    fn control(&mut self, control: Control, raw: &[u8]) -> Result<Bytes, Failure> {
        match control {
            Control::Time(time) => {
                self.clock = time.now_ms;
                self.advance()
                    .map_err(|error| Failure::Text(400, error.into()))?;
                serialize(&TimeReply {
                    now_ms: self.now(),
                    fixed: self.clock.is_some(),
                })
            }
            Control::Fault(FaultSpec {
                kind,
                delay_ms,
                message,
            }) => {
                if delay_ms > 60_000 {
                    return Err(Failure::Text(400, "delay_ms must not exceed 60000".into()));
                }
                let default_message = match kind {
                    FaultKind::RateLimit => "null",
                    _ => "Injected action refusal",
                };
                let message = message.unwrap_or_else(|| default_message.to_owned());
                self.faults.push_back((Fault { kind, delay_ms }, message));
                serialize(&QueuedReply {
                    queued: self.faults.len(),
                })
            }
            Control::Reset => {
                let now = self.now();
                let body = self.engine_control(Control::Reset, raw, now)?;
                self.faults.clear();
                self.clock = None;
                Ok(body)
            }
            other => {
                let now = self
                    .advance()
                    .map_err(|error| Failure::Text(400, error.into()))?;
                self.engine_control(other, raw, now)
            }
        }
    }

    fn engine_control(&mut self, control: Control, raw: &[u8], now: u64) -> Result<Bytes, Failure> {
        let reply = self
            .engine
            .control(control, raw, now)
            .map_err(|error| Failure::Text(400, error.into()))?;
        serialize(&reply)
    }
}

#[derive(Serialize)]
struct TimeReply {
    now_ms: u64,
    fixed: bool,
}

#[derive(Serialize)]
struct QueuedReply {
    queued: usize,
}

fn serialize<T: Serialize>(value: &T) -> Result<Bytes, Failure> {
    serde_json::to_vec(value)
        .map(Bytes::from)
        .map_err(|error| Failure::Text(500, error.to_string().into()))
}

pub fn text(status: u16, body: impl Into<String>) -> Response {
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        body.into(),
    )
        .into_response()
}

pub fn json_response(body: Bytes) -> Response {
    ([(header::CONTENT_TYPE, "application/json")], body).into_response()
}

pub fn exchange_error(message: impl Into<Cow<'static, str>>) -> Response {
    let reply = ExchangeReply::Err(message.into().into_owned().into());
    match serde_json::to_vec(&reply) {
        Ok(body) => json_response(Bytes::from(body)),
        Err(error) => text(500, error.to_string()),
    }
}
