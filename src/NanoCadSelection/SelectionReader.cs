using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using HostMgd.ApplicationServices;
using HostMgd.EditorInput;
using Teigha.DatabaseServices;
using Teigha.Runtime;

namespace NanoCadSelection;

internal static class SelectionReader
{
    internal static string SelectionJson(ResultBuffer? args)
    {
        var values = args?.AsArray() ?? Array.Empty<TypedValue>();
        int offset = 0, limit = 50;
        if (values.Length > 2 ||
            (values.Length > 0 && !ReadInteger(values[0], out offset)) ||
            (values.Length > 1 && !ReadInteger(values[1], out limit)) ||
            offset < 0 || limit < 1 || limit > 500)
        {
            return JsonSerializer.Serialize(new { ok = false, error = "Expected offset >= 0 and limit in 1..500." });
        }
        return ReadSelection(Application.DocumentManager.MdiActiveDocument, offset, limit);
    }

    private static bool ReadInteger(TypedValue argument, out int result)
    {
        result = 0;
        if (argument.Value is short small) { result = small; return true; }
        if (argument.Value is int integer) { result = integer; return true; }
        return false;
    }

    private static ObjectId[] SelectedIds(Document doc)
    {
        PromptSelectionResult selection = doc.Editor.SelectImplied();
        if (selection.Status != PromptStatus.OK && selection.Status != PromptStatus.Error &&
            selection.Status != PromptStatus.None)
            throw new InvalidOperationException("SelectImplied: " + selection.Status);

        return selection.Status == PromptStatus.OK
            ? selection.Value.GetObjectIds() : Array.Empty<ObjectId>();
    }

    internal static string ReadSelection(Document doc, int offset, int limit)
    {
        var ids = SelectedIds(doc);
        var items = new List<object>();
        using var transaction = doc.Database.TransactionManager.StartOpenCloseTransaction();
        foreach (ObjectId id in ids.Skip(offset).Take(limit))
        {
            try
            {
                var entity = (Entity)transaction.GetObject(id, OpenMode.ForRead);
                items.Add(Describe(entity, transaction));
            }
            catch (Teigha.Runtime.Exception error)
            {
                items.Add(new { handle = id.Handle.ToString(), error = error.Message });
            }
        }
        return JsonSerializer.Serialize(new { ok = true, document = doc.Name,
            units = doc.Database.Insunits.ToString(), count = ids.Length, offset, returned = items.Count, items });
    }

    internal static string MeasureSelection(Document doc)
    {
        var ids = SelectedIds(doc);
        var skipped = new Dictionary<string, int>();
        var errors = new List<object>();
        int failed = 0, lengthCount = 0, areaCount = 0;
        double length = 0, area = 0;
        using var transaction = doc.Database.TransactionManager.StartOpenCloseTransaction();
        foreach (var id in ids)
        {
            try
            {
                var entity = (Entity)transaction.GetObject(id, OpenMode.ForRead);
                if (entity is Hatch hatch)
                {
                    area += hatch.Area;
                    areaCount++;
                    continue;
                }
                if (entity is not (Line or Arc or Circle or Polyline or Polyline2d or Polyline3d or Ellipse or Spline))
                {
                    string type = entity.GetRXClass().DxfName;
                    skipped[type] = skipped.GetValueOrDefault(type) + 1;
                    continue;
                }
                var curve = (Curve)entity;
                length += curve is Line line ? line.Length :
                    curve.GetDistanceAtParameter(curve.EndParam) - curve.GetDistanceAtParameter(curve.StartParam);
                lengthCount++;
                if (curve.Closed && curve.IsPlanar)
                {
                    area += Math.Abs(curve.Area);
                    areaCount++;
                }
            }
            catch (Teigha.Runtime.Exception error)
            {
                failed++;
                if (errors.Count < 30) errors.Add(new { handle = id.Handle.ToString(), error = error.Message });
            }
        }
        return JsonSerializer.Serialize(new { ok = true, document = doc.Name, units = doc.Database.Insunits.ToString(),
            count = ids.Length, length, area, length_count = lengthCount, area_count = areaCount,
            skipped_by_type = skipped, failed_count = failed, errors });
    }

    private static Dictionary<string, object> Describe(Entity entity, Transaction transaction)
    {
        var result = new Dictionary<string, object>
        {
            ["handle"] = entity.Handle.ToString(),
            ["type"] = entity.GetRXClass().DxfName,
            ["layer"] = entity.Layer
        };
        if (entity is Line line)
        {
            result["start_wcs"] = line.StartPoint.ToArray();
            result["end_wcs"] = line.EndPoint.ToArray();
            result["length"] = line.Length;
            return result;
        }
        if (entity is not BlockReference block) return result;

        ObjectId definitionId = block.IsDynamicBlock ? block.DynamicBlockTableRecord : block.BlockTableRecord;
        var definition = (BlockTableRecord)transaction.GetObject(definitionId, OpenMode.ForRead);
        result["block"] = definition.Name;
        result["insertion_wcs"] = new[] { block.Position.X, block.Position.Y, block.Position.Z };

        var attributes = new List<object>();
        foreach (ObjectId attributeId in block.AttributeCollection)
        {
            if (transaction.GetObject(attributeId, OpenMode.ForRead) is AttributeReference attribute)
                attributes.Add(new { tag = attribute.Tag, value = attribute.TextString });
        }
        foreach (ObjectId definitionEntityId in definition)
        {
            if (!definitionEntityId.ObjectClass.IsDerivedFrom(RXObject.GetClass(typeof(AttributeDefinition)))) continue;
            var attribute = (AttributeDefinition)transaction.GetObject(definitionEntityId, OpenMode.ForRead);
            if (attribute.Constant) attributes.Add(new { tag = attribute.Tag, value = attribute.TextString });
        }
        result["attributes"] = attributes;
        return result;
    }
}
