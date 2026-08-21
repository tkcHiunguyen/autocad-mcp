using System.Globalization;
using System.Drawing.Imaging;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

namespace AutoCADMcpBridge;

internal static class AutoCADQueries
{
    internal static Document RequireDocument(string? expectedDocumentId = null)
    {
        var document = Autodesk.AutoCAD.ApplicationServices.Core.Application.DocumentManager.MdiActiveDocument;
        if (document == null)
        {
            throw new BridgeFault(ErrorCodes.DocumentNotResolved, "No AutoCAD drawing document is available");
        }
        if (!string.IsNullOrWhiteSpace(expectedDocumentId) &&
            !string.Equals(GetDocumentId(document), expectedDocumentId, StringComparison.Ordinal))
        {
            throw new BridgeFault(
                ErrorCodes.DocumentNotResolved,
                "The active AutoCAD document changed during this MCP session",
                new Dictionary<string, object?>
                {
                    ["expected_document_id"] = expectedDocumentId,
                    ["actual_document_id"] = GetDocumentId(document),
                });
        }
        return document;
    }

    internal static object GetDrawingState(bool requireDocument)
    {
        var document = Autodesk.AutoCAD.ApplicationServices.Core.Application.DocumentManager.MdiActiveDocument;
        if (document == null)
        {
            if (requireDocument)
            {
                throw new BridgeFault(ErrorCodes.DocumentNotResolved, "No AutoCAD drawing document is available");
            }
            return new Dictionary<string, object?> { ["document_id"] = null };
        }

        var database = document.Database;
        var dbmod = GetSystemVariable("DBMOD");
        var tilemode = GetSystemVariable("TILEMODE");
        var ctab = GetSystemVariable("CTAB")?.ToString() ?? string.Empty;
        var databaseFingerprint = database.FingerprintGuid ?? string.Empty;
        var databaseVersion = GetDatabaseVersionGuid(database);
        return new Dictionary<string, object?>
        {
            ["document_id"] = GetDocumentId(document),
            ["absolute_path"] = document.Name,
            ["drawing_name"] = Path.GetFileName(document.Name),
            ["active_space"] = System.Convert.ToInt32(tilemode ?? 1, CultureInfo.InvariantCulture) == 1 ? "Model" : ctab,
            ["units"] = GetSystemVariable("INSUNITS"),
            ["dbmod"] = dbmod,
            ["fingerprint"] = ComputeDrawingFingerprint(document.Name, databaseFingerprint, databaseVersion, dbmod),
            ["database_fingerprint"] = databaseFingerprint,
            ["database_version"] = databaseVersion,
            ["current_layer"] = GetSystemVariable("CLAYER"),
            ["ctab"] = ctab,
            ["tilemode"] = tilemode,
            ["extents"] = ExtentsToObject(database.Extmin, database.Extmax),
            ["viewport"] = TryGetViewState(),
        };
    }

    internal static object GetFingerprint()
    {
        var document = RequireDocument();
        var state = (Dictionary<string, object?>)GetDrawingState(true);
        return new Dictionary<string, object?>
        {
            ["document_id"] = state["document_id"],
            ["fingerprint"] = state["fingerprint"],
            ["database_fingerprint"] = state["database_fingerprint"],
            ["database_version"] = state["database_version"],
            ["dbmod"] = state["dbmod"],
            ["absolute_path"] = document.Name,
        };
    }

    internal static object GetDrawingInfo(JsonElement parameters)
    {
        var document = RequireDocument();
        var database = document.Database;
        var includeEntityCount = OptionalBool(parameters, "include_entity_count", false);
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        var modelSpace = (BlockTableRecord)transaction.GetObject(
            SymbolUtilityServices.GetBlockModelSpaceId(database), OpenMode.ForRead);
        var layers = ListLayers(transaction, database);
        return new Dictionary<string, object?>
        {
            ["document"] = GetDrawingState(true),
            ["entity_count"] = includeEntityCount ? modelSpace.Cast<ObjectId>().Count() : null,
            ["entity_count_included"] = includeEntityCount,
            ["layers"] = layers,
        };
    }

    internal static object GetVariables(JsonElement parameters)
    {
        var names = ReadStringArray(parameters, "names");
        var result = new Dictionary<string, object?>();
        foreach (var name in names)
        {
            var cleanName = name.TrimStart('$');
            result[cleanName] = GetSystemVariable(cleanName);
        }
        return result;
    }

    internal static object GetViewState()
    {
        var document = RequireDocument();
        using var view = document.Editor.GetCurrentView();
        var center = view.CenterPoint;
        var width = view.Width;
        var height = view.Height;
        var screenSize = GetSystemVariable("SCREENSIZE") is Point2d point ? point : default(Point2d?);
        return new Dictionary<string, object?>
        {
            ["view_center"] = new[] { center.X, center.Y },
            ["view_width"] = width,
            ["view_height"] = height,
            ["world_bounds"] = new Dictionary<string, double>
            {
                ["xmin"] = center.X - width / 2.0,
                ["ymin"] = center.Y - height / 2.0,
                ["xmax"] = center.X + width / 2.0,
                ["ymax"] = center.Y + height / 2.0,
            },
            ["screen_width"] = screenSize?.X ?? 0,
            ["screen_height"] = screenSize?.Y ?? 0,
            ["ctab"] = GetSystemVariable("CTAB"),
            ["tilemode"] = GetSystemVariable("TILEMODE"),
        };
    }

