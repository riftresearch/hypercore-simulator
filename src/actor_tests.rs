use crate::{
    actor::{Actor, Reply, Request, Server},
    engine::Engine,
    wire::{Address, Control, Funding, Info},
};
use axum::{
    body::{Bytes, to_bytes},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use std::{
    sync::mpsc,
    thread::{self, JoinHandle},
};
use tokio::sync::oneshot;

const USER: &str = "0x1111111111111111111111111111111111111111";
const META: &str = r#"{"tokens":[{"index":0,"name":"USDC","tokenId":"0x1","szDecimals":8,"weiDecimals":8}],"universe":[]}"#;

struct RunningActor {
    actor: Option<Actor>,
    thread: Option<JoinHandle<()>>,
    start: Option<mpsc::Sender<()>>,
}

impl RunningActor {
    fn new(max_inflight: usize, paused: bool) -> Self {
        let server = Server::new(Engine::new(META).unwrap(), false, Some(1));
        let (actor, thread, start) = if paused {
            let (start, ready) = mpsc::channel();
            let (sender, receiver) = tokio::sync::mpsc::channel(max_inflight);
            let actor = Actor::new(sender, max_inflight, false);
            let thread = thread::spawn(move || {
                let _ = ready.recv();
                server.run(receiver);
            });
            (actor, thread, Some(start))
        } else {
            let (actor, thread) = Actor::spawn(server, max_inflight).unwrap();
            (actor, thread, None)
        };
        Self {
            actor: Some(actor),
            thread: Some(thread),
            start,
        }
    }

    fn actor(&self) -> &Actor {
        self.actor.as_ref().unwrap()
    }

    fn release(&mut self) {
        // Disconnecting the startup gate also releases it during unwinding.
        self.start.take();
    }

    fn shutdown(&mut self) -> thread::Result<()> {
        self.actor.take();
        self.release();
        self.thread.take().unwrap().join()
    }
}

impl Drop for RunningActor {
    fn drop(&mut self) {
        if self.thread.is_some() {
            let _ = self.shutdown();
        }
    }
}

fn submit(actor: &Actor, request: Request) -> oneshot::Receiver<Reply> {
    let admission = actor.admit().unwrap_or_else(|error| {
        panic!(
            "unexpected admission rejection: {}",
            error.into_response().status()
        )
    });
    actor.submit(admission, request).unwrap_or_else(|error| {
        panic!(
            "unexpected enqueue rejection: {}",
            error.into_response().status()
        )
    })
}

fn fund(amount: &str) -> Request {
    let body = json!({"address": USER, "token": "USDC", "amount": amount, "mode": "transfer"});
    let funding: Funding = serde_json::from_value(body.clone()).unwrap();
    Request::Control {
        control: Control::Fund(funding),
        raw: Bytes::from(body.to_string()),
    }
}

fn balances() -> Request {
    Request::Info(Info::ClearinghouseState {
        user: Address::parse(USER).unwrap(),
    })
}

fn reset() -> Request {
    Request::Control {
        control: Control::Reset,
        raw: Bytes::from_static(b"{}"),
    }
}

async fn response_json(response: Response) -> Value {
    let status = response.status();
    let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    serde_json::from_slice(&body).unwrap()
}

async fn reply_json(receiver: oneshot::Receiver<Reply>) -> Value {
    response_json(
        receiver
            .await
            .expect("accepted command lost its reply")
            .respond()
            .await,
    )
    .await
}

fn assert_usdc(value: &Value, amount: &str) {
    let balance = value["balances"]
        .as_array()
        .unwrap()
        .iter()
        .find(|balance| balance["coin"] == "USDC")
        .expect("funded account has no USDC balance");
    assert_eq!(balance["total"], amount);
}

fn assert_saturated(actor: &Actor) {
    match actor.admit() {
        Ok(_) => panic!("occupied admission was reused"),
        Err(error) => assert_eq!(
            error.into_response().status(),
            StatusCode::SERVICE_UNAVAILABLE
        ),
    }
}

#[tokio::test]
async fn funding_queries_and_reset_follow_enqueue_order() {
    let mut running = RunningActor::new(6, false);
    let actor = running.actor();
    let first_fund = submit(actor, fund("11"));
    let before_reset = submit(actor, balances());
    let reset = submit(actor, reset());
    let after_reset = submit(actor, balances());
    let second_fund = submit(actor, fund("7"));
    let after_refund = submit(actor, balances());

    // Read replies in reverse: waiting on a reply must not determine execution order.
    assert_usdc(&reply_json(after_refund).await, "7.0");
    reply_json(second_fund).await;
    assert_eq!(reply_json(after_reset).await["balances"], json!([]));
    reply_json(reset).await;
    assert_usdc(&reply_json(before_reset).await, "11.0");
    reply_json(first_fund).await;
    running.shutdown().unwrap();
}

#[tokio::test]
async fn queued_mutation_commits_after_reply_receiver_is_dropped() {
    let mut running = RunningActor::new(2, true);
    let mutation = submit(running.actor(), fund("13"));
    let query = submit(running.actor(), balances());
    drop(mutation);
    running.release();

    assert_usdc(&reply_json(query).await, "13.0");
    running.shutdown().unwrap();
}

#[tokio::test]
async fn retained_reply_holds_admission_until_dropped() {
    let mut running = RunningActor::new(1, false);
    let actor = running.actor();
    let reply = submit(actor, fund("17"))
        .await
        .expect("accepted command lost its reply");
    assert_saturated(actor);
    drop(reply);

    let admission = actor.admit().unwrap_or_else(|error| {
        panic!(
            "dropped reply retained admission: {}",
            error.into_response().status()
        )
    });
    assert_usdc(
        &response_json(actor.call(admission, balances()).await).await,
        "17.0",
    );
    running.shutdown().unwrap();
}

#[tokio::test]
async fn dropping_all_handles_drains_accepted_commands_and_exits() {
    let mut running = RunningActor::new(2, true);
    let actor_clone = running.actor().clone();
    let mutation = submit(running.actor(), fund("19"));
    let query = submit(&actor_clone, balances());
    drop(actor_clone);

    // Drop the final handle before releasing the worker. Joining before receiving
    // replies proves shutdown drains commands without waiting for consumers.
    running.shutdown().unwrap();
    reply_json(mutation).await;
    assert_usdc(&reply_json(query).await, "19.0");
}
