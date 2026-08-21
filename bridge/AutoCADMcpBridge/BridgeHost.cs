using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Runtime;

namespace AutoCADMcpBridge;

public sealed class BridgeHost : IExtensionApplication
{
    internal const int ProtocolVersion = 1;
    private const int MaxFrameBytes = 8 * 1024 * 1024;
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = false,
    };

    private readonly ConcurrentQueue<PendingRequest> _pending = new();
    private readonly ConcurrentDictionary<string, PendingRequest> _pendingByRequestId = new();
    private readonly CancellationTokenSource _stopping = new();
    private readonly BatchStore _batches = new();
    private readonly string _token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
    private readonly string _sessionId = Guid.NewGuid().ToString("N");
    private string _discoveryPath = string.Empty;
    private TcpListener? _listener;
    private Task? _listenerTask;
    private int _draining;

    public void Initialize()
    {
        _listener = new TcpListener(IPAddress.Loopback, 0);
        _listener.Start();
        var endpoint = (IPEndPoint)_listener.LocalEndpoint;
        _discoveryPath = WriteDiscoveryRecord(endpoint.Port);
        Autodesk.AutoCAD.ApplicationServices.Core.Application.Idle += OnIdle;
        _listenerTask = Task.Run(() => AcceptLoopAsync(_stopping.Token));
        WriteMessage($"\nAutoCAD MCP direct bridge listening on 127.0.0.1:{endpoint.Port}.");
    }

    public void Terminate()
    {
        Autodesk.AutoCAD.ApplicationServices.Core.Application.Idle -= OnIdle;
        _stopping.Cancel();
        _listener?.Stop();
        while (_pending.TryDequeue(out var pending))
        {
            pending.Cancellation.Cancel();
            _pendingByRequestId.TryRemove(pending.Request.RequestId ?? string.Empty, out _);
            pending.Completion.TrySetResult(BridgeResponse.Failure(
                pending.Request.RequestId,
                new BridgeError
                {
                    Code = ErrorCodes.AutoCADNotConnected,
                    Message = "AutoCAD direct bridge is shutting down",
                }
            ));
        }
        TryDeleteDiscoveryRecord();
    }

    [CommandMethod("MCPBRIDGESTATUS", CommandFlags.Session)]
    public void PrintStatus()
    {
        var endpoint = _listener == null ? null : (IPEndPoint)_listener.LocalEndpoint;
        WriteMessage(endpoint == null
            ? "\nAutoCAD MCP direct bridge is not running."
            : $"\nAutoCAD MCP direct bridge: 127.0.0.1:{endpoint.Port}, discovery={_discoveryPath}");
    }

    private async Task AcceptLoopAsync(CancellationToken cancellationToken)
    {
        if (_listener == null)
        {
            return;
        }

        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                var client = await _listener.AcceptTcpClientAsync(cancellationToken).ConfigureAwait(false);
                _ = Task.Run(() => ServeClientAsync(client, cancellationToken), cancellationToken);
            }
        }
        catch (OperationCanceledException)
        {
            // Expected while AutoCAD unloads the plugin.
        }
        catch (ObjectDisposedException)
        {
            // Expected after listener shutdown.
        }
    }

    private async Task ServeClientAsync(TcpClient client, CancellationToken stoppingToken)
    {
        using var clientScope = client;
        using var stream = client.GetStream();
        using var reader = new StreamReader(stream, new UTF8Encoding(false), false, MaxFrameBytes, leaveOpen: true);
        using var writer = new StreamWriter(stream, new UTF8Encoding(false), MaxFrameBytes, leaveOpen: true)
        {
            AutoFlush = true,
        };
        while (!stoppingToken.IsCancellationRequested)
        {
            string? line;
            try
            {
                line = await reader.ReadLineAsync(stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            if (line == null)
            {
                return;
            }
            if (Encoding.UTF8.GetByteCount(line) > MaxFrameBytes)
            {
                await WriteAsync(writer, BridgeResponse.Failure(null, new BridgeError
                {
                    Code = ErrorCodes.ProtocolError,
                    Message = "Request exceeds the maximum frame size",
                })).ConfigureAwait(false);
                return;
            }

            BridgeRequest? request;
            try
            {
                request = JsonSerializer.Deserialize<BridgeRequest>(line, JsonOptions);
            }
            catch (JsonException exception)
            {
                await WriteAsync(writer, BridgeResponse.Failure(null, new BridgeError
                {
                    Code = ErrorCodes.ProtocolError,
                    Message = "Request is not valid JSON",
                    Details = new Dictionary<string, object?> { ["reason"] = exception.Message },
                })).ConfigureAwait(false);
                continue;
            }
            if (request == null)
            {
                continue;
            }

            var response = await QueueRequestAsync(request, _sessionId, stoppingToken).ConfigureAwait(false);
            // Envelope fields are populated on the AutoCAD Idle thread when a
            // request executes; transport-level failures retain null facts.
            await WriteAsync(writer, response).ConfigureAwait(false);
        }
    }

    private async Task<BridgeResponse> QueueRequestAsync(
        BridgeRequest request,
        string sessionId,
        CancellationToken stoppingToken)
    {
        if (request.ProtocolVersion != ProtocolVersion || request.Kind != "request")
        {
            return BridgeResponse.Failure(request.RequestId, new BridgeError
            {
                Code = ErrorCodes.ProtocolError,
                Message = "Unsupported direct bridge protocol request",
                Details = new Dictionary<string, object?>
                {
                    ["protocol_version"] = request.ProtocolVersion,
                    ["kind"] = request.Kind,
                },
            });
        }
        if (!CryptographicOperations.FixedTimeEquals(
                Encoding.UTF8.GetBytes(request.Token ?? string.Empty),
                Encoding.UTF8.GetBytes(_token)))
        {
            return BridgeResponse.Failure(request.RequestId, new BridgeError
            {
                Code = ErrorCodes.BridgeAuthFailed,
                Message = "Direct bridge authentication failed",
            });
        }
        if (!string.Equals(request.Operation, "session.handshake", StringComparison.Ordinal) &&
            !string.Equals(request.SessionId, sessionId, StringComparison.Ordinal))
        {
            return BridgeResponse.Failure(request.RequestId, new BridgeError
            {
                Code = ErrorCodes.ProtocolError,
                Message = "Direct bridge session_id is invalid",
            });
        }
        if (string.IsNullOrWhiteSpace(request.Operation))
        {
            return BridgeResponse.Failure(request.RequestId, new BridgeError
            {
                Code = ErrorCodes.InvalidRequest,
                Message = "operation is required",
            });
        }

        if (string.Equals(request.Operation, "request.cancel", StringComparison.Ordinal))
        {
            if (!request.Params.TryGetProperty("request_id", out var target) ||
                target.ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(target.GetString()))
            {
                return BridgeResponse.Failure(request.RequestId, new BridgeError
                {
                    Code = ErrorCodes.InvalidRequest,
                    Message = "request.cancel requires request_id",
                });
            }
            var targetId = target.GetString()!;
            var cancelled = _pendingByRequestId.TryGetValue(targetId, out var targetRequest);
            if (cancelled)
            {
                targetRequest!.Cancellation.Cancel();
            }
            return BridgeResponse.Success(request.RequestId, new
            {
                request_id = targetId,
                cancelled,
            });
        }

        var completion = new TaskCompletionSource<BridgeResponse>(TaskCreationOptions.RunContinuationsAsynchronously);
        var timeoutMs = Math.Clamp(request.DeadlineMs, 1000, 300000);
        var pending = new PendingRequest(
            request,
            sessionId,
            completion,
            CancellationTokenSource.CreateLinkedTokenSource(stoppingToken),
            DateTimeOffset.UtcNow.AddMilliseconds(timeoutMs));
        if (string.IsNullOrWhiteSpace(request.RequestId) ||
            !_pendingByRequestId.TryAdd(request.RequestId!, pending))
        {
            pending.Cancellation.Dispose();
            return BridgeResponse.Failure(request.RequestId, new BridgeError
            {
                Code = ErrorCodes.InvalidRequest,
                Message = "request_id must be unique and non-empty",
            });
        }
        _pending.Enqueue(pending);
        try
        {
            return await completion.Task.WaitAsync(
                TimeSpan.FromMilliseconds(timeoutMs), pending.Cancellation.Token).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            pending.Cancellation.Cancel();
            _pendingByRequestId.TryRemove(request.RequestId!, out _);
            return BridgeResponse.Failure(request.RequestId, new BridgeError
            {
                Code = ErrorCodes.RequestTimeout,
                Message = "Request did not reach the AutoCAD execution context before its deadline",
                Details = new Dictionary<string, object?> { ["deadline_ms"] = timeoutMs },
            });
        }
        catch (OperationCanceledException)
        {
            _pendingByRequestId.TryRemove(request.RequestId!, out _);
            return BridgeResponse.Failure(request.RequestId, new BridgeError
            {
                Code = ErrorCodes.Cancelled,
                Message = "Request was cancelled before AutoCAD execution completed",
            });
        }
    }

    private void OnIdle(object? sender, EventArgs args)
    {
        if (Interlocked.Exchange(ref _draining, 1) != 0)
        {
            return;
        }
        try
        {
            // Small bounded drain keeps normal AutoCAD interaction responsive.
            for (var index = 0; index < 4 && _pending.TryDequeue(out var pending); index++)
            {
                if (pending.Cancellation.IsCancellationRequested || DateTimeOffset.UtcNow >= pending.DeadlineUtc)
                {
                    _pendingByRequestId.TryRemove(pending.Request.RequestId ?? string.Empty, out _);
                    pending.Completion.TrySetResult(BridgeResponse.Failure(
                        pending.Request.RequestId,
                        new BridgeError
                        {
                            Code = ErrorCodes.RequestTimeout,
                            Message = "Request expired before reaching the AutoCAD execution context",
                        }));
                    pending.Cancellation.Dispose();
                    continue;
                }
                try
                {
                    var response = Dispatch(pending.Request, pending.SessionId);
                    AttachEnvelope(response, pending.SessionId);
                    pending.Completion.TrySetResult(response);
                }
                catch (BridgeFault fault)
                {
                    pending.Completion.TrySetResult(BridgeResponse.Failure(pending.Request.RequestId, fault.Error));
                }
                catch (System.Exception exception)
                {
                    pending.Completion.TrySetResult(BridgeResponse.Failure(pending.Request.RequestId, new BridgeError
                    {
                        Code = ErrorCodes.Unknown,
                        Message = "Unexpected error while executing in AutoCAD",
                        Details = new Dictionary<string, object?> { ["reason"] = exception.Message },
                    }));
                }
                finally
                {
                    _pendingByRequestId.TryRemove(pending.Request.RequestId ?? string.Empty, out _);
                    pending.Cancellation.Dispose();
                }
            }
        }
        finally
        {
            Volatile.Write(ref _draining, 0);
        }
    }

    private static void AttachEnvelope(BridgeResponse response, string sessionId)
    {
        response.SessionId = sessionId;
        response.Capabilities = BridgeCapabilities.Current();
        if (response.Payload is IDictionary<string, object?> payload)
        {
            response.DocumentId = payload.TryGetValue("document_id", out var documentId)
                ? documentId?.ToString()
                : null;
            response.DrawingFingerprint = payload.TryGetValue("fingerprint", out var fingerprint)
                ? fingerprint?.ToString()
                : payload.TryGetValue("database_fingerprint", out var databaseFingerprint)
                    ? databaseFingerprint?.ToString()
                    : null;
        }
    }

    private BridgeResponse Dispatch(BridgeRequest request, string sessionId)
    {
        var operation = request.Operation!;
        if (operation == "session.handshake")
        {
            return BridgeResponse.Success(request.RequestId, new
            {
                session_id = sessionId,
                document = AutoCADQueries.GetDrawingState(requireDocument: false),
                bridge = new { protocol_version = ProtocolVersion, transport = "loopback_tcp" },
                capabilities = BridgeCapabilities.Current(),
            });
        }
        if (operation == "session.health")
        {
            return BridgeResponse.Success(request.RequestId, new
            {
                connected = true,
                session_id = sessionId,
                document = AutoCADQueries.GetDrawingState(requireDocument: false),
            });
        }
        if (operation == "capabilities.list")
        {
            return BridgeResponse.Success(request.RequestId, new { capabilities = BridgeCapabilities.Current() });
        }

        var capabilities = BridgeCapabilities.Current();
        if (!capabilities.TryGetValue(operation, out var supported) || !supported)
        {
            throw new BridgeFault(
                ErrorCodes.UnsupportedCapability,
                "Operation is not available from the direct bridge",
                new Dictionary<string, object?> { ["operation"] = operation });
        }
        var document = AutoCADQueries.RequireDocument(request.DocumentId);
        using var documentLock = document.LockDocument();

        var payload = operation switch
        {
            "drawing.info" => AutoCADQueries.GetDrawingInfo(request.Params),
            "drawing.get_state" => AutoCADQueries.GetDrawingState(requireDocument: true),
            "drawing.get_fingerprint" => AutoCADQueries.GetFingerprint(),
            "drawing.get_variables" => AutoCADQueries.GetVariables(request.Params),
            "view.get_state" => AutoCADQueries.GetViewState(),
            "layer.list" => AutoCADQueries.ListLayers(),
            "entity.get" => AutoCADQueries.GetEntity(request.Params),
            "entity.get_geometry" => AutoCADQueries.GetGeometry(request.Params),
            "entity.search_text" => AutoCADQueries.SearchText(request.Params),
            "entity.search_text_batch" => AutoCADQueries.SearchTextBatch(request.Params),
            "entity.get_geometry_batch" => AutoCADQueries.GetGeometryBatch(request.Params),
            "entity.query" => AutoCADQueries.QueryEntities(request.Params),
            "entity.query_spatial" => AutoCADQueries.QuerySpatial(request.Params),
            "entity.count" => AutoCADQueries.CountEntities(request.Params),
            "entity.count_by_layer_type" => AutoCADQueries.CountByLayerType(request.Params),
            "batch.preview" => _batches.Preview(request.Params),
            "batch.apply" => _batches.Apply(request.Params),
            "batch.rollback" => _batches.Rollback(request.Params),
            "batch.status" => _batches.Status(request.Params),
            "batch.get_screenshot" => _batches.GetScreenshot(request.Params),
            "view.get_screenshot" => AutoCADQueries.GetScreenshot(request.Params),
            _ => throw new BridgeFault(
                ErrorCodes.UnsupportedCapability,
                "Operation is not available from the direct bridge",
                new Dictionary<string, object?> { ["operation"] = operation }),
        };
        return BridgeResponse.Success(request.RequestId, payload);
    }

    private string WriteDiscoveryRecord(int port)
    {
        var directory = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AutoCAD-MCP"
        );
        Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, "bridge.json");
        var temporaryPath = path + ".tmp";
        var record = JsonSerializer.Serialize(new
        {
            protocol_version = ProtocolVersion,
            host = "127.0.0.1",
            port,
            token = _token,
            pid = Environment.ProcessId,
            started_at_utc = DateTimeOffset.UtcNow,
        }, JsonOptions);
        File.WriteAllText(temporaryPath, record, new UTF8Encoding(false));
        File.Move(temporaryPath, path, true);
        return path;
    }

    private void TryDeleteDiscoveryRecord()
    {
        try
        {
            if (!string.IsNullOrWhiteSpace(_discoveryPath))
            {
                File.Delete(_discoveryPath);
            }
        }
        catch (IOException)
        {
            // A stale record is rejected by the socket connect check.
        }
    }

    private static Task WriteAsync(StreamWriter writer, BridgeResponse response)
    {
        return writer.WriteLineAsync(JsonSerializer.Serialize(response, JsonOptions));
    }

    private static void WriteMessage(string text)
    {
        Autodesk.AutoCAD.ApplicationServices.Core.Application.DocumentManager.MdiActiveDocument?
            .Editor.WriteMessage(text);
    }

    private sealed record PendingRequest(
        BridgeRequest Request,
        string SessionId,
        TaskCompletionSource<BridgeResponse> Completion,
        CancellationTokenSource Cancellation,
        DateTimeOffset DeadlineUtc
    );
}

