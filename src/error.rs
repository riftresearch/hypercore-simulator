//! The simulator's error is its message: upstream reports failures as text, and
//! the text is the contract. Static messages never allocate.

use serde::{Serialize, Serializer};
use std::{borrow::Cow, fmt};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Error(Cow<'static, str>);

pub type Result<T, E = Error> = std::result::Result<T, E>;

impl Error {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl From<&'static str> for Error {
    fn from(message: &'static str) -> Self {
        Self(Cow::Borrowed(message))
    }
}

impl From<String> for Error {
    fn from(message: String) -> Self {
        Self(Cow::Owned(message))
    }
}

impl From<Error> for Cow<'static, str> {
    fn from(error: Error) -> Self {
        error.0
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

impl Serialize for Error {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0)
    }
}
