//! The HTTP surface: body parsing with upstream's status semantics, the three
//! handlers, and the router.

use crate::{
    actor::{Actor, Admission, Request, exchange_error, text},
    crypto,
    wire::{Control, Envelope, InfoRequest, Object},
};
use axum::{
    Router,
    body::Bytes,
    extract::{Path, State},
    http::{HeaderMap, header},
    response::Response,
    routing::post,
};
use serde::{
    Deserialize,
    de::{DeserializeOwned, IgnoredAny},
};

pub fn router(actor: Actor) -> Router {
    Router::new()
        .route("/info", post(info))
        .route("/exchange", post(exchange))
        .route("/_test/{control}", post(control))
        .with_state(actor)
}

enum BodyError {
    UnsupportedMediaType,
    Syntax,
    Data(serde_json::Error),
}

impl BodyError {
    /// Upstream's fixed texts for the Hyperliquid surface.
    fn upstream(self) -> Response {
        match self {
            Self::UnsupportedMediaType => text(
                415,
                "Expected request with `Content-Type: application/json`",
            ),
            Self::Syntax => text(400, "Failed to parse the request body as JSON"),
            Self::Data(_) => text(
                422,
                "Failed to deserialize the JSON body into the target type",
            ),
        }
    }

    /// Test controls report what was wrong with the body.
    fn control(self) -> Response {
        match self {
            Self::Data(error) => text(400, format!("Simulator: {error}")),
            other => other.upstream(),
        }
    }
}

/// Parse one JSON value from the body, as Hyperliquid does: trailing bytes are
/// accepted, malformed JSON is a syntax failure, and a well-formed value that
/// does not fit the target type is a data failure.
fn parse_body<T: DeserializeOwned>(headers: &HeaderMap, body: &[u8]) -> Result<T, BodyError> {
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let mime = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if mime != "application/json" && !(mime.starts_with("application/") && mime.ends_with("+json"))
    {
        return Err(BodyError::UnsupportedMediaType);
    }
    match T::deserialize(&mut serde_json::Deserializer::from_slice(body)) {
        Ok(value) => Ok(value),
        Err(error) if error.is_data() => {
            // A typed parse can stop before a later syntax error, which takes precedence.
            match IgnoredAny::deserialize(&mut serde_json::Deserializer::from_slice(body)) {
                Ok(_) => Err(BodyError::Data(error)),
                Err(_) => Err(BodyError::Syntax),
            }
        }
        Err(_) => Err(BodyError::Syntax),
    }
}

async fn info(
    admission: Admission,
    State(actor): State<Actor>,
    headers: HeaderMap,
    bytes: Bytes,
) -> Response {
    match parse_body::<InfoRequest>(&headers, &bytes) {
        Ok(InfoRequest(query)) => actor.call(admission, Request::Info(query)).await,
        Err(error) => error.upstream(),
    }
}

async fn exchange(
    admission: Admission,
    State(actor): State<Actor>,
    headers: HeaderMap,
    bytes: Bytes,
) -> Response {
    let envelope: Envelope = match parse_body::<Object<Envelope>>(&headers, &bytes) {
        Ok(Object(envelope)) => envelope,
        Err(error) => return error.upstream(),
    };
    if envelope
        .action
        .user_signed_nonce()
        .is_some_and(|nonce| nonce != envelope.nonce)
    {
        return exchange_error("Nonce mismatch.");
    }
    // Public-key recovery is the costliest step and touches no state, so it
    // runs here on the HTTP worker pool rather than on the state thread.
    let signer = crypto::recover_signer(&envelope, actor.is_mainnet());
    actor
        .call(
            admission,
            Request::Exchange {
                envelope,
                raw: bytes,
                signer,
            },
        )
        .await
}

async fn control(
    admission: Admission,
    State(actor): State<Actor>,
    Path(name): Path<String>,
    headers: HeaderMap,
    bytes: Bytes,
) -> Response {
    fn parse<T: DeserializeOwned>(
        headers: &HeaderMap,
        bytes: &[u8],
        wrap: fn(T) -> Control,
    ) -> Result<Control, BodyError> {
        parse_body(headers, bytes).map(wrap)
    }
    let control = match name.as_str() {
        "time" => parse(&headers, &bytes, Control::Time),
        "fault" => parse(&headers, &bytes, Control::Fault),
        "reset" => parse(&headers, &bytes, |_: IgnoredAny| Control::Reset),
        "upgrade" => parse(&headers, &bytes, Control::Upgrade),
        "account" => parse(&headers, &bytes, Control::Account),
        "fees" => parse(&headers, &bytes, Control::Fees),
        "dust" => parse(&headers, &bytes, Control::Dust),
        "fund" => parse(&headers, &bytes, Control::Fund),
        "book" => parse(&headers, &bytes, Control::Book),
        "evm_sends" => parse(&headers, &bytes, |_: IgnoredAny| Control::EvmSends),
        _ => return text(400, "Simulator: unknown control endpoint"),
    };
    match control {
        Ok(control) => {
            actor
                .call(
                    admission,
                    Request::Control {
                        control,
                        raw: bytes,
                    },
                )
                .await
        }
        Err(error) => error.control(),
    }
}
