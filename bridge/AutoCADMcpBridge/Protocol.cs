using System.Text.Json;
using System.Text.Json.Serialization;

namespace AutoCADMcpBridge;

internal sealed class BridgeRequest
{
    [JsonPropertyName("protocol_version")]
    public int ProtocolVersion { get; init; }
    [JsonPropertyName("kind")]
    public string? Kind { get; init; }
    [JsonPropertyName("request_id")]
    public string? RequestId { get; init; }
    [JsonPropertyName("session_id")]
    public string? SessionId { get; init; }
    [JsonPropertyName("document_id")]
    public string? DocumentId { get; init; }
    [JsonPropertyName("token")]
    public string? Token { get; init; }
    [JsonPropertyName("operation")]
    public string? Operation { get; init; }
    [JsonPropertyName("params")]
    public JsonElement Params { get; init; }
    [JsonPropertyName("deadline_ms")]
    public int DeadlineMs { get; init; }
}

internal sealed class BridgeResponse
{
    [JsonPropertyName("protocol_version")]
    public int ProtocolVersion { get; init; } = BridgeHost.ProtocolVersion;
    [JsonPropertyName("request_id")]
    public string? RequestId { get; set; }
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }
    [JsonPropertyName("payload")]
    public object? Payload { get; set; }
    [JsonPropertyName("error")]
    public BridgeError? Error { get; set; }
    [JsonPropertyName("session_id")]
    public string? SessionId { get; set; }
    [JsonPropertyName("document_id")]
    public string? DocumentId { get; set; }
    [JsonPropertyName("drawing_fingerprint")]
    public string? DrawingFingerprint { get; set; }
    [JsonPropertyName("capabilities")]
    public IReadOnlyDictionary<string, bool>? Capabilities { get; set; }
    [JsonPropertyName("warnings")]
    public IReadOnlyList<string> Warnings { get; set; } = Array.Empty<string>();

    public static BridgeResponse Success(string? requestId, object? payload) => new()
    {
        RequestId = requestId,
        Ok = true,
        Payload = payload,
    };

    public static BridgeResponse Failure(string? requestId, BridgeError error, object? payload = null) => new()
    {
        RequestId = requestId,
        Ok = false,
        Error = error,
        Payload = payload,
    };
}

internal sealed class BridgeError
{
    [JsonPropertyName("code")]
    public string Code { get; init; } = ErrorCodes.Unknown;
    [JsonPropertyName("message")]
    public string Message { get; init; } = "Unknown bridge error";
    [JsonPropertyName("details")]
    public IReadOnlyDictionary<string, object?> Details { get; init; } = new Dictionary<string, object?>();
}

internal sealed class BridgeFault : System.Exception
{
    public BridgeFault(string code, string message, IReadOnlyDictionary<string, object?>? details = null)
        : base(message)
    {
        Error = new BridgeError
        {
            Code = code,
            Message = message,
            Details = details ?? new Dictionary<string, object?>(),
        };
    }

    public BridgeError Error { get; }
}

internal static class ErrorCodes
{
    public const string AutoCADNotConnected = "AUTOCAD_NOT_CONNECTED";
    public const string DocumentNotResolved = "DOCUMENT_NOT_RESOLVED";
    public const string UnsupportedCapability = "UNSUPPORTED_CAPABILITY";
    public const string CapabilityUnavailable = "CAPABILITY_UNAVAILABLE";
    public const string GeometryUnavailable = "GEOMETRY_UNAVAILABLE";
    public const string GeometryInvalid = "GEOMETRY_INVALID";
    public const string RequestTimeout = "REQUEST_TIMEOUT";
    public const string BridgeAuthFailed = "BRIDGE_AUTH_FAILED";
    public const string TransactionFailed = "TRANSACTION_FAILED";
    public const string VerificationFailed = "VERIFICATION_FAILED";
    public const string ProtocolError = "PROTOCOL_ERROR";
    public const string InvalidRequest = "INVALID_REQUEST";
    public const string ApprovalRequired = "APPROVAL_REQUIRED";
    public const string ApprovalExpired = "APPROVAL_EXPIRED";
    public const string SourceImmutable = "SOURCE_IMMUTABLE";
    public const string SourceUnsaved = "SOURCE_UNSAVED";
    public const string SourceFingerprintChanged = "SOURCE_FINGERPRINT_CHANGED";
    public const string Cancelled = "CANCELLED";
    public const string Unknown = "UNKNOWN";
}
