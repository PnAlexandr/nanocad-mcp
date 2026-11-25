using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using HostMgd.ApplicationServices;
using Teigha.DatabaseServices;
using Teigha.Runtime;

[assembly: CommandClass(typeof(NanoCadMcp.WindowRegistration))]

namespace NanoCadMcp;

public sealed class WindowRegistration
{
    private static int? cookie;

    [DllImport("ole32.dll", PreserveSig = false)]
    private static extern void GetRunningObjectTable(int reserved, out IRunningObjectTable table);

    [DllImport("ole32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void CreateItemMoniker(string delimiter, string item, out IMoniker moniker);

    [LispFunction("NCMCP_WINDOW")]
    public string SetRegistration(ResultBuffer args)
    {
        try
        {
            string? action = args?.AsArray()[0].Value as string;
            GetRunningObjectTable(0, out var table);
            if (cookie.HasValue)
            {
                table.Revoke(cookie.Value);
                cookie = null;
            }
            if (action == "on")
            {
                CreateItemMoniker("!", $"NanoCAD-MCP.Window.{Environment.ProcessId}", out var name);
                cookie = table.Register(0, Application.AcadApplication, name);
            }
            return "OK";
        }
        catch (System.Exception ex)
        {
            return ex.Message;
        }
    }
}