internal static class BridgeCapabilities
{
    internal static IReadOnlyDictionary<string, bool> Current()
    {
        var activeDocumentScreenshot = Autodesk.AutoCAD.ApplicationServices.Core.Application.DocumentManager.MdiActiveDocument?
            .GetType().GetMethod("CapturePreviewImage", new[] { typeof(int), typeof(int) }) != null;
        // Capturing the active source document is not evidence for a detached
        // output clone. Mutation remains unavailable until a renderer can
        // inspect that clone without opening or switching documents.
        const bool outputCloneScreenshot = false;
        var verifiedOverlayMutation = outputCloneScreenshot;
        return new Dictionary<string, bool>
        {
            ["session.handshake"] = true,
            ["session.health"] = true,
            ["capabilities.list"] = true,
            ["request.cancel"] = true,
            ["drawing.info"] = true,
            ["drawing.get_state"] = true,
            ["drawing.get_fingerprint"] = true,
            ["drawing.get_variables"] = true,
            ["view.get_state"] = true,
            ["view.get_screenshot"] = activeDocumentScreenshot,
            ["layer.list"] = true,
            ["entity.get"] = true,
            ["entity.get_geometry"] = true,
            ["entity.search_text"] = true,
            ["entity.search_text_batch"] = true,
            ["entity.get_geometry_batch"] = true,
            ["entity.query"] = true,
            ["entity.query_spatial"] = true,
            ["entity.count"] = true,
            ["entity.count_by_layer_type"] = true,
            ["batch.preview"] = verifiedOverlayMutation,
            ["batch.apply"] = verifiedOverlayMutation,
            ["batch.rollback"] = true,
            ["batch.status"] = true,
            ["batch.get_screenshot"] = outputCloneScreenshot,
        };
    }
}
