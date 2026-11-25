using HostMgd.ApplicationServices;
using System;
using System.Text;
using System.Text.Json;
using Teigha.DatabaseServices;
using Teigha.Runtime;

[assembly: CommandClass(typeof(NanoCadSelection.ServerCommands))]

namespace NanoCadSelection;

public sealed class ServerCommands
{
    [LispFunction("NCMCP_QUEUE_SPECIALTY")]
    public string QueueSpecialty(ResultBuffer? args)
    {
        var doc = Application.DocumentManager.MdiActiveDocument;
        var values = args?.AsArray();
        var command = values?.Length == 2 ? values[0].Value as string : null;
        if (doc == null || command is not ("CW_PROJECT" or "CW_SHOW_PROPERTIES" or "GcSfRedPntGradeMarker")
            || values![1].Value is not string encodedName)
            return JsonSerializer.Serialize(new { ok = false, error = "Неизвестная команда или нет активного DWG." });

        string expected;
        try { expected = Encoding.UTF8.GetString(Convert.FromBase64String(encodedName)); }
        catch (FormatException) { return JsonSerializer.Serialize(new { ok = false, error = "Некорректное имя чертежа." }); }
        if (!String.Equals(doc.Name, expected, StringComparison.OrdinalIgnoreCase))
            return JsonSerializer.Serialize(new { ok = false, error = "Изменился чертёж. Повторите предварительный просмотр." });

        // Start the interactive command after the LispFunction returns to the editor.
        doc.SendStringToExecute("_" + command + "\n", false, false, false);
        return JsonSerializer.Serialize(new { ok = true, document = doc.Name, command, state = "queued" });
    }

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
