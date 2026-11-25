using HostMgd.ApplicationServices;
using Teigha.DatabaseServices;
using Teigha.Runtime;

[assembly: CommandClass(typeof(NanoCadSelection.SelectionCommands))]

namespace NanoCadSelection;

public sealed class SelectionCommands
{
    // Both entry points read the current document on the CAD command thread.
    [CommandMethod("NCMCPREADSELECTION", CommandFlags.UsePickSet | CommandFlags.Redraw)]
    public void PrintSelection()
    {
        var doc = Application.DocumentManager.MdiActiveDocument;
        doc.Editor.WriteMessage("\n" + SelectionReader.ReadSelection(doc, 0, 50) + "\n");
    }

    [LispFunction("NCMCP_SELECTION")]
    public string SelectionJson(ResultBuffer? args) => SelectionReader.SelectionJson(args);
}
