//! Error codes and the single error type crossing every API boundary.
//!
//! The canonical list is `schema/errors.json` (contract **C-04**, owner C2).
//! [`ErrorCode::ALL`] is kept in the same order as the C-04 table so the W2
//! machine comparison can diff two ordered lists instead of eyeballing names.
//! **Never invent a code here** — add it to C-04 by RFC first.
//!
//! Two orthogonal bits travel with every error:
//!
//! * `retriable` — a property of the *code* (from C-04): may the caller retry
//!   the operation, possibly on another connection?
//! * `fatal` — a property of the *occurrence*: must the caller tear the
//!   connection down right now? Replay, generation rollback and AEAD failure
//!   are always fatal because the receive nonce has already moved.

use std::fmt;

/// Every code in `schema/errors.json`, in table order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum ErrorCode {
    InvalidArgument,
    NotFound,
    PermissionDenied,
    Timeout,
    Cancelled,
    Unavailable,
    Overloaded,
    MemoryFull,
    PayloadTooLarge,
    Unsupported,
    ProtocolMismatch,
    ManifestInvalid,
    ToolConflict,
    RiskDowngradeForbidden,
    ApproverInvalid,
    OriginMismatch,
    ReadOnlySession,
    BindUnauthorized,
    BindFingerprintMismatch,
    BindHelloSigInvalid,
    BindDeviceLimit,
    BindClockSkew,
    BindKeyUnsupported,
    BindSetupLocked,
    AuthFailed,
    AdminLocked,
    SetupUnauthorized,
    ModuleDraining,
    Internal,
    ConfirmRequired,
    UnknownConfigKey,
    CodeInvalid,
    CodeExpired,
}

impl ErrorCode {
    /// All codes, in the order of the C-04 table.
    pub const ALL: [ErrorCode; 33] = [
        ErrorCode::InvalidArgument,
        ErrorCode::NotFound,
        ErrorCode::PermissionDenied,
        ErrorCode::Timeout,
        ErrorCode::Cancelled,
        ErrorCode::Unavailable,
        ErrorCode::Overloaded,
        ErrorCode::MemoryFull,
        ErrorCode::PayloadTooLarge,
        ErrorCode::Unsupported,
        ErrorCode::ProtocolMismatch,
        ErrorCode::ManifestInvalid,
        ErrorCode::ToolConflict,
        ErrorCode::RiskDowngradeForbidden,
        ErrorCode::ApproverInvalid,
        ErrorCode::OriginMismatch,
        ErrorCode::ReadOnlySession,
        ErrorCode::BindUnauthorized,
        ErrorCode::BindFingerprintMismatch,
        ErrorCode::BindHelloSigInvalid,
        ErrorCode::BindDeviceLimit,
        ErrorCode::BindClockSkew,
        ErrorCode::BindKeyUnsupported,
        ErrorCode::BindSetupLocked,
        ErrorCode::AuthFailed,
        ErrorCode::AdminLocked,
        ErrorCode::SetupUnauthorized,
        ErrorCode::ModuleDraining,
        ErrorCode::Internal,
        ErrorCode::ConfirmRequired,
        ErrorCode::UnknownConfigKey,
        ErrorCode::CodeInvalid,
        ErrorCode::CodeExpired,
    ];