    private static object? TryGetViewState()
    {
        try
        {
            return GetViewState();
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            return null;
        }
    }

    internal static object GetScreenshot(JsonElement parameters)
    {
        var document = RequireDocument();
        var width = Math.Clamp(OptionalInt(parameters, "width", 1600), 64, 4096);
        var height = Math.Clamp(OptionalInt(parameters, "height", 1000), 64, 4096);
        var method = document.GetType().GetMethod("CapturePreviewImage", new[] { typeof(int), typeof(int) });
        if (method == null)
        {
            throw new BridgeFault(
                ErrorCodes.UnsupportedCapability,
                "This AutoCAD release does not expose Document.CapturePreviewImage"
            );
        }
        using var image = method.Invoke(document, new object[] { width, height }) as System.Drawing.Image;
        if (image == null)
        {
            throw new BridgeFault(ErrorCodes.UnsupportedCapability, "AutoCAD did not return a preview image");
        }
        using var buffer = new MemoryStream();
        image.Save(buffer, ImageFormat.Png);
        return new Dictionary<string, object?>
        {
            ["data"] = Convert.ToBase64String(buffer.ToArray()),
            ["metadata"] = new Dictionary<string, object?>
            {
                ["capture_mode"] = "document_preview",
                ["scope"] = "active_document",
                ["width"] = image.Width,
                ["height"] = image.Height,
                ["view_state"] = GetViewState(),
            },
        };
    }

    internal static object ListLayers()
    {
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        return new Dictionary<string, object?>
        {
            ["layers"] = ListLayers(transaction, document.Database),
        };
    }

    internal static object GetEntity(JsonElement parameters)
    {
        var entityId = RequiredString(parameters, "entity_id");
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var entity = OpenEntity(document.Database, transaction, entityId);
        return EntitySummary(entity, transaction);
    }

    internal static object GetGeometry(JsonElement parameters)
    {
        var entityId = RequiredString(parameters, "entity_id");
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var entity = OpenEntity(document.Database, transaction, entityId);
        return EntityGeometry(entity, transaction);
    }

