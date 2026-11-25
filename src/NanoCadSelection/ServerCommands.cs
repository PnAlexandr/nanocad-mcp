using HostMgd.ApplicationServices;
using Teigha.DatabaseServices;
using Teigha.Runtime;

[assembly: CommandClass(typeof(NanoCadSelection.ServerCommands))]

namespace NanoCadSelection;

public sealed class ServerCommands
{
    [CommandMethod("NCMCPGETSELECTION", CommandFlags.UsePickSet | CommandFlags.Redraw)]
    public void PrintSelection()
    {
        var doc = Application.DocumentManager.MdiActiveDocument;
        doc.Editor.WriteMessage("\n" + SelectionReader.ReadSelection(doc, 0, 50) + "\n");
    }

    [LispFunction("NCMCP_GET_SELECTION")]
    public string Read(ResultBuffer? args) => SelectionReader.SelectionJson(args);

    [LispFunction("NCMCP_MEASURE_SELECTION")]
    public string Measure(ResultBuffer? args) =>
        SelectionReader.MeasureSelection(Application.DocumentManager.MdiActiveDocument);
}