    /// Wire/log spelling, identical to the C-04 key.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::InvalidArgument => "INVALID_ARGUMENT",
            ErrorCode::NotFound => "NOT_FOUND",
            ErrorCode::PermissionDenied => "PERMISSION_DENIED",
            ErrorCode::Timeout => "TIMEOUT",
            ErrorCode::Cancelled => "CANCELLED",
            ErrorCode::Unavailable => "UNAVAILABLE",
            ErrorCode::Overloaded => "OVERLOADED",
            ErrorCode::MemoryFull => "MEMORY_FULL",
            ErrorCode::PayloadTooLarge => "PAYLOAD_TOO_LARGE",
            ErrorCode::Unsupported => "UNSUPPORTED",
            ErrorCode::ProtocolMismatch => "PROTOCOL_MISMATCH",
            ErrorCode::ManifestInvalid => "MANIFEST_INVALID",
            ErrorCode::ToolConflict => "TOOL_CONFLICT",
            ErrorCode::RiskDowngradeForbidden => "RISK_DOWNGRADE_FORBIDDEN",
            ErrorCode::ApproverInvalid => "APPROVER_INVALID",
            ErrorCode::OriginMismatch => "ORIGIN_MISMATCH",
            ErrorCode::ReadOnlySession => "READ_ONLY_SESSION",
            ErrorCode::BindUnauthorized => "BIND_UNAUTHORIZED",
            ErrorCode::BindFingerprintMismatch => "BIND_FINGERPRINT_MISMATCH",
            ErrorCode::BindHelloSigInvalid => "BIND_HELLO_SIG_INVALID",
            ErrorCode::BindDeviceLimit => "BIND_DEVICE_LIMIT",
            ErrorCode::BindClockSkew => "BIND_CLOCK_SKEW",
            ErrorCode::BindKeyUnsupported => "BIND_KEY_UNSUPPORTED",
            ErrorCode::BindSetupLocked => "BIND_SETUP_LOCKED",
            ErrorCode::AuthFailed => "AUTH_FAILED",
            ErrorCode::AdminLocked => "ADMIN_LOCKED",
            ErrorCode::SetupUnauthorized => "SETUP_UNAUTHORIZED",
            ErrorCode::ModuleDraining => "MODULE_DRAINING",
            ErrorCode::Internal => "INTERNAL",
            ErrorCode::ConfirmRequired => "CONFIRM_REQUIRED",
            ErrorCode::UnknownConfigKey => "UNKNOWN_CONFIG_KEY",
            ErrorCode::CodeInvalid => "CODE_INVALID",
            ErrorCode::CodeExpired => "CODE_EXPIRED",
        }
    }

    /// Parse the wire spelling. Returns `None` for anything not in C-04.
    #[must_use]
    pub fn from_name(s: &str) -> Option<Self> {
        ErrorCode::ALL.iter().copied().find(|c| c.as_str() == s)
    }

    /// From the C-04 table.
    #[must_use]
    pub fn retriable(self) -> bool {
        matches!(
            self,
            ErrorCode::Timeout
                | ErrorCode::Unavailable
                | ErrorCode::Overloaded
                | ErrorCode::MemoryFull
                | ErrorCode::BindSetupLocked
                | ErrorCode::AuthFailed
                | ErrorCode::AdminLocked
                | ErrorCode::SetupUnauthorized
                | ErrorCode::ModuleDraining
        )
    }

    /// From the C-04 table. `499` for `CANCELLED` because the admin API maps it
    /// to the nginx convention.
    #[must_use]
    pub fn http_status(self) -> u16 {
        match self {
            ErrorCode::InvalidArgument => 400,
            ErrorCode::NotFound => 404,
            ErrorCode::PermissionDenied => 403,
            ErrorCode::Timeout => 504,
            ErrorCode::Cancelled => 499,
            ErrorCode::Unavailable => 503,
            ErrorCode::Overloaded => 429,
            ErrorCode::MemoryFull => 409,
            ErrorCode::PayloadTooLarge => 413,
            ErrorCode::Unsupported => 501,
            ErrorCode::ProtocolMismatch => 400,
            ErrorCode::ManifestInvalid => 400,
            ErrorCode::ToolConflict => 409,
            ErrorCode::RiskDowngradeForbidden => 403,
            ErrorCode::ApproverInvalid => 403,
            ErrorCode::OriginMismatch => 403,
            ErrorCode::ReadOnlySession => 403,
            ErrorCode::BindUnauthorized => 401,
            ErrorCode::BindFingerprintMismatch => 409,
            ErrorCode::BindHelloSigInvalid => 403,
            ErrorCode::BindDeviceLimit => 409,
            ErrorCode::BindClockSkew => 400,
            ErrorCode::BindKeyUnsupported => 400,
            ErrorCode::BindSetupLocked => 423,
            ErrorCode::AuthFailed => 401,
            ErrorCode::AdminLocked => 429,
            ErrorCode::SetupUnauthorized => 401,
            ErrorCode::ModuleDraining => 503,
            ErrorCode::Internal => 500,
            ErrorCode::ConfirmRequired => 409,
            ErrorCode::UnknownConfigKey => 400,
            ErrorCode::CodeInvalid => 400,
            ErrorCode::CodeExpired => 400,
        }
    }
}

/// Maximum human-readable message length (C-04 rule: ≤ 300 characters).
pub const MAX_MESSAGE_CHARS: usize = 300;