    internal static object SearchText(JsonElement parameters)
    {
        var query = RequiredString(parameters, "query");
        var mode = OptionalString(parameters, "match_mode", "contains");
        var caseSensitive = OptionalBool(parameters, "case_sensitive", false);
        var limit = Math.Clamp(OptionalInt(parameters, "limit", 20), 1, 100);
        if (mode is not ("exact" or "contains"))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "match_mode must be exact or contains");
        }

        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var matches = new List<object>();
        foreach (var entity in EnumerateModelSpace(transaction, document.Database))
        {
            foreach (var textEntity in TextEntities(entity, transaction))
            {
                var text = TextValue(textEntity);
                var left = caseSensitive ? text : text.ToUpperInvariant();
                var right = caseSensitive ? query : query.ToUpperInvariant();
                var matched = mode == "exact" ? left == right : left.Contains(right, StringComparison.Ordinal);
                if (!matched)
                {
                    continue;
                }
                if (matches.Count >= limit)
                {
                    return new Dictionary<string, object?>
                    {
                        ["query"] = query,
                        ["match_mode"] = mode,
                        ["case_sensitive"] = caseSensitive,
                        ["matches"] = matches,
                        ["count"] = matches.Count,
                        ["truncated"] = true,
                    };
                }
                matches.Add(TextSummary(textEntity));
            }
        }
        return new Dictionary<string, object?>
        {
            ["query"] = query,
            ["match_mode"] = mode,
            ["case_sensitive"] = caseSensitive,
            ["matches"] = matches,
            ["count"] = matches.Count,
            ["truncated"] = false,
        };
    }

    internal static object SearchTextBatch(JsonElement parameters)
    {
        if (!parameters.TryGetProperty("queries", out var queries) || queries.ValueKind != JsonValueKind.Array)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "queries must be an array");
        }
        if (queries.GetArrayLength() > 32)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "queries is limited to 32 items");
        }
        var specs = new List<TextSearchSpec>();
        foreach (var query in queries.EnumerateArray())
        {
            var queryText = RequiredString(query, "query");
            var mode = OptionalString(query, "match_mode", "contains");
            if (mode is not ("exact" or "contains"))
            {
                throw new BridgeFault(ErrorCodes.InvalidRequest, "match_mode must be exact or contains");
            }
            specs.Add(new TextSearchSpec(
                queryText,
                mode,
                OptionalBool(query, "case_sensitive", false),
                Math.Clamp(OptionalInt(query, "limit", 20), 1, 100)));
        }

        var matches = specs.Select(_ => new List<object>()).ToList();
        var truncated = specs.Select(_ => false).ToList();
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        foreach (var entity in EnumerateModelSpace(transaction, document.Database))
        {
            foreach (var textEntity in TextEntities(entity, transaction))
            {
                var text = TextValue(textEntity);
                for (var index = 0; index < specs.Count; index++)
                {
                    var spec = specs[index];
                    var left = spec.CaseSensitive ? text : text.ToUpperInvariant();
                    var right = spec.CaseSensitive ? spec.Query : spec.Query.ToUpperInvariant();
                    var matched = spec.Mode == "exact" ? left == right : left.Contains(right, StringComparison.Ordinal);
                    if (!matched)
                    {
                        continue;
                    }
                    if (matches[index].Count >= spec.Limit)
                    {
                        truncated[index] = true;
                        continue;
                    }
                    matches[index].Add(TextSummary(textEntity));
                }
            }
        }
        var results = specs.Select((spec, index) => (object)new Dictionary<string, object?>
        {
            ["query"] = spec.Query,
            ["match_mode"] = spec.Mode,
            ["case_sensitive"] = spec.CaseSensitive,
            ["matches"] = matches[index],
            ["count"] = matches[index].Count,
            ["truncated"] = truncated[index],
        }).ToList();
        return new Dictionary<string, object?> { ["results"] = results };
    }

    private sealed record TextSearchSpec(string Query, string Mode, bool CaseSensitive, int Limit);

    internal static object GetGeometryBatch(JsonElement parameters)
    {
        var handles = ReadStringArray(parameters, "entity_ids");
        if (handles.Count == 0 || handles.Count > 200)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "entity_ids must contain between 1 and 200 handles");
        }
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var geometries = new List<object>();
        foreach (var handle in handles)
        {
            var entity = OpenEntity(document.Database, transaction, handle);
            geometries.Add(EntityGeometry(entity, transaction));
        }
        return new Dictionary<string, object?> { ["geometries"] = geometries };
    }

    internal static object QueryEntities(JsonElement parameters)
    {
        var layers = ReadStringArray(parameters, "layers").ToHashSet(StringComparer.OrdinalIgnoreCase);
        var layer = OptionalString(parameters, "layer", string.Empty);
        if (!string.IsNullOrWhiteSpace(layer))
        {
            layers.Add(layer);
        }
        var types = ReadStringArray(parameters, "types");
        var handles = ReadStringArray(parameters, "handles");
        var limit = Math.Clamp(OptionalInt(parameters, "limit", 100), 1, 1000);
        if (layers.Count == 0 && types.Count == 0 && handles.Count == 0)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "entity.query requires layer, types, or handles");
        }

        var handleSet = handles.ToHashSet(StringComparer.OrdinalIgnoreCase);
        var typeSet = types.ToHashSet(StringComparer.OrdinalIgnoreCase);
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var entities = new List<object>();
        var truncated = false;
        foreach (var entity in EnumerateModelSpace(transaction, document.Database))
        {
            if (layers.Count > 0 && !layers.Contains(entity.Layer))
            {
                continue;
            }
            if (typeSet.Count > 0 && !typeSet.Contains(entity.GetType().Name) && !typeSet.Contains(entity.GetRXClass().DxfName))
            {
                continue;
            }
            if (handleSet.Count > 0 && !handleSet.Contains(entity.Handle.ToString()))
            {
                continue;
            }
            if (entities.Count >= limit)
            {
                truncated = true;
                break;
            }
            entities.Add(EntitySummary(entity, transaction));
        }
        return new Dictionary<string, object?>
        {
            ["entities"] = entities,
            ["count"] = entities.Count,
            ["truncated"] = truncated,
        };
    }

    internal static object CountEntities(JsonElement parameters)
    {
        var layer = OptionalString(parameters, "layer", string.Empty);
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var count = EnumerateModelSpace(transaction, document.Database)
            .Count(entity => string.IsNullOrWhiteSpace(layer) || string.Equals(entity.Layer, layer, StringComparison.OrdinalIgnoreCase));
        return new Dictionary<string, object?> { ["count"] = count, ["layer"] = layer };
    }

    internal static object CountByLayerType(JsonElement parameters)
    {
        var layers = ReadStringArray(parameters, "layers").ToHashSet(StringComparer.OrdinalIgnoreCase);
        var types = ReadStringArray(parameters, "types").ToHashSet(StringComparer.OrdinalIgnoreCase);
        if (layers.Count == 0 && types.Count == 0)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "count_by_layer_type requires layers or types");
        }
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var counts = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        foreach (var entity in EnumerateModelSpace(transaction, document.Database))
        {
            if (layers.Count > 0 && !layers.Contains(entity.Layer))
            {
                continue;
            }
            var type = entity.GetRXClass().DxfName;
            if (types.Count > 0 && !types.Contains(type))
            {
                continue;
            }
            var key = $"{entity.Layer}|{type}";
            counts[key] = counts.GetValueOrDefault(key) + 1;
        }
        return new Dictionary<string, object?> { ["counts"] = counts };
    }

    internal static object QuerySpatial(JsonElement parameters)
    {
        var operation = RequiredString(parameters, "operation");
        return operation switch
        {
            "point_in_polygon" => PointInPolygon(parameters),
            "nearest_boundary" => NearestBoundary(parameters),
            "intersection" => Intersection(parameters),
            "containment" => Containment(parameters),
            "overlap" => Overlap(parameters),
            "distance" => Distance(parameters),
            _ => throw new BridgeFault(ErrorCodes.InvalidRequest, "Unsupported spatial operation", new Dictionary<string, object?> { ["operation"] = operation }),
        };
    }

    private static object PointInPolygon(JsonElement parameters)
    {
        var handle = RequiredString(parameters, "boundary_handle");
        var point = RequiredPoint(parameters, "point");
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var entity = OpenEntity(document.Database, transaction, handle);
        if (entity is not Polyline polyline || !polyline.Closed || !IsLinearPolyline(polyline))
        {
            throw new BridgeFault(ErrorCodes.GeometryUnavailable, "point_in_polygon requires a closed linear Polyline", new Dictionary<string, object?> { ["handle"] = handle });
        }
        var vertices = Enumerable.Range(0, polyline.NumberOfVertices)
            .Select(polyline.GetPoint2dAt)
            .ToList();
        var inside = false;
        for (var i = 0; i < vertices.Count; i++)
        {
            var a = vertices[i];
            var b = vertices[(i + 1) % vertices.Count];
            if (((a.Y > point.Y) != (b.Y > point.Y)) &&
                (point.X < (b.X - a.X) * (point.Y - a.Y) / (b.Y - a.Y) + a.X))
            {
                inside = !inside;
            }
        }
        return new Dictionary<string, object?> { ["boundary_handle"] = handle, ["point"] = new[] { point.X, point.Y }, ["inside"] = inside };
    }

    private static object NearestBoundary(JsonElement parameters)
    {
        var point = RequiredPoint(parameters, "point");
        var handles = ReadStringArray(parameters, "candidate_handles");
        if (handles.Count == 0)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "nearest_boundary requires candidate_handles");
        }
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var results = new List<(string Handle, double Distance)>();
        foreach (var handle in handles)
        {
            var entity = OpenEntity(document.Database, transaction, handle);
            var distance = DistanceToEntity(entity, point);
            results.Add((handle, distance));
        }
        return new Dictionary<string, object?>
        {
            ["point"] = new[] { point.X, point.Y },
            ["matches"] = results.OrderBy(item => item.Distance).Select(item => new { handle = item.Handle, distance = item.Distance }).ToList(),
        };
    }

    private static object Intersection(JsonElement parameters)
    {
        var (first, second, transaction, database) = OpenPair(parameters);
        using (transaction)
        {
            var points = new Point3dCollection();
            first.IntersectWith(second, Intersect.OnBothOperands, points, IntPtr.Zero, IntPtr.Zero);
            return new Dictionary<string, object?>
            {
                ["intersects"] = points.Count > 0,
                ["points"] = points.Cast<Point3d>().Select(point => new[] { point.X, point.Y, point.Z }).ToList(),
            };
        }
    }

    private static object Containment(JsonElement parameters)
    {
        var container = RequiredString(parameters, "container_handle");
        var contained = RequiredString(parameters, "contained_handle");
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var outer = OpenEntity(document.Database, transaction, container) as Polyline;
        var inner = OpenEntity(document.Database, transaction, contained) as Polyline;
        if (outer == null || inner == null || !outer.Closed || !inner.Closed || !IsLinearPolyline(outer) || !IsLinearPolyline(inner))
        {
            throw new BridgeFault(ErrorCodes.GeometryUnavailable, "containment requires two closed linear Polyline entities");
        }
        var vertices = Enumerable.Range(0, inner.NumberOfVertices).Select(inner.GetPoint2dAt).ToList();
        var allInside = vertices.All(point => IsInside(outer, point));
        return new Dictionary<string, object?> { ["container_handle"] = container, ["contained_handle"] = contained, ["contains"] = allInside };
    }

    private static object Overlap(JsonElement parameters)
    {
        var (first, second, transaction, database) = OpenPair(parameters);
        using (transaction)
        {
            var points = new Point3dCollection();
            first.IntersectWith(second, Intersect.OnBothOperands, points, IntPtr.Zero, IntPtr.Zero);
            var boundsOverlap = first.GeometricExtents.MinPoint.X <= second.GeometricExtents.MaxPoint.X &&
                                first.GeometricExtents.MaxPoint.X >= second.GeometricExtents.MinPoint.X &&
                                first.GeometricExtents.MinPoint.Y <= second.GeometricExtents.MaxPoint.Y &&
                                first.GeometricExtents.MaxPoint.Y >= second.GeometricExtents.MinPoint.Y;
            var firstContainsSecond = false;
            var secondContainsFirst = false;
            if (first is Polyline firstPolyline && second is Polyline secondPolyline &&
                firstPolyline.Closed && secondPolyline.Closed &&
                IsLinearPolyline(firstPolyline) && IsLinearPolyline(secondPolyline))
            {
                var firstVertices = Enumerable.Range(0, firstPolyline.NumberOfVertices)
                    .Select(firstPolyline.GetPoint2dAt)
                    .ToList();
                var secondVertices = Enumerable.Range(0, secondPolyline.NumberOfVertices)
                    .Select(secondPolyline.GetPoint2dAt)
                    .ToList();
                firstContainsSecond = secondVertices.All(point => IsInsideOrOn(firstPolyline, point));
                secondContainsFirst = firstVertices.All(point => IsInsideOrOn(secondPolyline, point));
            }
            return new Dictionary<string, object?>
            {
                ["overlap"] = points.Count > 0 || firstContainsSecond || secondContainsFirst,
                ["intersection_points"] = points.Count,
                ["bounds_overlap"] = boundsOverlap,
                ["first_contains_second"] = firstContainsSecond,
                ["second_contains_first"] = secondContainsFirst,
            };
        }
    }

    private static object Distance(JsonElement parameters)
    {
        if (parameters.TryGetProperty("first_handle", out _) || parameters.TryGetProperty("second_handle", out _))
        {
            var firstHandle = RequiredString(parameters, "first_handle");
            var secondHandle = RequiredString(parameters, "second_handle");
            var (first, second, pairTransaction, _) = OpenPair(parameters);
            using (pairTransaction)
            {
                return new Dictionary<string, object?>
                {
                    ["first_handle"] = firstHandle,
                    ["second_handle"] = secondHandle,
                    ["distance"] = DistanceBetweenEntities(first, second),
                };
            }
        }
        var point = RequiredPoint(parameters, "point");
        var handle = RequiredString(parameters, "entity_handle");
        var document = RequireDocument();
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        var entity = OpenEntity(document.Database, transaction, handle);
        return new Dictionary<string, object?> { ["entity_handle"] = handle, ["distance"] = DistanceToEntity(entity, point) };
    }

    private static double DistanceBetweenEntities(Entity first, Entity second)
    {
        var firstSegments = LinearSegments(first);
        var secondSegments = LinearSegments(second);
        var best = double.PositiveInfinity;
        foreach (var firstSegment in firstSegments)
        {
            foreach (var secondSegment in secondSegments)
            {
                if (SegmentsIntersect(firstSegment.Start, firstSegment.End, secondSegment.Start, secondSegment.End))
                {
                    return 0.0;
                }
                best = Math.Min(best, DistanceToSegment(firstSegment.Start, secondSegment.Start, secondSegment.End));
                best = Math.Min(best, DistanceToSegment(firstSegment.End, secondSegment.Start, secondSegment.End));
                best = Math.Min(best, DistanceToSegment(secondSegment.Start, firstSegment.Start, firstSegment.End));
                best = Math.Min(best, DistanceToSegment(secondSegment.End, firstSegment.Start, firstSegment.End));
            }
        }
        return best;
    }

    private static List<(Point2d Start, Point2d End)> LinearSegments(Entity entity)
    {
        if (entity is Line line)
        {
            return new List<(Point2d, Point2d)>
            {
                (new Point2d(line.StartPoint.X, line.StartPoint.Y), new Point2d(line.EndPoint.X, line.EndPoint.Y)),
            };
        }
        if (entity is not Polyline polyline || !IsLinearPolyline(polyline))
        {
            throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Distance between entities requires Line or linear Polyline geometry");
        }
        var segmentCount = polyline.NumberOfVertices - (polyline.Closed ? 0 : 1);
        if (segmentCount <= 0)
        {
            throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Distance requires at least one verified segment");
        }
        var segments = new List<(Point2d, Point2d)>();
        for (var index = 0; index < segmentCount; index++)
        {
            segments.Add((polyline.GetPoint2dAt(index), polyline.GetPoint2dAt((index + 1) % polyline.NumberOfVertices)));
        }
        return segments;
    }

    private static bool SegmentsIntersect(Point2d firstStart, Point2d firstEnd, Point2d secondStart, Point2d secondEnd)
    {
        static double Orientation(Point2d start, Point2d end, Point2d point) =>
            (end.X - start.X) * (point.Y - start.Y) - (end.Y - start.Y) * (point.X - start.X);
        static bool OnSegment(Point2d start, Point2d end, Point2d point) =>
            point.X >= Math.Min(start.X, end.X) - 1e-9 && point.X <= Math.Max(start.X, end.X) + 1e-9 &&
            point.Y >= Math.Min(start.Y, end.Y) - 1e-9 && point.Y <= Math.Max(start.Y, end.Y) + 1e-9;

        var firstA = Orientation(firstStart, firstEnd, secondStart);
        var firstB = Orientation(firstStart, firstEnd, secondEnd);
        var secondA = Orientation(secondStart, secondEnd, firstStart);
        var secondB = Orientation(secondStart, secondEnd, firstEnd);
        if ((firstA > 0) != (firstB > 0) && (secondA > 0) != (secondB > 0))
        {
            return true;
        }
        return (Math.Abs(firstA) <= 1e-9 && OnSegment(firstStart, firstEnd, secondStart)) ||
               (Math.Abs(firstB) <= 1e-9 && OnSegment(firstStart, firstEnd, secondEnd)) ||
               (Math.Abs(secondA) <= 1e-9 && OnSegment(secondStart, secondEnd, firstStart)) ||
               (Math.Abs(secondB) <= 1e-9 && OnSegment(secondStart, secondEnd, firstEnd));
    }

    private static (Entity First, Entity Second, OpenCloseTransaction Transaction, Database Database) OpenPair(JsonElement parameters)
    {
        var firstHandle = RequiredString(parameters, "first_handle");
        var secondHandle = RequiredString(parameters, "second_handle");
        var document = RequireDocument();
        var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        try
        {
            return (OpenEntity(document.Database, transaction, firstHandle), OpenEntity(document.Database, transaction, secondHandle), transaction, document.Database);
        }
        catch
        {
            transaction.Dispose();
            throw;
        }
    }

    private static List<object> ListLayers(OpenCloseTransaction transaction, Database database)
    {
        var table = (LayerTable)transaction.GetObject(database.LayerTableId, OpenMode.ForRead);
        var layers = new List<object>();
        foreach (ObjectId id in table)
        {
            var layer = (LayerTableRecord)transaction.GetObject(id, OpenMode.ForRead);
            layers.Add(new
            {
                name = layer.Name,
                color = layer.Color.ColorIndex,
                is_frozen = layer.IsFrozen,
                is_locked = layer.IsLocked,
            });
        }
        return layers;
    }

    private static IEnumerable<Entity> EnumerateModelSpace(OpenCloseTransaction transaction, Database database)
    {
        var modelSpace = (BlockTableRecord)transaction.GetObject(
            database.CurrentSpaceId, OpenMode.ForRead);
        foreach (ObjectId id in modelSpace)
        {
            if (transaction.GetObject(id, OpenMode.ForRead) is Entity entity)
            {
                yield return entity;
            }
        }
    }

    private static Entity OpenEntity(Database database, OpenCloseTransaction transaction, string handleText)
    {
        if (!long.TryParse(handleText, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var numericHandle))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, "Entity handle is not hexadecimal", new Dictionary<string, object?> { ["handle"] = handleText });
        }
        var objectId = database.GetObjectId(false, new Handle(numericHandle), 0);
        if (objectId.IsNull || transaction.GetObject(objectId, OpenMode.ForRead) is not Entity entity)
        {
            throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Entity handle could not be resolved", new Dictionary<string, object?> { ["handle"] = handleText });
        }
        return entity;
    }

    private static object EntitySummary(Entity entity, OpenCloseTransaction transaction)
    {
        var extents = TryExtents(entity);
        return new Dictionary<string, object?>
        {
            ["handle"] = entity.Handle.ToString(),
            ["type"] = entity.GetRXClass().DxfName,
            ["layer"] = entity.Layer,
            ["bounds"] = extents,
        };
    }

    private static object EntityGeometry(Entity entity, OpenCloseTransaction transaction)
    {
        var result = new Dictionary<string, object?>
        {
            ["handle"] = entity.Handle.ToString(),
            ["type"] = entity.GetRXClass().DxfName,
            ["layer"] = entity.Layer,
            ["vertices"] = null,
            ["segments"] = null,
            ["closed"] = null,
            ["bounds"] = TryExtents(entity),
            ["insertion_point"] = null,
            ["text_bounds"] = null,
            ["block_name"] = null,
            ["block_reference"] = null,
            ["transform"] = null,
            ["source_document_id"] = GetDocumentId(RequireDocument()),
        };
        switch (entity)
        {
            case Polyline polyline:
                var vertices = Enumerable.Range(0, polyline.NumberOfVertices)
                    .Select(index => new[] { polyline.GetPoint2dAt(index).X, polyline.GetPoint2dAt(index).Y })
                    .ToList();
                result["vertices"] = vertices;
                result["segments"] = PolylineSegments(polyline);
                result["closed"] = polyline.Closed;
                break;
            case Line line:
                result["vertices"] = new[] { new[] { line.StartPoint.X, line.StartPoint.Y }, new[] { line.EndPoint.X, line.EndPoint.Y } };
                result["segments"] = new[]
                {
                    new Dictionary<string, object?>
                    {
                        ["type"] = "line",
                        ["start_vertex_index"] = 0,
                        ["end_vertex_index"] = 1,
                        ["bulge"] = 0.0,
                    },
                };
                result["closed"] = false;
                break;
            case AttributeReference attribute:
                result["insertion_point"] = new[] { attribute.Position.X, attribute.Position.Y };
                result["text_bounds"] = TryExtents(attribute);
                result["text"] = attribute.TextString;
                break;
            case DBText text:
                result["insertion_point"] = new[] { text.Position.X, text.Position.Y };
                result["text_bounds"] = TryExtents(text);
                result["text"] = text.TextString;
                break;
            case MText mtext:
                result["insertion_point"] = new[] { mtext.Location.X, mtext.Location.Y };
                result["text_bounds"] = TryExtents(mtext);
                result["text"] = mtext.Text;
                break;
            case BlockReference block:
                result["insertion_point"] = new[] { block.Position.X, block.Position.Y };
                var blockRecord = (BlockTableRecord)transaction.GetObject(block.BlockTableRecord, OpenMode.ForRead);
                result["block_name"] = blockRecord.Name;
                result["block_reference"] = block.Handle.ToString();
                result["transform"] = MatrixToArray(block.BlockTransform);
                break;
            case Circle circle:
                result["center"] = new[] { circle.Center.X, circle.Center.Y };
                result["radius"] = circle.Radius;
                break;
            case Arc arc:
                result["center"] = new[] { arc.Center.X, arc.Center.Y };
                result["radius"] = arc.Radius;
                result["start_angle"] = arc.StartAngle;
                result["end_angle"] = arc.EndAngle;
                break;
        }
        return result;
    }

    private static List<object> PolylineSegments(Polyline polyline)
    {
        var segmentCount = polyline.NumberOfVertices - (polyline.Closed ? 0 : 1);
        var segments = new List<object>();
        for (var index = 0; index < Math.Max(segmentCount, 0); index++)
        {
            var bulge = polyline.GetBulgeAt(index);
            segments.Add(new Dictionary<string, object?>
            {
                ["type"] = Math.Abs(bulge) < 1e-12 ? "line" : "arc",
                ["start_vertex_index"] = index,
                ["end_vertex_index"] = (index + 1) % polyline.NumberOfVertices,
                ["bulge"] = bulge,
            });
        }
        return segments;
    }

    private static object? MatrixToArray(Matrix3d matrix)
    {
        // Reflection avoids promising a Matrix3d conversion API on every
        // AutoCAD release while still exposing the real block transform.
        var method = typeof(Matrix3d).GetMethod("ToArray", Type.EmptyTypes);
        return method?.Invoke(matrix, null) as double[];
    }

    private static IEnumerable<Entity> TextEntities(Entity entity, OpenCloseTransaction transaction)
    {
        if (entity is DBText or MText or AttributeReference)
        {
            yield return entity;
        }
        if (entity is BlockReference block)
        {
            foreach (ObjectId id in block.AttributeCollection)
            {
                if (transaction.GetObject(id, OpenMode.ForRead) is AttributeReference attribute)
                {
                    yield return attribute;
                }
            }
        }
    }

    private static string TextValue(Entity entity) => entity switch
    {
        AttributeReference attribute => attribute.TextString,
        DBText text => text.TextString,
        MText mtext => mtext.Text,
        _ => string.Empty,
    };

    private static object TextSummary(Entity entity)
    {
        var insertion = entity switch
        {
            AttributeReference attribute => attribute.Position,
            DBText text => text.Position,
            MText mtext => mtext.Location,
            _ => Point3d.Origin,
        };
        return new Dictionary<string, object?>
        {
            ["handle"] = entity.Handle.ToString(),
            ["type"] = entity.GetRXClass().DxfName,
            ["text"] = TextValue(entity),
            ["layer"] = entity.Layer,
            ["insertion"] = new[] { insertion.X, insertion.Y },
            ["bounds"] = TryExtents(entity),
            ["text_bounds"] = TryExtents(entity),
        };
    }

    private static double DistanceToEntity(Entity entity, Point2d point)
    {
        if (entity is Line line)
        {
            return DistanceToSegment(point, new Point2d(line.StartPoint.X, line.StartPoint.Y), new Point2d(line.EndPoint.X, line.EndPoint.Y));
        }
        if (entity is Polyline polyline)
        {
            if (!IsLinearPolyline(polyline))
            {
                throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Distance is unavailable for curved Polyline geometry");
            }
            var best = double.PositiveInfinity;
            for (var index = 0; index < polyline.NumberOfVertices - (polyline.Closed ? 0 : 1); index++)
            {
                var next = (index + 1) % polyline.NumberOfVertices;
                best = Math.Min(best, DistanceToSegment(point, polyline.GetPoint2dAt(index), polyline.GetPoint2dAt(next)));
            }
            return best;
        }
        throw new BridgeFault(ErrorCodes.GeometryUnavailable, "Distance is supported only for Line and Polyline entities");
    }

    private static double DistanceToSegment(Point2d point, Point2d start, Point2d end)
    {
        var dx = end.X - start.X;
        var dy = end.Y - start.Y;
        var lengthSquared = dx * dx + dy * dy;
        if (lengthSquared == 0)
        {
            return point.GetDistanceTo(start);
        }
        var t = Math.Clamp(((point.X - start.X) * dx + (point.Y - start.Y) * dy) / lengthSquared, 0, 1);
        return point.GetDistanceTo(new Point2d(start.X + t * dx, start.Y + t * dy));
    }

    private static bool IsInside(Polyline polyline, Point2d point)
    {
        var vertices = Enumerable.Range(0, polyline.NumberOfVertices).Select(polyline.GetPoint2dAt).ToList();
        var inside = false;
        for (var index = 0; index < vertices.Count; index++)
        {
            var a = vertices[index];
            var b = vertices[(index + 1) % vertices.Count];
            if (((a.Y > point.Y) != (b.Y > point.Y)) &&
                point.X < (b.X - a.X) * (point.Y - a.Y) / (b.Y - a.Y) + a.X)
            {
                inside = !inside;
            }
        }
        return inside;
    }

    private static bool IsInsideOrOn(Polyline polyline, Point2d point)
    {
        return IsInside(polyline, point) || DistanceToEntity(polyline, point) <= 1e-9;
    }

    private static bool IsLinearPolyline(Polyline polyline)
    {
        return Enumerable.Range(0, polyline.NumberOfVertices)
            .All(index => Math.Abs(polyline.GetBulgeAt(index)) < 1e-12);
    }

    private static object? TryExtents(Entity entity)
    {
        try
        {
            return ExtentsToObject(entity.GeometricExtents.MinPoint, entity.GeometricExtents.MaxPoint);
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            return null;
        }
    }

    private static object ExtentsToObject(Point3d minimum, Point3d maximum) => new Dictionary<string, double>
    {
        ["xmin"] = minimum.X,
        ["ymin"] = minimum.Y,
        ["zmin"] = minimum.Z,
        ["xmax"] = maximum.X,
        ["ymax"] = maximum.Y,
        ["zmax"] = maximum.Z,
    };

    private static object? GetSystemVariable(string name)
    {
        try
        {
            return Autodesk.AutoCAD.ApplicationServices.Core.Application.GetSystemVariable(name);
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            return null;
        }
    }

    private static string GetDocumentId(Document document)
    {
        // Include AutoCAD's database identity so two unsaved documents with
        // the same display name cannot share a session identity.
        var canonical = string.Join(
            "|",
            document.Name.Trim().ToUpperInvariant(),
            document.Database.FingerprintGuid ?? string.Empty
        );
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical))).ToLowerInvariant()[..24];
    }

    private static string ComputeDrawingFingerprint(
        string documentName,
        string databaseFingerprint,
        string databaseVersion,
        object? dbmod)
    {
        // VersionGuid establishes a new saved-content baseline. DBMOD detects
        // unsaved changes; view/layout variables stay out because they are
        // presentation state rather than source geometry.
        var canonicalName = documentName.Trim().ToUpperInvariant();
        var canonicalDbmod = System.Convert.ToString(dbmod, CultureInfo.InvariantCulture) ?? string.Empty;
        var canonical = string.Join("|", canonicalName, databaseFingerprint, databaseVersion, canonicalDbmod);
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical))).ToLowerInvariant();
    }

    private static string GetDatabaseVersionGuid(Database database)
    {
        // VersionGuid is present in supported AutoCAD releases, but reflection
        // keeps the bridge's read contract explicit if a future API removes it.
        return typeof(Database).GetProperty("VersionGuid")?.GetValue(database)?.ToString() ?? string.Empty;
    }

    private static string RequiredString(JsonElement parameters, string name)
    {
        if (!parameters.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(value.GetString()))
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} is required");
        }
        return value.GetString()!;
    }

    private static string OptionalString(JsonElement parameters, string name, string fallback)
    {
        return parameters.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback
            : fallback;
    }

    private static int OptionalInt(JsonElement parameters, string name, int fallback)
    {
        return parameters.TryGetProperty(name, out var value) && value.TryGetInt32(out var parsed) ? parsed : fallback;
    }

    private static bool OptionalBool(JsonElement parameters, string name, bool fallback)
    {
        return parameters.TryGetProperty(name, out var value) && value.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? value.GetBoolean()
            : fallback;
    }

    private static List<string> ReadStringArray(JsonElement parameters, string name)
    {
        if (!parameters.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Array)
        {
            return new List<string>();
        }
        return value.EnumerateArray()
            .Where(item => item.ValueKind == JsonValueKind.String)
            .Select(item => item.GetString()!)
            .Where(item => !string.IsNullOrWhiteSpace(item))
            .ToList();
    }

    private static Point2d RequiredPoint(JsonElement parameters, string name)
    {
        if (!parameters.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Array || value.GetArrayLength() < 2)
        {
            throw new BridgeFault(ErrorCodes.InvalidRequest, $"{name} must be [x,y]");
        }
        return new Point2d(value[0].GetDouble(), value[1].GetDouble());
    }
}
