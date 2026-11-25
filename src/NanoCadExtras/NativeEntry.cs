using System.Text.Json;
using HostMgd.ApplicationServices;
using Teigha.DatabaseServices;
using Teigha.Runtime;
using App = HostMgd.ApplicationServices.Application;

[assembly: CommandClass(typeof(CadEngine.NativeRoutes))]

namespace CadEngine;

public sealed record NativeRequest(int Pid, string Document, string Method, string Path, JsonElement Body)
{
    public string Query => Path.Contains('?') ? Path.Substring(Path.IndexOf('?')) : "";
}

public sealed partial class NativeRoutes
{
    [CommandMethod("NCMCPExtrasInfo")]
    public void Info() => CadContext.ActiveDocument?.Editor.WriteMessage("\nNanoCAD MCP Extras 26\n");

    // LispFunction runs on the UI thread of the process that received SendCommand.
    [LispFunction("NCMCP_EXTRA")]
    public string Invoke(ResultBuffer args)
    {
        try
        {
            var data = args.AsArray();
            var request = JsonSerializer.Deserialize<NativeRequest>(File.ReadAllText((string)data[0].Value))
                          ?? throw new ArgumentException("Empty native request");
            if (request.Pid != Environment.ProcessId || !string.Equals(request.Document,
                CadContext.ActiveDocument?.Name, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("The active nanoCAD window or drawing changed");
            var path = request.Path.Split('?')[0];
            var result = RouteRequest(request.Method, path, request);
            if (result == null) throw new NotSupportedException(path);
            return JsonSerializer.Serialize(result, JsonOptions);
        }
        catch (System.Exception ex)
        {
            return JsonSerializer.Serialize(new { success = false, error = ex.Message });
        }
    }
}

// The upstream services use these adapters for their HTTP worker. Our entry point
// is already on the CAD thread, and reads the current document for every call.
internal static class MainThreadExecutor
{
    public static object? Execute(Func<object?> action, int timeoutMs = 30000) => action();
}

internal static class CadContext
{
    public static Document? ActiveDocument => App.DocumentManager.MdiActiveDocument;
    public static void RefreshDocument() { }
    public static bool SendCommand(string command)
    {
        var doc = ActiveDocument;
        if (doc == null) return false;
        doc.SendStringToExecute(command, true, false, false);
        return true;
    }
}

internal static class PluginEntry
{
    public static void DebugLog(string message) => System.Diagnostics.Trace.WriteLine(message);
}