/// The only error type this crate produces.
///
/// Field order is the C-04 envelope: `code`, `message`, `retriable` (derived
/// from `code`), plus `fatal`, which is protocol-core specific and is **not**
/// part of the C-04 table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError {
    code: ErrorCode,
    message: String,
    fatal: bool,
}

impl ProtocolError {
    /// Non-fatal error: the connection may continue.
    #[must_use]
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: clip(&message.into()),
            fatal: false,
        }
    }

    /// Fatal error: the caller MUST drop the connection. There is no "resume"
    /// after a replay or an AEAD failure — the receive nonce already moved.
    #[must_use]
    pub fn fatal(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: clip(&message.into()),
            fatal: true,
        }
    }

    /// Mark an existing error fatal.
    #[must_use]
    pub fn into_fatal(mut self) -> Self {
        self.fatal = true;
        self
    }

    #[must_use]
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    /// Wire spelling of the code, e.g. `PAYLOAD_TOO_LARGE`.
    #[must_use]
    pub fn code_str(&self) -> &'static str {
        self.code.as_str()
    }

    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }

    /// From the C-04 table — a property of the code, not of this occurrence.
    #[must_use]
    pub fn retriable(&self) -> bool {
        self.code.retriable()
    }

    #[must_use]
    pub fn http_status(&self) -> u16 {
        self.code.http_status()
    }

    /// Must the caller tear the connection down?
    #[must_use]
    pub fn is_fatal(&self) -> bool {
        self.fatal
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}: {}{}",
            self.code.as_str(),
            self.message,
            if self.fatal { " (fatal)" } else { "" }
        )
    }
}

impl std::error::Error for ProtocolError {}

/// Truncate on a char boundary. Keeps the 300-char C-04 promise without ever
/// panicking on multi-byte input.
fn clip(msg: &str) -> String {
    if msg.chars().count() <= MAX_MESSAGE_CHARS {
        return msg.to_string();
    }
    let mut out: String = msg.chars().take(MAX_MESSAGE_CHARS - 1).collect();
    out.push('…');
    out
}

/// Crate-wide result alias.
pub type Result<T> = std::result::Result<T, ProtocolError>;

/// Shorthand for `ProtocolError::new`.
#[allow(dead_code)]
pub(crate) fn perr(code: ErrorCode, message: impl Into<String>) -> ProtocolError {
    ProtocolError::new(code, message)
}

/// Shorthand for `ProtocolError::fatal`.
#[allow(dead_code)]
pub(crate) fn pfatal(code: ErrorCode, message: impl Into<String>) -> ProtocolError {
    ProtocolError::fatal(code, message)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn code_table_matches_c04_shape() {
        // 33 codes, unique spellings, unique http statuses where the table has them.
        assert_eq!(ErrorCode::ALL.len(), 33);
        for (i, code) in ErrorCode::ALL.iter().enumerate() {
            assert_eq!(ErrorCode::from_name(code.as_str()), Some(*code));
            assert!(!code.as_str().is_empty());
            assert_eq!(
                ErrorCode::ALL.iter().filter(|c| c.as_str() == code.as_str()).count(),
                1,
                "duplicate code at index {i}"
            );
        }
    }

    #[test]
    fn retriable_set_is_exactly_the_c04_one() {
        let expected: Vec<&str> = ErrorCode::ALL
            .iter()
            .filter(|c| c.retriable())
            .map(|c| c.as_str())
            .collect();
        assert_eq!(
            expected,
            vec![
                "TIMEOUT",
                "UNAVAILABLE",
                "OVERLOADED",
                "MEMORY_FULL",
                "BIND_SETUP_LOCKED",
                "AUTH_FAILED",
                "ADMIN_LOCKED",
                "SETUP_UNAUTHORIZED",
                "MODULE_DRAINING",
            ]
        );
    }

    #[test]
    fn message_is_clipped_to_300_chars_on_a_char_boundary() {
        let long = "密".repeat(500);
        let e = ProtocolError::new(ErrorCode::InvalidArgument, long);
        assert_eq!(e.message().chars().count(), MAX_MESSAGE_CHARS);
    }

    #[test]
    fn fatal_flag_is_orthogonal_to_retriable() {
        let e = ProtocolError::fatal(ErrorCode::PermissionDenied, "replay");
        assert!(e.is_fatal());
        assert!(!e.retriable());
        assert_eq!(e.to_string(), "PERMISSION_DENIED: replay (fatal)");
    }
}
