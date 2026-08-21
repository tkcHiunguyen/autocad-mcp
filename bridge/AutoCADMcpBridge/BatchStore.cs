using System.Collections.Concurrent;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

namespace AutoCADMcpBridge;

internal sealed class BatchStore
{
    private readonly ConcurrentDictionary<string, BatchRecord> _records = new();

    internal object Preview(JsonElement parameters)
    {
        var plan = RequiredPlan(parameters);
        ValidatePlan(plan);
        if (!BridgeCapabilities.Current().TryGetValue("batch.get_screenshot", out var canCaptureOutput) || !canCaptureOutput)
        {
            throw new BridgeFault(
                ErrorCodes.UnsupportedCapability,
                "A verified output-clone screenshot is unavailable, so mutation preview is blocked"
            );
        }
        var expectedDocumentId = OptionalString(plan, "document_id", string.Empty);
        var state = (Dictionary<string, object?>)AutoCADQueries.GetDrawingState(requireDocument: true);
        var actualDocumentId = ValueAsString(state, "document_id");
        if (string.IsNullOrWhiteSpace(expectedDocumentId) ||
            !string.Equals(expectedDocumentId, actualDocumentId, StringComparison.Ordinal))
        {
            throw new BridgeFault(ErrorCodes.DocumentNotResolved, "The plan is bound to a different drawing document", new Dictionary<string, object?>
            {
                ["expected_document_id"] = expectedDocumentId,
                ["actual_document_id"] = actualDocumentId,
            });
        }
        var sourcePath = ValueAsString(state, "absolute_path");
        var targetPath = Path.GetFullPath(RequiredString(plan, "target_path"));
        if (!string.IsNullOrWhiteSpace(sourcePath) &&
            string.Equals(Path.GetFullPath(sourcePath), targetPath, StringComparison.OrdinalIgnoreCase))
        {
            throw new BridgeFault(ErrorCodes.SourceImmutable, "Overlay output must not overwrite the source drawing", new Dictionary<string, object?>
            {
                ["source_path"] = sourcePath,
                ["target_path"] = targetPath,
            });
        }
        var fingerprint = CurrentFingerprint();
        var expected = RequiredString(plan, "before_fingerprint");
        if (!string.Equals(expected, fingerprint, StringComparison.OrdinalIgnoreCase))
        {
            throw new BridgeFault(ErrorCodes.VerificationFailed, "The drawing fingerprint changed before preview", new Dictionary<string, object?>
            {
                ["expected"] = expected,
                ["actual"] = fingerprint,
            });
        }
        RequirePersistedSource(plan, state);

        var idempotencyKey = OptionalString(plan, "idempotency_key", string.Empty);
        if (string.IsNullOrWhiteSpace(idempotencyKey))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "plan.idempotency_key is required");
        }
        var planHash = OptionalString(plan, "plan_hash", string.Empty);
        if (string.IsNullOrWhiteSpace(planHash))
        {
            planHash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(plan)))).ToLowerInvariant();
        }
        var batchId = Guid.NewGuid().ToString("N");
        var approvalToken = Convert.ToHexString(RandomNumberGenerator.GetBytes(24));
        var approvalExpiresAt = DateTimeOffset.UtcNow.AddMinutes(10);
        var preview = CreatePreviewClone(plan, batchId);
        object screenshot;
        try
        {
            screenshot = CaptureOutputCloneScreenshot(preview.Path);
        }
        catch
        {
            TryDeletePath(preview.Path);
            throw;
        }
        var beforeDbmod = RequiredInteger(state, "dbmod");
        var record = new BatchRecord(batchId, approvalToken, plan, fingerprint, beforeDbmod, planHash, idempotencyKey, approvalExpiresAt)
        {
            PreviewPath = preview.Path,
        };
        _records[batchId] = record;
        return new Dictionary<string, object?>
        {
            ["batch_id"] = batchId,
            ["approval_token"] = approvalToken,
            ["state"] = "previewed",
            ["before_fingerprint"] = fingerprint,
            ["before_dbmod"] = beforeDbmod,
            ["document_id"] = actualDocumentId,
            ["removed_handles"] = Array.Empty<string>(),
            ["actions"] = ActionSummaries(plan),
            ["source_to_overlay"] = preview.Build.SourceToOverlay,
            ["created_handles"] = preview.Build.CreatedHandles,
            ["preview_path"] = preview.Path,
            ["screenshot"] = screenshot,
            ["screenshot_scope"] = "output_clone",
            ["overlay_verification"] = preview.Verification,
            ["source_immutable"] = true,
            ["requires_approval"] = true,
            ["plan_hash"] = planHash,
            ["idempotency_key"] = idempotencyKey,
            ["approval_expires_at"] = approvalExpiresAt.ToUnixTimeSeconds(),
        };
    }

    internal object Apply(JsonElement parameters)
    {
        var batchId = RequiredString(parameters, "batch_id");
        var approvalToken = RequiredString(parameters, "approval_token");
        var idempotencyKey = OptionalString(parameters, "idempotency_key", string.Empty);
        if (!_records.TryGetValue(batchId, out var record))
        {
            throw new BridgeFault(ErrorCodes.TransactionFailed, "Unknown batch_id", new Dictionary<string, object?> { ["batch_id"] = batchId });
        }
        lock (record.SyncRoot)
        {
            if (!CryptographicOperations.FixedTimeEquals(
                    Encoding.UTF8.GetBytes(approvalToken),
                    Encoding.UTF8.GetBytes(record.ApprovalToken)))
            {
                throw new BridgeFault(ErrorCodes.ApprovalRequired, "The approval token is invalid", new Dictionary<string, object?> { ["batch_id"] = batchId });
            }
            if (!string.Equals(idempotencyKey, record.IdempotencyKey, StringComparison.Ordinal))
            {
                throw new BridgeFault(ErrorCodes.InvalidRequest, "The idempotency key is invalid", new Dictionary<string, object?> { ["batch_id"] = batchId });
            }
            if (record.State == "applied")
            {
                return record.Result!;
            }
            if (DateTimeOffset.UtcNow >= record.ApprovalExpiresAt)
            {
                throw new BridgeFault(ErrorCodes.ApprovalExpired, "The approval token has expired", new Dictionary<string, object?> { ["batch_id"] = batchId });
            }
            if (record.State != "previewed")
            {
                throw new BridgeFault(ErrorCodes.TransactionFailed, "Batch is not in previewed state", new Dictionary<string, object?> { ["state"] = record.State });
            }

            var currentState = (Dictionary<string, object?>)AutoCADQueries.GetDrawingState(requireDocument: true);
            var plannedDocumentId = RequiredString(record.Plan, "document_id");
            var currentDocumentId = ValueAsString(currentState, "document_id");
            if (!string.Equals(plannedDocumentId, currentDocumentId, StringComparison.Ordinal))
            {
                throw new BridgeFault(ErrorCodes.DocumentNotResolved, "The active drawing document changed after preview", new Dictionary<string, object?>
                {
                    ["expected_document_id"] = plannedDocumentId,
                    ["actual_document_id"] = currentDocumentId,
                });
            }
            RequirePersistedSource(record.Plan, currentState);
            var currentFingerprint = CurrentFingerprint();
            if (!string.Equals(record.BeforeFingerprint, currentFingerprint, StringComparison.OrdinalIgnoreCase))
            {
                throw new BridgeFault(ErrorCodes.VerificationFailed, "The drawing changed after preview; apply was refused", new Dictionary<string, object?>
                {
                    ["expected"] = record.BeforeFingerprint,
                    ["actual"] = currentFingerprint,
                });
            }

            var targetPath = RequiredString(record.Plan, "target_path");
            var output = ApplyToClone(record.Plan, targetPath, batchId);
            var previewExisted = !string.IsNullOrWhiteSpace(record.PreviewPath) && File.Exists(record.PreviewPath);
            var previewRemoved = previewExisted && TryDeletePath(record.PreviewPath);
            if (previewRemoved)
            {
                record.PreviewPath = null;
            }
            else if (!previewExisted)
            {
                record.PreviewPath = null;
            }
            var afterState = (Dictionary<string, object?>)AutoCADQueries.GetDrawingState(requireDocument: true);
            var afterDocumentId = ValueAsString(afterState, "document_id");
            var afterFingerprint = ValueAsString(afterState, "fingerprint");
            var afterDbmod = RequiredInteger(afterState, "dbmod");
            if (!string.Equals(plannedDocumentId, afterDocumentId, StringComparison.Ordinal) ||
                !string.Equals(record.BeforeFingerprint, afterFingerprint, StringComparison.OrdinalIgnoreCase) ||
                afterDbmod != record.BeforeDbmod)
            {
                var outputRemoved = TryDeleteOutput(output);
                throw new BridgeFault(ErrorCodes.SourceFingerprintChanged, "The source drawing identity changed during overlay apply", new Dictionary<string, object?>
                {
                    ["before_document_id"] = plannedDocumentId,
                    ["after_document_id"] = afterDocumentId,
                    ["before"] = record.BeforeFingerprint,
                    ["after"] = afterFingerprint,
                    ["before_dbmod"] = record.BeforeDbmod,
                    ["after_dbmod"] = afterDbmod,
                    ["output_removed"] = outputRemoved,
                });
            }
            output["source_unchanged"] = true;
            output["after_source_fingerprint"] = afterFingerprint;
            output["after_source_dbmod"] = afterDbmod;
            output["preview_removed"] = previewRemoved;
            record.State = "applied";
            record.Result = output;
            return output;
        }
    }

    internal object Rollback(JsonElement parameters)
    {
        var batchId = RequiredString(parameters, "batch_id");
        if (!_records.TryGetValue(batchId, out var record))
        {
            throw new BridgeFault(ErrorCodes.TransactionFailed, "Unknown batch_id", new Dictionary<string, object?> { ["batch_id"] = batchId });
        }
        var outputRemoved = false;
        var previewRemoved = false;
        lock (record.SyncRoot)
        {
            if (record.State == "applied")
            {
                var outputPath = OutputPath(record.Result);
                var outputExisted = !string.IsNullOrWhiteSpace(outputPath) && File.Exists(outputPath);
                if (outputExisted && !TryDeletePath(outputPath))
                {
                    throw new BridgeFault(ErrorCodes.TransactionFailed, "Unable to remove the output owned by this batch", new Dictionary<string, object?>
                    {
                        ["output_path"] = outputPath,
                    });
                }
                outputRemoved = outputExisted && !File.Exists(outputPath);
            }
            var previewExisted = !string.IsNullOrWhiteSpace(record.PreviewPath) && File.Exists(record.PreviewPath);
            if (previewExisted && !TryDeletePath(record.PreviewPath))
            {
                throw new BridgeFault(ErrorCodes.TransactionFailed, "Unable to remove the preview clone owned by this batch", new Dictionary<string, object?>
                {
                    ["preview_path"] = record.PreviewPath,
                });
            }
            previewRemoved = previewExisted && !File.Exists(record.PreviewPath);
            record.PreviewPath = null;
            record.State = "rolled_back";
        }
        return new Dictionary<string, object?>
        {
            ["batch_id"] = batchId,
            ["state"] = record.State,
            ["source_unchanged"] = true,
            ["output_path"] = OutputPath(record.Result),
            ["output_removed"] = outputRemoved,
            ["preview_removed"] = previewRemoved,
            ["note"] = "The source was never edited; rollback only reports files it actually removed.",
        };
    }

    internal object Status(JsonElement parameters)
    {
        var batchId = RequiredString(parameters, "batch_id");
        if (!_records.TryGetValue(batchId, out var record))
        {
            throw new BridgeFault(ErrorCodes.TransactionFailed, "Unknown batch_id", new Dictionary<string, object?> { ["batch_id"] = batchId });
        }
        return new Dictionary<string, object?>
        {
            ["batch_id"] = record.BatchId,
            ["state"] = record.State,
            ["before_fingerprint"] = record.BeforeFingerprint,
            ["before_dbmod"] = record.BeforeDbmod,
            ["plan_hash"] = record.PlanHash,
            ["idempotency_key"] = record.IdempotencyKey,
            ["approval_expires_at"] = record.ApprovalExpiresAt.ToUnixTimeSeconds(),
            ["result"] = record.Result,
        };
    }

    internal object GetScreenshot(JsonElement parameters)
    {
        _ = RequiredString(parameters, "batch_id");
        throw new BridgeFault(
            ErrorCodes.UnsupportedCapability,
            "The direct bridge cannot yet produce a verified screenshot of an output clone without opening or switching documents"
        );
    }

    private static object CaptureOutputCloneScreenshot(string previewPath)
    {
        _ = previewPath;
        // Keep this explicit until AutoCAD exposes a renderer that can inspect
        // a detached clone without opening or switching the active document.
        throw new BridgeFault(
            ErrorCodes.UnsupportedCapability,
            "A verified output-clone screenshot renderer is not implemented"
        );
    }

    private static Dictionary<string, object?> RequiredPlan(JsonElement parameters)
    {
        if (!parameters.TryGetProperty("plan", out var plan) || plan.ValueKind != JsonValueKind.Object)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "plan must be a JSON object");
        }
        return JsonSerializer.Deserialize<Dictionary<string, object?>>(plan.GetRawText())
            ?? throw new BridgeFault(ErrorCodes.InvalidRequest, "plan could not be decoded");
    }

    private static void ValidatePlan(Dictionary<string, object?> plan)
    {
        if (!plan.TryGetValue("removed_handles", out var removed) ||
            removed is not JsonElement removedElement ||
            removedElement.ValueKind != JsonValueKind.Array ||
            removedElement.GetArrayLength() > 0)
        {
            throw new BridgeFault(ErrorCodes.SourceImmutable, "removed_handles must be empty for the first overlay transaction");
        }
        var targetPath = RequiredString(plan, "target_path");
        if (!targetPath.EndsWith("_overlay.dwg", StringComparison.OrdinalIgnoreCase))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "target_path must end with _overlay.dwg");
        }
        if (OptionalBool(plan, "allow_overwrite", false))
        {
            throw new BridgeFault(ErrorCodes.SourceImmutable, "allow_overwrite is not permitted for immutable-source overlay batches");
        }
        if (!plan.TryGetValue("actions", out var actions) || actions is not JsonElement actionsElement || actionsElement.ValueKind != JsonValueKind.Array)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "plan.actions must be an array");
        }
        if (actionsElement.GetArrayLength() == 0)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "plan.actions must not be empty");
        }
        var actionSourceHandles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var action in actionsElement.EnumerateArray())
        {
            var kind = OptionalString(action, "action", string.Empty);
            if (kind is not ("preserve" or "copy_to_overlay" or "simplify_copy" or "create_connector_line"))
            {
                throw new BridgeFault(ErrorCodes.InvalidRequest, "Unsupported plan action", new Dictionary<string, object?> { ["action"] = kind });
            }
            if (kind is "preserve" or "copy_to_overlay" or "simplify_copy")
            {
                actionSourceHandles.Add(RequiredString(action, "source_handle"));
            }
            if (kind is "copy_to_overlay" or "simplify_copy")
            {
                _ = RequiredString(action, "target_layer");
            }
            if (kind == "simplify_copy")
            {
                if (!action.TryGetProperty("source_vertices", out var vertices) ||
                    vertices.ValueKind != JsonValueKind.Array || vertices.GetArrayLength() < 2)
                {
                    throw new BridgeFault(ErrorCodes.GeometryUnavailable, "simplify_copy requires source_vertices returned by geometry observation");
                }
                _ = RequiredIntArray(action, "vertex_indices");
            }
            if (kind == "create_connector_line")
            {
                _ = RequiredPoint(action, "start");
                _ = RequiredPoint(action, "end");
                _ = RequiredString(action, "target_layer");
                _ = RequiredString(action, "start_source_handle");
                _ = RequiredString(action, "end_source_handle");
                _ = RequiredInt(action, "start_vertex_index");
                _ = RequiredInt(action, "end_vertex_index");
            }
        }
        if (plan.TryGetValue("required_labels", out var requiredLabels) &&
            requiredLabels is JsonElement requiredLabelsElement)
        {
            if (requiredLabelsElement.ValueKind != JsonValueKind.Array)
            {
                throw new BridgeFault(ErrorCodes.InvalidRequest, "plan.required_labels must be an array");
            }
            foreach (var label in requiredLabelsElement.EnumerateArray())
            {
                if (label.ValueKind != JsonValueKind.Object)
                {
                    throw new BridgeFault(ErrorCodes.InvalidRequest, "Each required label must be an object");
                }
                var labelHandle = RequiredString(label, "source_handle");
                if (!actionSourceHandles.Contains(labelHandle))
                {
                    throw new BridgeFault(
                        ErrorCodes.SourceImmutable,
                        "Every required label must be represented by a preserve/copy action",
                        new Dictionary<string, object?> { ["source_handle"] = labelHandle }
                    );
                }
            }
        }
    }

    private Dictionary<string, object?> ApplyToClone(Dictionary<string, object?> plan, string targetPath, string batchId)
    {
        var sourceDocument = Autodesk.AutoCAD.ApplicationServices.Core.Application.DocumentManager.MdiActiveDocument
            ?? throw new BridgeFault(ErrorCodes.DocumentNotResolved, "No source drawing is available");
        EnsureSavedSource(sourceDocument);
        RequirePersistedSource(plan, (Dictionary<string, object?>)AutoCADQueries.GetDrawingState(requireDocument: true));
        var sourcePath = Path.GetFullPath(sourceDocument.Name);
        var destinationPath = Path.GetFullPath(targetPath);
        if (string.Equals(sourcePath, destinationPath, StringComparison.OrdinalIgnoreCase))
        {
            throw new BridgeFault(ErrorCodes.SourceImmutable, "Overlay output must not overwrite the source drawing");
        }
        if (File.Exists(destinationPath))
        {
            throw new BridgeFault(ErrorCodes.TransactionFailed, "Overlay output already exists", new Dictionary<string, object?> { ["target_path"] = destinationPath });
        }
        Directory.CreateDirectory(Path.GetDirectoryName(destinationPath)!);
        var temporaryPath = Path.Combine(
            Path.GetDirectoryName(destinationPath)!,
            $"{Path.GetFileNameWithoutExtension(destinationPath)}.mcp-tmp-{batchId}{Path.GetExtension(destinationPath)}"
        );
        var moved = false;
        var completed = false;
        try
        {
            OverlayBuild build;
            using (var clone = new Database(false, true))
            {
                clone.ReadDwgFile(sourcePath, FileOpenMode.OpenForReadAndAllShare, true, string.Empty);
                clone.CloseInput(true);
                build = BuildOverlay(clone, (JsonElement)plan["actions"]!);
                _ = VerifyOverlay(clone, build);
                clone.SaveAs(temporaryPath, DwgVersion.Current);
            }
            File.Move(temporaryPath, destinationPath);
            moved = true;
            var verification = VerifySavedOverlay(destinationPath, build);
            completed = true;

            return new Dictionary<string, object?>
            {
                ["batch_id"] = batchId,
                ["state"] = "applied",
                ["output_path"] = destinationPath,
                ["source_path"] = sourcePath,
                ["source_unchanged"] = true,
                ["source_to_overlay"] = build.SourceToOverlay,
                ["created_handles"] = build.CreatedHandles,
                ["removed_handles"] = Array.Empty<string>(),
                ["overlay_verification"] = verification,
                ["after_source_fingerprint"] = CurrentFingerprint(),
            };
        }
        finally
        {
            if (!completed)
            {
                TryDeletePath(temporaryPath);
                if (moved)
                {
                    TryDeletePath(destinationPath);
                }
            }
        }
    }

    private static PreviewClone CreatePreviewClone(Dictionary<string, object?> plan, string batchId)
    {
        var sourceDocument = Autodesk.AutoCAD.ApplicationServices.Core.Application.DocumentManager.MdiActiveDocument
            ?? throw new BridgeFault(ErrorCodes.DocumentNotResolved, "No source drawing is available");
        EnsureSavedSource(sourceDocument);
        RequirePersistedSource(plan, (Dictionary<string, object?>)AutoCADQueries.GetDrawingState(requireDocument: true));
        var targetPath = Path.GetFullPath(RequiredString(plan, "target_path"));
        var directory = Path.GetDirectoryName(targetPath)!;
        Directory.CreateDirectory(directory);
        var previewPath = Path.Combine(
            directory,
            $"{Path.GetFileNameWithoutExtension(targetPath)}.preview-{batchId}{Path.GetExtension(targetPath)}");
        if (File.Exists(previewPath))
        {
            throw new BridgeFault(ErrorCodes.TransactionFailed, "Overlay preview path already exists", new Dictionary<string, object?> { ["preview_path"] = previewPath });
        }
        var saved = false;
        try
        {
            OverlayBuild build;
            using (var clone = new Database(false, true))
            {
                clone.ReadDwgFile(sourceDocument.Name, FileOpenMode.OpenForReadAndAllShare, true, string.Empty);
                clone.CloseInput(true);
                build = BuildOverlay(clone, (JsonElement)plan["actions"]!);
                _ = VerifyOverlay(clone, build);
                clone.SaveAs(previewPath, DwgVersion.Current);
            }
            var verification = VerifySavedOverlay(previewPath, build);
            saved = true;
            return new PreviewClone(previewPath, build, verification);
        }
        finally
        {
            if (!saved)
            {
                TryDeletePath(previewPath);
            }
        }
    }

    private static bool TryDeleteOutput(Dictionary<string, object?>? result)
    {
        var path = OutputPath(result);
        if (string.IsNullOrWhiteSpace(path))
        {
            return false;
        }
        return File.Exists(path) && TryDeletePath(path) && !File.Exists(path);
    }

    private static bool TryDeletePath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return false;
        }
        try
        {
            if (!File.Exists(path))
            {
                return true;
            }
            File.Delete(path);
            return !File.Exists(path);
        }
        catch (IOException)
        {
            // Rollback remains source-safe even if an external process holds the clone.
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            // Reported by status; never touch the source drawing.
            return false;
        }
    }

    private static void EnsureLayer(Database database, Transaction transaction, string name)
    {
        var table = (LayerTable)transaction.GetObject(database.LayerTableId, OpenMode.ForRead);
        if (table.Has(name))
        {
            return;
        }
        table.UpgradeOpen();
        var record = new LayerTableRecord { Name = name };
        table.Add(record);
        transaction.AddNewlyCreatedDBObject(record, true);
    }

    private static Dictionary<string, object?> VerifyOverlay(Database database, OverlayBuild build)
    {
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        var missingCreatedHandles = build.CreatedHandles
            .Where(handle => !EntityExists(database, transaction, handle))
            .ToList();
        var missingMappedHandles = build.SourceToOverlay
            .Where(pair =>
                !EntityExists(database, transaction, pair.Key) ||
                !EntityExists(database, transaction, pair.Value))
            .Select(pair => pair.Key)
            .ToList();
        if (missingCreatedHandles.Count > 0 || missingMappedHandles.Count > 0)
        {
            throw new BridgeFault(ErrorCodes.VerificationFailed, "Overlay clone does not contain every planned source and output handle", new Dictionary<string, object?>
            {
                ["missing_created_handles"] = missingCreatedHandles,
                ["missing_mapped_source_handles"] = missingMappedHandles,
            });
        }
        return new Dictionary<string, object?>
        {
            ["created_handles_verified"] = true,
            ["source_to_overlay_verified"] = true,
            ["created_handle_count"] = build.CreatedHandles.Count,
        };
    }

    private static Dictionary<string, object?> VerifySavedOverlay(string path, OverlayBuild build)
    {
        using var output = new Database(false, true);
        output.ReadDwgFile(path, FileOpenMode.OpenForReadAndAllShare, true, string.Empty);
        output.CloseInput(true);
        var verification = VerifyOverlay(output, build);
        verification["scope"] = "output_clone";
        return verification;
    }

    private static bool EntityExists(Database database, Transaction transaction, string handle)
    {
        try
        {
            var objectId = database.GetObjectId(false, new Handle(ParseHandle(handle)), 0);
            return !objectId.IsNull && transaction.GetObject(objectId, OpenMode.ForRead) is Entity;
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            return false;
        }
    }

    private static OverlayBuild BuildOverlay(Database database, JsonElement actions)
    {
        var sourceToOverlay = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var createdHandles = new List<string>();
        using var transaction = database.TransactionManager.StartTransaction();
        foreach (var action in actions.EnumerateArray())
        {
            var kind = OptionalString(action, "action", string.Empty);
            if (kind == "preserve")
            {
                var sourceHandle = RequiredString(action, "source_handle");
                var sourceId = database.GetObjectId(false, new Handle(ParseHandle(sourceHandle)), 0);
                if (sourceId.IsNull || transaction.GetObject(sourceId, OpenMode.ForRead) is not Entity sourceEntity)
                {
                    throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Preserved source handle could not be resolved in overlay clone", new Dictionary<string, object?> { ["handle"] = sourceHandle });
                }
                // The source entity already exists unchanged in the output clone.
                sourceToOverlay[sourceHandle] = sourceEntity.Handle.ToString();
            }
            else if (kind is "copy_to_overlay" or "simplify_copy")
            {
                var sourceHandle = RequiredString(action, "source_handle");
                var targetLayer = RequiredString(action, "target_layer");
                var sourceId = database.GetObjectId(false, new Handle(ParseHandle(sourceHandle)), 0);
                if (sourceId.IsNull || transaction.GetObject(sourceId, OpenMode.ForRead) is not Entity sourceEntity)
                {
                    throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Source handle could not be resolved in overlay clone", new Dictionary<string, object?> { ["handle"] = sourceHandle });
                }
                EnsureLayer(database, transaction, targetLayer);
                Entity cloneEntity;
                if (kind == "simplify_copy")
                {
                    if (sourceEntity is not Polyline sourcePolyline || !IsLinear(sourcePolyline))
                    {
                        throw new BridgeFault(ErrorCodes.GeometryUnavailable, "simplify_copy requires a linear Polyline source", new Dictionary<string, object?> { ["handle"] = sourceHandle });
                    }
                    cloneEntity = BuildSimplifiedPolyline(action, sourcePolyline);
                }
                else
                {
                    cloneEntity = (Entity)sourceEntity.Clone();
                }
                cloneEntity.Layer = targetLayer;
                var owner = (BlockTableRecord)transaction.GetObject(sourceEntity.OwnerId, OpenMode.ForWrite);
                owner.AppendEntity(cloneEntity);
                transaction.AddNewlyCreatedDBObject(cloneEntity, true);
                var overlayHandle = cloneEntity.Handle.ToString();
                sourceToOverlay[sourceHandle] = overlayHandle;
                createdHandles.Add(overlayHandle);
            }
            else if (kind == "create_connector_line")
            {
                var start = ResolveVerifiedVertex(database, transaction, action, "start");
                var end = ResolveVerifiedVertex(database, transaction, action, "end");
                var targetLayer = RequiredString(action, "target_layer");
                EnsureLayer(database, transaction, targetLayer);
                var model = (BlockTableRecord)transaction.GetObject(database.CurrentSpaceId, OpenMode.ForWrite);
                var line = new Line(new Point3d(start[0], start[1], 0), new Point3d(end[0], end[1], 0)) { Layer = targetLayer };
                model.AppendEntity(line);
                transaction.AddNewlyCreatedDBObject(line, true);
                createdHandles.Add(line.Handle.ToString());
            }
        }
        transaction.Commit();
        return new OverlayBuild(sourceToOverlay, createdHandles);
    }

    private static bool IsLinear(Polyline polyline)
    {
        return Enumerable.Range(0, polyline.NumberOfVertices)
            .All(index => Math.Abs(polyline.GetBulgeAt(index)) < 1e-12);
    }

    private static Polyline BuildSimplifiedPolyline(JsonElement action, Polyline source)
    {
        var indices = RequiredIntArray(action, "vertex_indices");
        var simplified = new Polyline();
        for (var outputIndex = 0; outputIndex < indices.Count; outputIndex++)
        {
            var sourceIndex = indices[outputIndex];
            if (sourceIndex < 0 || sourceIndex >= source.NumberOfVertices)
            {
                throw new BridgeFault(ErrorCodes.GeometryUnavailable, "simplify_copy vertex index is outside source geometry", new Dictionary<string, object?>
                {
                    ["source_handle"] = source.Handle.ToString(),
                    ["vertex_index"] = sourceIndex,
                });
            }
            simplified.AddVertexAt(outputIndex, source.GetPoint2dAt(sourceIndex), 0.0, 0.0, 0.0);
        }
        var requestedClosed = OptionalBool(action, "closed", false);
        var exactSourceVertexOrder = indices.Count == source.NumberOfVertices &&
            indices.SequenceEqual(Enumerable.Range(0, source.NumberOfVertices));
        if (requestedClosed && (!source.Closed || !exactSourceVertexOrder))
        {
            throw new BridgeFault(ErrorCodes.GeometryUnavailable, "simplify_copy may close only the exact verified closed source vertex sequence", new Dictionary<string, object?>
            {
                ["source_handle"] = source.Handle.ToString(),
            });
        }
        simplified.Closed = requestedClosed;
        return simplified;
    }

    private static double[] ResolveVerifiedVertex(
        Database database,
        Transaction transaction,
        JsonElement action,
        string endpoint)
    {
        var sourceHandle = RequiredString(action, endpoint + "_source_handle");
        var vertexIndex = RequiredInt(action, endpoint + "_vertex_index");
        var objectId = database.GetObjectId(false, new Handle(ParseHandle(sourceHandle)), 0);
        if (objectId.IsNull || transaction.GetObject(objectId, OpenMode.ForRead) is not Entity entity)
        {
            throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Connector source handle could not be resolved", new Dictionary<string, object?> { ["handle"] = sourceHandle });
        }
        return entity switch
        {
            Polyline polyline when vertexIndex >= 0 && vertexIndex < polyline.NumberOfVertices =>
                new[] { polyline.GetPoint2dAt(vertexIndex).X, polyline.GetPoint2dAt(vertexIndex).Y },
            Line line when vertexIndex == 0 => new[] { line.StartPoint.X, line.StartPoint.Y },
            Line line when vertexIndex == 1 => new[] { line.EndPoint.X, line.EndPoint.Y },
            _ => throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Connector vertex is not available from source geometry", new Dictionary<string, object?>
            {
                ["handle"] = sourceHandle,
                ["vertex_index"] = vertexIndex,
            }),
        };
    }

    private static string CurrentFingerprint()
    {
        var fingerprint = (Dictionary<string, object?>)AutoCADQueries.GetFingerprint();
        return fingerprint["fingerprint"]?.ToString() ?? string.Empty;
    }

    private static void EnsureSavedSource(Autodesk.AutoCAD.ApplicationServices.Document sourceDocument)
    {
        if (string.IsNullOrWhiteSpace(sourceDocument.Name) || !File.Exists(sourceDocument.Name))
        {
            throw new BridgeFault(ErrorCodes.SourceImmutable, "The source drawing must be saved before creating an overlay file");
        }
    }

    private static void RequirePersistedSource(
        Dictionary<string, object?> plan,
        Dictionary<string, object?> state)
    {
        var expectedDbmod = RequiredInteger(plan, "before_dbmod");
        var actualDbmod = RequiredInteger(state, "dbmod");
        if (actualDbmod != expectedDbmod)
        {
            throw new BridgeFault(ErrorCodes.VerificationFailed, "The source DBMOD changed after observation", new Dictionary<string, object?>
            {
                ["expected_dbmod"] = expectedDbmod,
                ["actual_dbmod"] = actualDbmod,
            });
        }
        if (actualDbmod != 0)
        {
            throw new BridgeFault(ErrorCodes.SourceUnsaved, "The source drawing has unsaved changes and cannot be cloned safely", new Dictionary<string, object?>
            {
                ["dbmod"] = actualDbmod,
            });
        }
    }

    private static string ValueAsString(Dictionary<string, object?> value, string name)
    {
        return value.TryGetValue(name, out var raw) ? raw?.ToString() ?? string.Empty : string.Empty;
    }

    private static string RequiredString(Dictionary<string, object?> value, string name)
    {
        var result = ValueAsString(value, name);
        if (string.IsNullOrWhiteSpace(result))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} is required");
        }
        return result;
    }

    private static long RequiredInteger(Dictionary<string, object?> value, string name)
    {
        if (!value.TryGetValue(name, out var raw) || raw == null)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} is required");
        }
        try
        {
            if (raw is JsonElement element && element.ValueKind == JsonValueKind.Number && element.TryGetInt64(out var jsonNumber))
            {
                return jsonNumber;
            }
            return Convert.ToInt64(raw, CultureInfo.InvariantCulture);
        }
        catch (Exception exception) when (exception is FormatException or InvalidCastException or OverflowException)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} must be an integer");
        }
    }

    private static string? OutputPath(Dictionary<string, object?>? result)
    {
        return result == null ? null : ValueAsString(result, "output_path");
    }

    private static object[] ActionSummaries(Dictionary<string, object?> plan)
    {
        if (plan["actions"] is not JsonElement actions || actions.ValueKind != JsonValueKind.Array)
        {
            return Array.Empty<object>();
        }
        return actions.EnumerateArray().Select(action => new
        {
            action = OptionalString(action, "action", string.Empty),
            source_handle = OptionalString(action, "source_handle", string.Empty),
            target_layer = OptionalString(action, "target_layer", string.Empty),
        }).Cast<object>().ToArray();
    }

    private static long ParseHandle(string value)
    {
        if (!long.TryParse(value, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var handle))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "Handle is not hexadecimal", new Dictionary<string, object?> { ["handle"] = value });
        }
        return handle;
    }

    private static string RequiredString(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out var property) || property.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(property.GetString()))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} is required");
        }
        return property.GetString()!;
    }

    private static string OptionalString(Dictionary<string, object?> value, string name, string fallback)
    {
        return value.TryGetValue(name, out var raw) ? raw?.ToString() ?? fallback : fallback;
    }

    private static string OptionalString(JsonElement value, string name, string fallback)
    {
        return value.TryGetProperty(name, out var property) && property.ValueKind == JsonValueKind.String
            ? property.GetString() ?? fallback
            : fallback;
    }

    private static bool OptionalBool(Dictionary<string, object?> value, string name, bool fallback)
    {
        if (!value.TryGetValue(name, out var raw))
        {
            return fallback;
        }
        if (raw is JsonElement element && element.ValueKind is JsonValueKind.True or JsonValueKind.False)
        {
            return element.GetBoolean();
        }
        return bool.TryParse(raw?.ToString(), out var parsed) ? parsed : fallback;
    }

    private static bool OptionalBool(JsonElement value, string name, bool fallback)
    {
        return value.TryGetProperty(name, out var property) &&
               property.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? property.GetBoolean()
            : fallback;
    }

    private static double[] RequiredPoint(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out var property) || property.ValueKind != JsonValueKind.Array || property.GetArrayLength() < 2)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} must be [x,y]");
        }
        return new[] { property[0].GetDouble(), property[1].GetDouble() };
    }

    private static int RequiredInt(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out var property) || !property.TryGetInt32(out var number))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} must be an integer");
        }
        return number;
    }

    private static List<int> RequiredIntArray(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out var property) || property.ValueKind != JsonValueKind.Array)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} must be an array of integers");
        }
        var result = property.EnumerateArray().Select(item =>
        {
            if (!item.TryGetInt32(out var number))
            {
                throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} must be an array of integers");
            }
            return number;
        }).ToList();
        if (result.Count < 2)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} must contain at least two indices");
        }
        return result;
    }

    private sealed record OverlayBuild(
        Dictionary<string, string> SourceToOverlay,
        List<string> CreatedHandles
    );

    private sealed record PreviewClone(
        string Path,
        OverlayBuild Build,
        Dictionary<string, object?> Verification
    );

    private sealed class BatchRecord
    {
        internal BatchRecord(
            string batchId,
            string approvalToken,
            Dictionary<string, object?> plan,
            string beforeFingerprint,
            long beforeDbmod,
            string planHash,
            string idempotencyKey,
            DateTimeOffset approvalExpiresAt)
        {
            BatchId = batchId;
            ApprovalToken = approvalToken;
            Plan = plan;
            BeforeFingerprint = beforeFingerprint;
            BeforeDbmod = beforeDbmod;
            PlanHash = planHash;
            IdempotencyKey = idempotencyKey;
            ApprovalExpiresAt = approvalExpiresAt;
        }

        internal string BatchId { get; }
        internal string ApprovalToken { get; }
        internal Dictionary<string, object?> Plan { get; }
        internal string BeforeFingerprint { get; }
        internal long BeforeDbmod { get; }
        internal string PlanHash { get; }
        internal string IdempotencyKey { get; }
        internal DateTimeOffset ApprovalExpiresAt { get; }
        internal object SyncRoot { get; } = new();
        internal string State { get; set; } = "previewed";
        internal Dictionary<string, object?>? Result { get; set; }
        internal string? PreviewPath { get; set; }
    }
}
